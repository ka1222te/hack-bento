import asyncio
import logging
import os

from sqlalchemy import select

from database import AsyncSessionLocal
from models import DefaultRootfsAsset, Image, RootfsConversionStatus
from services.firecracker_setup import convert_tar_to_ext4

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 10

# 変換対象となりうるテーブル・カラムの組（共有デフォルト資産／プロジェクトのrootfsの両方）。
# どちらも DefaultRootfsAsset と同じ4カラム構成（状態・エラー・変換元tarパス・実体パス）を持つ。
_TARGETS = [
    {
        "model": DefaultRootfsAsset,
        "status_col": "conversion_status",
        "error_col": "conversion_error",
        "source_col": "source_archive_path",
        "path_col": "file_path",
    },
    {
        "model": Image,
        "status_col": "rootfs_conversion_status",
        "error_col": "rootfs_conversion_error",
        "source_col": "rootfs_source_archive_path",
        "path_col": "rootfs_path",
    },
]


async def _claim_pending() -> tuple[dict, int, str, str] | None:
    """変換待ち(pending)の行を1件、行ロック付きで取得し converting へ遷移させる。

    claim_env_for_stop（services/watchdog.py）と同じ「SELECT ... FOR UPDATE で行ロック
    した上で状態遷移する」パターンにより、複数ワーカーが同時稼働しても二重変換を防ぐ。
    DefaultRootfsAsset / Image の両テーブルを順に確認する。
    """
    async with AsyncSessionLocal() as db:
        for target in _TARGETS:
            model = target["model"]
            status_col = getattr(model, target["status_col"])
            result = await db.execute(
                select(model)
                .where(status_col == RootfsConversionStatus.pending.value)
                .with_for_update()
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                continue
            setattr(row, target["status_col"], RootfsConversionStatus.converting.value)
            await db.flush()
            row_id = row.id
            tar_path = getattr(row, target["source_col"])
            dest_path = getattr(row, target["path_col"])
            await db.commit()
            return target, row_id, tar_path, dest_path
    return None


async def _finish(target: dict, row_id: int, status: str, error: str | None) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(target["model"], row_id)
        if row is not None:
            setattr(row, target["status_col"], status)
            setattr(row, target["error_col"], error)
            setattr(row, target["source_col"], None)
            await db.commit()


async def _convert_one(target: dict, row_id: int, tar_path: str, dest_path: str) -> None:
    label = target["model"].__tablename__
    try:
        await convert_tar_to_ext4(tar_path, dest_path)
        await _finish(target, row_id, RootfsConversionStatus.ready.value, None)
        logger.info(f"rootfs 変換が完了しました: {label}.id={row_id} -> {dest_path}")
    except Exception as e:
        message = str(e).strip()[:512]
        await _finish(target, row_id, RootfsConversionStatus.failed.value, message)
        logger.error(f"rootfs 変換に失敗しました: {label}.id={row_id}: {e}")
    finally:
        if tar_path and os.path.exists(tar_path):
            try:
                os.remove(tar_path)
            except OSError:
                pass


async def run_rootfs_conversion_worker() -> None:
    """pending 状態の rootfs（共有デフォルト資産・プロジェクト個別の両方）を順次 ext4 へ変換する
    バックグラウンドループ。

    run_watchdog（services/watchdog.py）と同じ定期ポーリング構成だが、ユーザ操作起点の
    変換ジョブのため待機間隔は短め（10秒）にしている。
    """
    logger.info("Rootfs conversion worker started")
    while True:
        try:
            claimed = await _claim_pending()
            if claimed is not None:
                target, row_id, tar_path, dest_path = claimed
                await _convert_one(target, row_id, tar_path, dest_path)
                continue
        except Exception as e:
            logger.error(f"Rootfs conversion worker error: {e}")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
