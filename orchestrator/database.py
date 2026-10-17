from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import event, text
from config import settings
from models import Base
import logging

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """WAL化 + busy_timeout で複数asyncioタスクからの同時書き込み時の "database is locked" を防ぐ。
    pysqlite/aiosqliteのデフォルトisolation_level("")はDML前に独自にBEGINを発行し、
    SQLAlchemyのトランザクション管理と二重に競合してWAL下でも即時 "database is locked"
    を引き起こすため、isolation_level=Noneにしてドライバ側の暗黙トランザクションを無効化する。
    """
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()


@event.listens_for(engine.sync_engine, "begin")
def _do_begin(conn):
    """isolation_level=None化に伴い、SQLAlchemyのトランザクション開始時に明示的にBEGINを発行する。"""
    conn.exec_driver_sql("BEGIN")


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _migrate(conn):
    """既存 DB へのカラム追加など create_all で対応できない差分を適用する。"""
    # (table, column, DDL)
    migrations = [
        ("images",  "archive_path",         "ALTER TABLE images  ADD COLUMN archive_path         VARCHAR(512)"),
        ("images",  "owner_id",             "ALTER TABLE images  ADD COLUMN owner_id             INTEGER REFERENCES users(id)"),
        ("images",  "visibility",           "ALTER TABLE images  ADD COLUMN visibility           VARCHAR(16) NOT NULL DEFAULT 'protected'"),
        ("users",   "display_name",         "ALTER TABLE users   ADD COLUMN display_name         VARCHAR(128)"),
        ("users",   "needs_username_setup", "ALTER TABLE users   ADD COLUMN needs_username_setup BOOLEAN NOT NULL DEFAULT 0"),
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
        ("images", "rootfs_conversion_status", "ALTER TABLE images ADD COLUMN rootfs_conversion_status VARCHAR(16) NOT NULL DEFAULT 'ready'"),
        ("images", "rootfs_conversion_error", "ALTER TABLE images ADD COLUMN rootfs_conversion_error VARCHAR(512)"),
        ("images", "rootfs_source_archive_path", "ALTER TABLE images ADD COLUMN rootfs_source_archive_path VARCHAR(512)"),
        ("images", "is_attack_machine", "ALTER TABLE images ADD COLUMN is_attack_machine BOOLEAN NOT NULL DEFAULT 0"),
    ]
    for table, column, ddl in migrations:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        columns = [row[1] for row in result.fetchall()]
        if column not in columns:
            await conn.execute(text(ddl))
            logger.info(f"Migration applied: {table}.{column}")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
