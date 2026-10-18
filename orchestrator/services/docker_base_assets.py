"""Compose バンドル用の共通土台 (docker-base rootfs) の準備。

services/firecracker_setup.py から分離した責務のひとつ。Debian + Docker + compose を
含み、/sbin/init が build/run の2モードで動く rootfs を Debian コンテナ内で構築し、
docker export で取り出して ext4 へ変換する。
"""
import asyncio
import logging
import os
import shutil
import tempfile

from config import settings
from services.rootfs_tar import convert_tar_to_ext4, docker_available, run as _run, run_to_file as _run_to_file
from services.vm_assets import (
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

# docker-base の /sbin/init。
#   build モード (カーネル引数に hb_build): scratch(/dev/vdb) のバンドルを docker load /
#     overlay 展開 / compose 設定を正規化出力し、結果を scratch に書き戻して poweroff。
#   run モード (通常起動): dockerd を起動し /opt/app の compose を up して居座る。
# ゲストカーネル(6.1)に nftables が無いため iptables は legacy に固定する。
_DOCKER_BASE_INIT_TEMPLATE = r"""#!/bin/sh
# PID1 は PATH を継承しないため明示設定する（これが無いと dockerd が
# containerd/runc を $PATH から見つけられず起動に失敗する）。
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# ---- 基本マウント ----
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true
# Docker / コンテナ用に cgroup v2 を単一階層でマウント
mkdir -p /sys/fs/cgroup
mount -t cgroup2 cgroup2 /sys/fs/cgroup 2>/dev/null || true
ip link set lo up 2>/dev/null || true

is_build=0
if grep -q 'hb_build' /proc/cmdline 2>/dev/null; then is_build=1; fi

# $1: "build" なら networking を無効化(iptables/bridge なし)で起動する。
# build フェーズは docker load と compose config のみでコンテナ networking を使わないため、
# ゲストカーネルの netfilter モジュール有無に依存せず確実に dockerd を起動できる。
# run フェーズは実際に compose up するため iptables/bridge を有効にする。
start_dockerd() {
    # storage-driver は /etc/docker/daemon.json で指定済み（CLI と二重指定すると
    # dockerd が起動拒否するため、ここでは渡さない）。
    if [ "$1" = "build" ]; then
        dockerd --iptables=false --ip6tables=false --bridge=none --userland-proxy=false >/var/log/dockerd.log 2>&1 &
    else
        dockerd --iptables=true --userland-proxy=false >/var/log/dockerd.log 2>&1 &
    fi
    i=0
    while [ $i -lt 60 ]; do
        if docker info >/dev/null 2>&1; then return 0; fi
        i=$((i+1)); sleep 1
    done
    echo "[hackbento] dockerd が起動しませんでした" >&2
    return 1
}

if [ "$is_build" = "1" ]; then
    # ===== build モード: scratch(/dev/vdb) のバンドルを取り込んで ext4 に焼き込む =====
    mkdir -p /mnt/scratch
    mount /dev/vdb /mnt/scratch 2>/dev/null || true
    STATUS=1
    {
        echo "=== hackbento docker-base build start ==="
        if start_dockerd build; then
            # バンドル展開: /mnt/scratch/app -> /opt/app, images.tar を load, overlay を / に重ねる
            if [ -d /mnt/scratch/app ]; then
                rm -rf /opt/app; mkdir -p /opt/app
                cp -a /mnt/scratch/app/. /opt/app/
            fi
            if [ -f /mnt/scratch/images.tar ]; then
                docker load -i /mnt/scratch/images.tar
            fi
            if [ -d /mnt/scratch/overlay ]; then
                cp -a /mnt/scratch/overlay/. /
            fi
            # compose 設定を正規化して結果ディスクへ書き出す（ホスト側で検査する）
            PROJ="$(cat /mnt/scratch/project_name 2>/dev/null || echo app)"
            if [ -f /opt/app/compose.yml ] || [ -f /opt/app/compose.yaml ] || [ -f /opt/app/docker-compose.yml ] || [ -f /opt/app/docker-compose.yaml ]; then
                docker compose --project-directory /opt/app -p "$PROJ" config --format json > /mnt/scratch/compose.json 2>/mnt/scratch/compose.err && STATUS=0 || STATUS=2
                docker compose --project-directory /opt/app -p "$PROJ" config --images > /mnt/scratch/images.list 2>/dev/null || true
            else
                echo "compose ファイルが /opt/app に見つかりません" > /mnt/scratch/compose.err
                STATUS=3
            fi
            docker images --format '{{.Repository}}:{{.Tag}}' > /mnt/scratch/loaded.list 2>/dev/null || true
        else
            echo "dockerd 起動失敗" > /mnt/scratch/compose.err
            STATUS=4
        fi
        echo "$STATUS" > /mnt/scratch/status
        echo "=== build end status=$STATUS ==="
    } >> /mnt/scratch/build.log 2>&1
    # 診断用に dockerd のログを結果ディスクへ退避する
    cp /var/log/dockerd.log /mnt/scratch/dockerd.log 2>/dev/null || true
    sync
    umount /mnt/scratch 2>/dev/null || true
    # Firecracker は ACPI/APM 等のハードウェア電源管理を仮想化していないため、
    # poweroff/reboot コマンドが存在する環境ではカーネルを HALT 状態に落とすだけで
    # Firecracker プロセス自体が終了しないことがある。SysRq 経由の電源断
    # （kernel_power_off() を直接呼ぶ）を使う。sysrq は非同期にシャットダウンを
    # 開始するだけなので、直後に別の電源管理コマンド（poweroff/reboot 等）を
    # 実行するとシーケンスに割り込んで却ってハングすることを実機で確認したため、
    # sysrq 発行後は何も実行せずシェルを終了させる。
    echo o > /proc/sysrq-trigger
else
    # ===== run モード: 本番起動。IP はカーネル引数で設定済み =====
    printf '__RESOLV_CONF__' > /etc/resolv.conf
    chmod 644 /etc/resolv.conf
    hostname hackbento 2>/dev/null || true
    grep -q hackbento /etc/hosts 2>/dev/null || echo "127.0.0.1 localhost hackbento" >> /etc/hosts
    if start_dockerd; then
        PROJ="$(cat /opt/app/.hb_project 2>/dev/null || echo app)"
        docker compose --project-directory /opt/app -p "$PROJ" up -d --pull never >/var/log/compose.log 2>&1 || true
    fi
    echo "=== HackBento docker-base VM 起動完了 ==="
    while true; do sleep 3600; done
fi
"""


def _build_docker_base_init_script() -> str:
    nameservers = [ns.strip() for ns in settings.FC_GUEST_DNS.split(",") if ns.strip()]
    resolv_conf = "\\n".join(f"nameserver {ns}" for ns in nameservers) + "\\n"
    # init テンプレートには docker の {{.Repository}} 等が含まれ str.format と相性が悪いため、
    # プレースホルダ置換で差し込む。
    return _DOCKER_BASE_INIT_TEMPLATE.replace("__RESOLV_CONF__", resolv_conf)


# docker-base rootfs を Debian コンテナ内で構築するスクリプト（docker export で取り出す）。
# __INIT_SCRIPT__ に _build_docker_base_init_script() を差し込む（置換方式）。
_DOCKER_BASE_BUILD_SCRIPT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl iproute2 iptables kmod e2fsprogs
# Docker 公式リポジトリ
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin
# ゲストカーネル(6.1)に nftables が無いため iptables を legacy に固定
update-alternatives --set iptables /usr/sbin/iptables-legacy || true
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy || true
# dockerd を overlay2 + containerd image store 無効に固定
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'JSON_EOF'
{"storage-driver":"overlay2","features":{"containerd-snapshotter":false}}
JSON_EOF
# ビルド時の apt キャッシュ等を削って軽量化
apt-get clean
rm -rf /var/lib/apt/lists/*
# 自作 init を配置
cat > /tmp/hackbento-init << 'INIT_EOF'
__INIT_SCRIPT__
INIT_EOF
chmod +x /tmp/hackbento-init
rm -f /sbin/init
mv /tmp/hackbento-init /sbin/init
tar -cf /rootfs.tar -C / --exclude=./proc --exclude=./sys --exclude=./dev --exclude=./rootfs.tar .
"""


async def build_docker_base_rootfs_at(dest_path: str, base_image: str = DOCKER_BASE_DEBIAN_IMAGE) -> None:
    """docker-base rootfs（Debian 系 + Docker + compose）を dest_path に構築する。

    base_image は apt 系・docker-ce 公式リポジトリに対応したイメージである必要がある
    （_DOCKER_BASE_BUILD_SCRIPT が apt-get/docker-ce 公式リポジトリ前提のため、
    現時点では実質 Debian 系のみ対応）。dest_path に既にファイルがあれば再構築しない。

    Debian コンテナ内で docker-ce 一式を導入し、2 モード init を強制的に埋め込んで
    docker export で取り出し、ext4 へ変換する。dockerd はこのビルドコンテナ内では
    起動しない（インストールのみ）。実際に dockerd が動くのはビルド用/本番用 VM の中。
    """
    if os.path.exists(dest_path):
        logger.info(f"docker-base rootfs は既に存在します: {dest_path}")
        return
    if not docker_available():
        raise RuntimeError(
            "docker-base rootfs の構築には Docker (docker.sock) が必要ですが、"
            "/var/run/docker.sock が見つかりません。資産ビルド時のみ docker.sock マウントを有効にしてください。"
        )
    logger.info(f"docker-base rootfs を構築中...（数分かかる場合があります）base_image={base_image}")

    tmpdir = tempfile.mkdtemp(prefix="hackbento-docker-base-")
    container_name = f"hackbento-docker-base-build-{os.path.basename(tmpdir)}"
    try:
        # daemon.json の { } を含むため str.format は使わず置換で init を差し込む
        # （init の build/run 2モードはサーバ側の約束事であり、ユーザ/管理者が選べる
        #  base_image の違いに関わらず常にこのスクリプトで強制的に埋め込む）
        build_script = _DOCKER_BASE_BUILD_SCRIPT.replace("__INIT_SCRIPT__", _build_docker_base_init_script())
        try:
            await _run(["docker", "rm", "-f", container_name])
        except Exception:
            pass
        await _run([
            "docker", "run", "--name", container_name,
            base_image,
            "bash", "-c", build_script,
        ], timeout=1800)

        tar_path = os.path.join(tmpdir, "rootfs.tar")
        # docker export でホスト側に直接ストリーミング（大きいためメモリに載せない）
        await _run_to_file(["docker", "export", container_name], tar_path, timeout=900)
        await convert_tar_to_ext4(tar_path, dest_path, size_mb=DOCKER_BASE_ROOTFS_SIZE_MB)
        logger.info(f"docker-base rootfs を保存しました: {dest_path}")
    finally:
        try:
            await _run(["docker", "rm", "-f", container_name])
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


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
