"""Compose バンドル用の共通土台 (docker-base rootfs) の準備。

services/firecracker_setup.py から分離した責務のひとつ。Debian + Docker + compose を
含み、/sbin/init が build/run の2モードで動く rootfs を Debian コンテナ内で構築し、
docker export で取り出して ext4 へ変換する。

実際のビルド処理（docker.sock が必要）は docker-base-builder コンテナに委譲する。
orchestrator は Firecracker 脱出時の被害を抑えるため docker.sock を一切持たない
（CLAUDE.md 参照）。このモジュールは HTTP 経由でジョブを依頼し、完了をポーリングするだけ。
"""
import logging
import os
import uuid

from config import settings
from services.rootfs_tar import run_builder_job
from services.vm_assets import (
    DEFAULTS_DIR,
    DOCKER_BASE_DEBIAN_IMAGE,
    DOCKER_BASE_LABEL,
    DOCKER_BASE_ROOTFS_PATH,
    DOCKER_BASE_ROOTFS_SIZE_MB,
)

logger = logging.getLogger(__name__)

# 管理者が /admin から docker-base をビルドする際に選べるベースイメージ。
# _DOCKER_BASE_BUILD_SCRIPT が apt-get + docker-ce 公式リポジトリ（Debian系専用）を
# 前提にしているため、現時点では Debian 系のみを許可する（UI の先駆けとして用意し、
# 対応系統を増やす場合はビルドスクリプト自体を OS 系統ごとに分岐させる必要がある）。
ALLOWED_DOCKER_BASE_IMAGES = {
    "debian:bookworm-slim": "Debian (bookworm-slim)",
}

_POLL_INTERVAL_SECONDS = 10


def _resolv_conf_for_builder() -> str:
    nameservers = [ns.strip() for ns in settings.FC_GUEST_DNS.split(",") if ns.strip()]
    return "\\n".join(f"nameserver {ns}" for ns in nameservers) + "\\n"


async def build_docker_base_rootfs_at(dest_path: str, base_image: str = DOCKER_BASE_DEBIAN_IMAGE) -> None:
    """docker-base rootfs（Debian 系 + Docker + compose）を dest_path に構築する。

    実際のビルド（Debian コンテナ起動・docker export・ext4変換）は docker-base-builder
    コンテナが行う。orchestrator はジョブを HTTP で依頼し、完了をポーリングするだけ
    （docker.sock は orchestrator に一切持たせない。CLAUDE.md 参照）。
    dest_path は DEFAULTS_DIR 配下である必要がある（docker-base-builder の /output が
    ホスト上で DEFAULTS_DIR を指すボリュームにマウントされているため）。
    dest_path に既にファイルがあれば再構築しない。
    """
    if os.path.exists(dest_path):
        logger.info(f"docker-base rootfs は既に存在します: {dest_path}")
        return
    defaults_real = os.path.realpath(DEFAULTS_DIR)
    dest_real = os.path.realpath(dest_path)
    if not (dest_real == defaults_real or dest_real.startswith(defaults_real + os.sep)):
        raise RuntimeError(f"dest_path は DEFAULTS_DIR 配下である必要があります: {dest_path}")

    logger.info(f"docker-base rootfs を構築中...（数分かかる場合があります）base_image={base_image}")
    job_id = uuid.uuid4().hex
    dest_filename = os.path.basename(dest_path)

    await run_builder_job(
        settings.DOCKER_BASE_BUILDER_URL,
        "/build",
        {
            "job_id": job_id,
            "base_image": base_image,
            "resolv_conf": _resolv_conf_for_builder(),
            "dest_filename": dest_filename,
            "size_mb": DOCKER_BASE_ROOTFS_SIZE_MB,
        },
        job_id,
    )

    logger.info(f"docker-base rootfs を保存しました: {dest_path}")


async def build_docker_base_rootfs() -> None:
    """起動時オーケストレーション用: 既定パス・既定ベースイメージで docker-base rootfs を構築する。"""
    await build_docker_base_rootfs_at(DOCKER_BASE_ROOTFS_PATH, DOCKER_BASE_DEBIAN_IMAGE)


async def register_docker_base_asset() -> None:
    """docker-base rootfs を kind='docker-base' の DefaultRootfsAsset として登録する。"""
    if not os.path.exists(DOCKER_BASE_ROOTFS_PATH):
        return
    from database import AsyncSessionLocal
    from models import DefaultRootfsAsset
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(DefaultRootfsAsset).where(DefaultRootfsAsset.file_path == DOCKER_BASE_ROOTFS_PATH)
        )
        asset = existing.scalar_one_or_none()
        if asset is None:
            db.add(DefaultRootfsAsset(
                label=DOCKER_BASE_LABEL, file_path=DOCKER_BASE_ROOTFS_PATH, kind="docker-base",
            ))
            logger.info("docker-base rootfs を資産として登録しました")
        else:
            asset.label = DOCKER_BASE_LABEL
            asset.kind = "docker-base"
        await db.commit()


# ---- 管理者が /admin から追加登録する docker-base 資産のビルドワーカー ----
# rootfs_convert.py と同じ「pending を行ロックで claim → 処理 → ready/failed」パターン。
# 対象は DefaultRootfsAsset(kind="docker-base") のうち base_image が設定されている行のみ
# （起動時に ensure_default_vm_assets が用意する既定の docker-base 資産は base_image 不要で
#  このワーカーの対象にはならない）。

async def _claim_pending_docker_base() -> tuple[int, str, str] | None:
    from database import AsyncSessionLocal
    from models import DefaultRootfsAsset, RootfsConversionStatus
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DefaultRootfsAsset)
            .where(
                DefaultRootfsAsset.kind == "docker-base",
                DefaultRootfsAsset.conversion_status == RootfsConversionStatus.pending.value,
                DefaultRootfsAsset.base_image.isnot(None),
            )
            .with_for_update()
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.conversion_status = RootfsConversionStatus.converting.value
        await db.flush()
        row_id, base_image, dest_path = row.id, row.base_image, row.file_path
        await db.commit()
        return row_id, base_image, dest_path


async def _finish_docker_base(row_id: int, status: str, error: str | None) -> None:
    from database import AsyncSessionLocal
    from models import DefaultRootfsAsset

    async with AsyncSessionLocal() as db:
        row = await db.get(DefaultRootfsAsset, row_id)
        if row is not None:
            row.conversion_status = status
            row.conversion_error = error
            await db.commit()


async def run_docker_base_build_worker() -> None:
    """管理者が登録した docker-base 資産（pending）を順次ビルドするバックグラウンドループ。"""
    logger.info("docker-base build worker started")
    while True:
        try:
            claimed = await _claim_pending_docker_base()
            if claimed is not None:
                row_id, base_image, dest_path = claimed
                from models import RootfsConversionStatus
                try:
                    await build_docker_base_rootfs_at(dest_path, base_image)
                    await _finish_docker_base(row_id, RootfsConversionStatus.ready.value, None)
                    logger.info(f"docker-base 資産のビルドが完了しました: id={row_id} -> {dest_path}")
                except Exception as e:
                    message = str(e).strip()[:512]
                    await _finish_docker_base(row_id, RootfsConversionStatus.failed.value, message)
                    logger.error(f"docker-base 資産のビルドに失敗しました: id={row_id}: {e}")
                continue
        except Exception as e:
            logger.error(f"docker-base build worker error: {e}")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
