from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text
from config import settings
from models import Base
import asyncio
import logging

logger = logging.getLogger(__name__)

# FastAPIの Depends(...) ジェネレーター依存（get_db/get_db_readonly）は、リクエスト
# ハンドラの処理が完全に終わるまでセッション（＝プールからのコネクション）を保持し続ける。
# VM起動（start_env）は複数の依存（認証チェック・本体のdb）で同時に2本のセッションを
# 開いたまま、Firecracker起動という数十秒かかる処理を実行するため、同時に複数の
# VM起動・ページアクセスが重なるとデフォルトのプール（pool_size=5+max_overflow=10=15）
# を食い尽くし、それ以降の全リクエストが新規コネクション取得待ちで無期限にハングする
# 事故が起きた（実際に発生: pg_stat_activityのidle in transactionが
# 蓄積し続けてプール枯渇）。pool_size/max_overflowを増やして当面の限界値を上げ、
# pool_timeoutで「無期限ハング」を「明確なタイムアウトエラー」に変える。
engine = create_async_engine(
    settings.DATABASE_URL, echo=False,
    pool_size=20, max_overflow=20, pool_timeout=30,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# PostgreSQL は MVCC（読み取りは書き込みをブロックしない、書き込み同士も行ロック単位で
# 競合する）のため、読み取り専用の別エンジンを用意する必要がない。get_db/get_db_readonly
# はどちらも同じエンジンのセッションを返す。
AsyncSessionLocalReadonly = AsyncSessionLocal


async def init_db():
    # postgresコンテナの起動完了（healthcheck通過）前に接続を試みて失敗することがある
    # ため、起動直後のみ短いリトライを行う。
    attempts = 10
    for attempt in range(1, attempts + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await _migrate(conn)
                await _ensure_global_settings(conn)
            return
        except Exception as e:
            if attempt == attempts:
                raise
            logger.warning(f"DB接続に失敗しました（{attempt}/{attempts}回目）。3秒後に再試行します: {e}")
            await asyncio.sleep(3)


async def _ensure_global_settings(conn):
    """GlobalSetting の id=1 行が無ければ、.env の値を初期値として投入する。

    以後の値は admin 画面からの変更が正になる（.env を変更しても再投入されない）。
    """
    result = await conn.execute(text("SELECT id FROM global_settings WHERE id = 1"))
    if result.first() is None:
        await conn.execute(
            text(
                "INSERT INTO global_settings "
                "(id, rootfs_margin_max_mb, vm_cpu_max, vm_memory_max_mb, vm_pids_max, vm_disk_max_gb) "
                "VALUES (1, :margin, :cpu, :mem, :pids, :disk)"
            ),
            {
                "margin": settings.ROOTFS_CONVERT_SIZE_MARGIN_MB,
                "cpu": settings.VM_CPU_LIMIT,
                "mem": settings.VM_MEMORY_LIMIT_MB,
                "pids": settings.VM_PIDS_LIMIT,
                "disk": settings.VM_DISK_LIMIT_GB,
            },
        )
        logger.info(
            "GlobalSetting initialized: "
            f"rootfs_margin_max_mb={settings.ROOTFS_CONVERT_SIZE_MARGIN_MB} "
            f"vm_cpu_max={settings.VM_CPU_LIMIT} vm_memory_max_mb={settings.VM_MEMORY_LIMIT_MB} "
            f"vm_pids_max={settings.VM_PIDS_LIMIT} vm_disk_max_gb={settings.VM_DISK_LIMIT_GB}"
        )


async def _migrate(conn):
    """既存 DB へのカラム追加など create_all で対応できない差分を適用する。"""
    # (table, column, DDL)
    migrations = [
        ("images",  "archive_path",         "ALTER TABLE images  ADD COLUMN archive_path         VARCHAR(512)"),
        ("images",  "owner_id",             "ALTER TABLE images  ADD COLUMN owner_id             INTEGER REFERENCES users(id)"),
        ("images",  "visibility",           "ALTER TABLE images  ADD COLUMN visibility           VARCHAR(16) NOT NULL DEFAULT 'protected'"),
        ("users",   "display_name",         "ALTER TABLE users   ADD COLUMN display_name         VARCHAR(128)"),
        ("users",   "needs_username_setup", "ALTER TABLE users   ADD COLUMN needs_username_setup BOOLEAN NOT NULL DEFAULT FALSE"),
        ("images",  "readme_path",          "ALTER TABLE images  ADD COLUMN readme_path          VARCHAR(512)"),
        # backend は Firecracker 一本化後は未使用の後方互換カラム（既定 'bridge'）
        ("images",  "backend",              "ALTER TABLE images  ADD COLUMN backend              VARCHAR(16) NOT NULL DEFAULT 'bridge'"),
        ("images",  "kernel_path",          "ALTER TABLE images  ADD COLUMN kernel_path          VARCHAR(512)"),
        ("images",  "rootfs_path",          "ALTER TABLE images  ADD COLUMN rootfs_path          VARCHAR(512)"),
        ("images",  "default_kernel_asset_id", "ALTER TABLE images ADD COLUMN default_kernel_asset_id INTEGER REFERENCES default_kernel_assets(id)"),
        ("images",  "default_rootfs_asset_id", "ALTER TABLE images ADD COLUMN default_rootfs_asset_id INTEGER REFERENCES default_rootfs_assets(id)"),
        ("default_rootfs_assets", "conversion_status", "ALTER TABLE default_rootfs_assets ADD COLUMN conversion_status VARCHAR(16) NOT NULL DEFAULT 'ready'"),
        ("default_rootfs_assets", "conversion_error", "ALTER TABLE default_rootfs_assets ADD COLUMN conversion_error VARCHAR(512)"),
        ("default_rootfs_assets", "source_archive_path", "ALTER TABLE default_rootfs_assets ADD COLUMN source_archive_path VARCHAR(512)"),
        ("default_rootfs_assets", "kind", "ALTER TABLE default_rootfs_assets ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'os'"),
        ("images", "rootfs_kind", "ALTER TABLE images ADD COLUMN rootfs_kind VARCHAR(16) NOT NULL DEFAULT 'os'"),
        ("images", "base_rootfs_asset_id", "ALTER TABLE images ADD COLUMN base_rootfs_asset_id INTEGER REFERENCES default_rootfs_assets(id)"),
        ("images", "compose_summary", "ALTER TABLE images ADD COLUMN compose_summary TEXT"),
        ("images", "extra_boot_args", "ALTER TABLE images ADD COLUMN extra_boot_args VARCHAR(512)"),
        ("images", "rootfs_conversion_status", "ALTER TABLE images ADD COLUMN rootfs_conversion_status VARCHAR(16) NOT NULL DEFAULT 'ready'"),
        ("images", "rootfs_conversion_error", "ALTER TABLE images ADD COLUMN rootfs_conversion_error VARCHAR(512)"),
        ("images", "rootfs_source_archive_path", "ALTER TABLE images ADD COLUMN rootfs_source_archive_path VARCHAR(512)"),
        ("images", "is_attack_machine", "ALTER TABLE images ADD COLUMN is_attack_machine BOOLEAN NOT NULL DEFAULT FALSE"),
        ("default_rootfs_assets", "base_image", "ALTER TABLE default_rootfs_assets ADD COLUMN base_image VARCHAR(256)"),
        ("images", "build_phase", "ALTER TABLE images ADD COLUMN build_phase VARCHAR(32)"),
        ("environments", "is_unlimited", "ALTER TABLE environments ADD COLUMN is_unlimited BOOLEAN NOT NULL DEFAULT FALSE"),
        ("images", "rootfs_margin_mb", "ALTER TABLE images ADD COLUMN rootfs_margin_mb INTEGER"),
        ("images", "pids_limit", "ALTER TABLE images ADD COLUMN pids_limit INTEGER"),
        ("global_settings", "vm_cpu_max", "ALTER TABLE global_settings ADD COLUMN vm_cpu_max INTEGER NOT NULL DEFAULT 1"),
        ("global_settings", "vm_memory_max_mb", "ALTER TABLE global_settings ADD COLUMN vm_memory_max_mb INTEGER NOT NULL DEFAULT 1024"),
        ("global_settings", "vm_pids_max", "ALTER TABLE global_settings ADD COLUMN vm_pids_max INTEGER NOT NULL DEFAULT 256"),
        ("global_settings", "vm_disk_max_gb", "ALTER TABLE global_settings ADD COLUMN vm_disk_max_gb INTEGER NOT NULL DEFAULT 10"),
        ("environments", "last_error", "ALTER TABLE environments ADD COLUMN last_error VARCHAR(512)"),
        ("environments", "vm_processing_started_at", "ALTER TABLE environments ADD COLUMN vm_processing_started_at TIMESTAMP"),
    ]
    newly_added = set()
    for table, column, ddl in migrations:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table"
            ),
            {"table": table},
        )
        columns = [row[0] for row in result.fetchall()]
        if column not in columns:
            await conn.execute(text(ddl))
            logger.info(f"Migration applied: {table}.{column}")
            newly_added.add((table, column))

    # global_settings に既存行がある状態で新カラムが追加された場合、DDLのデフォルト値
    # ではなく .env の値で初期化する（_ensure_global_settings は新規行のみを対象とするため）。
    global_settings_env_defaults = {
        "vm_cpu_max": settings.VM_CPU_LIMIT,
        "vm_memory_max_mb": settings.VM_MEMORY_LIMIT_MB,
        "vm_pids_max": settings.VM_PIDS_LIMIT,
        "vm_disk_max_gb": settings.VM_DISK_LIMIT_GB,
    }
    for column, env_value in global_settings_env_defaults.items():
        if ("global_settings", column) in newly_added:
            await conn.execute(
                text(f"UPDATE global_settings SET {column} = :v WHERE id = 1"),
                {"v": env_value},
            )


async def get_db():
    """書き込みを行うエンドポイント用。POST/PATCH/DELETEやバックグラウンドワーカーで使う。"""
    async with AsyncSessionLocal() as session:
        yield session


async def get_db_readonly():
    """読み取り専用エンドポイント用。GETで一覧・詳細取得のみ行うエンドポイントはこちらを使う。
    セッション内で書き込み（INSERT/UPDATE/DELETE）を行わないこと。
    """
    async with AsyncSessionLocalReadonly() as session:
        yield session
