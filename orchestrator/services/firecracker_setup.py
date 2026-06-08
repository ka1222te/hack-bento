import asyncio
import logging
import os
import tempfile
import shutil

from config import settings

logger = logging.getLogger(__name__)

VM_DIR = "/data/images/vm"
DEFAULTS_DIR = os.path.join(VM_DIR, "defaults")
DEFAULT_KERNEL_PATH = os.path.join(DEFAULTS_DIR, "vmlinux")
DEFAULT_ROOTFS_PATH = os.path.join(DEFAULTS_DIR, "rootfs.ext4")

KERNEL_URL = "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128"
ROOTFS_SIZE_MB = 512

_INIT_SCRIPT = """#!/bin/sh
sleep 1
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true
ip link set lo up 2>/dev/null || true
/usr/sbin/sshd -D &
echo "=== HackBento Firecracker VM 起動完了 ==="
wait
"""

_ROOTFS_BUILD_SCRIPT = """
set -e
apk add --no-cache openssh-server bash sudo
echo "root:hackbento" | chpasswd
adduser -D -s /bin/bash hackbento
echo "hackbento:hackbento" | chpasswd
addgroup hackbento wheel
echo "%wheel ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/wheel
sed -i "s/#PermitRootLogin prohibit-password/PermitRootLogin yes/" /etc/ssh/sshd_config
sed -i "s/#PasswordAuthentication yes/PasswordAuthentication yes/" /etc/ssh/sshd_config
echo "PermitRootLogin yes" >> /etc/ssh/sshd_config
ssh-keygen -A
cat > /tmp/hackbento-init << 'INIT_EOF'
{init_script}
INIT_EOF
chmod +x /tmp/hackbento-init
rm -f /sbin/init
mv /tmp/hackbento-init /sbin/init
tar -cf /rootfs.tar -C / --exclude=./proc --exclude=./sys --exclude=./dev --exclude=./rootfs.tar .
"""


async def _run(cmd: list[str], timeout: int = 600) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {stderr.decode().strip()}")


async def _download_default_kernel() -> None:
    if os.path.exists(DEFAULT_KERNEL_PATH):
        logger.info(f"デフォルトゲストカーネルは既に存在します: {DEFAULT_KERNEL_PATH}")
        return
    logger.info("デフォルトゲストカーネルをダウンロード中...")
    tmp_path = DEFAULT_KERNEL_PATH + ".tmp"
    await _run(["curl", "-fsSL", KERNEL_URL, "-o", tmp_path])
    os.replace(tmp_path, DEFAULT_KERNEL_PATH)
    logger.info(f"デフォルトゲストカーネルを保存しました: {DEFAULT_KERNEL_PATH}")


async def _build_default_rootfs() -> None:
    if os.path.exists(DEFAULT_ROOTFS_PATH):
        logger.info(f"デフォルト rootfs は既に存在します: {DEFAULT_ROOTFS_PATH}")
        return
    logger.info("デフォルト rootfs を構築中...")

    # オーケストレータは Docker-in-Docker (ホストの docker.sock を共有) で動作しており、
    # ホスト側 dockerd から見たパスとコンテナ内パスが一致しないため bind mount は使えない。
    # コンテナ内でビルドしたファイルは `docker cp` で取り出す。
    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-")
    container_name = f"hackbento-rootfs-build-{os.path.basename(tmpdir)}"
    try:
        build_script = _ROOTFS_BUILD_SCRIPT.format(init_script=_INIT_SCRIPT)
        await _run(["docker", "rm", "-f", container_name])  # 残骸があれば除去（失敗は無視）
    except Exception:
        pass
    try:
        await _run([
            "docker", "run", "--name", container_name,
            "alpine:3.19",
            "sh", "-c", build_script,
        ], timeout=900)

        tar_path = os.path.join(tmpdir, "rootfs.tar")
        await _run(["docker", "cp", f"{container_name}:/rootfs.tar", tar_path])

        rootfs_root = os.path.join(tmpdir, "rootfs")
        os.makedirs(rootfs_root, exist_ok=True)
        await _run(["tar", "-xf", tar_path, "-C", rootfs_root])
        for d in ("proc", "sys", "dev", "tmp"):
            os.makedirs(os.path.join(rootfs_root, d), exist_ok=True)

        tmp_rootfs = DEFAULT_ROOTFS_PATH + ".tmp"
        if os.path.exists(tmp_rootfs):
            os.remove(tmp_rootfs)

        # mount/loop デバイスを使わずに ext4 イメージを直接構築する
        await _run(["dd", "if=/dev/zero", f"of={tmp_rootfs}", "bs=1M", f"count={ROOTFS_SIZE_MB}"])
        await _run(["mkfs.ext4", "-F", "-d", rootfs_root, tmp_rootfs])
        os.replace(tmp_rootfs, DEFAULT_ROOTFS_PATH)
        logger.info(f"デフォルト rootfs を保存しました: {DEFAULT_ROOTFS_PATH}")
    finally:
        try:
            await _run(["docker", "rm", "-f", container_name])
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


async def ensure_default_vm_assets() -> None:
    """bridge バックエンド用のデフォルトゲストカーネル・rootfs を準備する（無ければ生成）。

    プロジェクト登録時に「デフォルトのカーネル/rootfsを使う」を選択した場合に
    参照される共通ファイル。失敗してもアプリ起動はブロックしない。
    """
    if settings.VM_BACKEND != "bridge":
        return
    if not os.path.exists("/dev/kvm"):
        logger.warning("/dev/kvm が見つからないため、デフォルトVMアセットの準備をスキップします。")
        return

    try:
        os.makedirs(DEFAULTS_DIR, exist_ok=True)
        await _download_default_kernel()
        await _build_default_rootfs()
        await _register_builtin_asset_records()
    except Exception as e:
        logger.error(f"デフォルトVMアセットの準備に失敗しました（プロジェクト作成時に手動アップロードで代替できます）: {e}")


async def _register_builtin_asset_records() -> None:
    """組み込みデフォルトカーネル/rootfs を選択肢として使えるよう、DB に資産レコードを登録する（無ければ作成）。"""
    from database import AsyncSessionLocal
    from models import DefaultKernelAsset, DefaultRootfsAsset
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        if os.path.exists(DEFAULT_KERNEL_PATH):
            existing = await db.execute(
                select(DefaultKernelAsset).where(DefaultKernelAsset.file_path == DEFAULT_KERNEL_PATH)
            )
            if not existing.scalar_one_or_none():
                db.add(DefaultKernelAsset(label="組み込みデフォルト (vmlinux-6.1.128)", file_path=DEFAULT_KERNEL_PATH))
                logger.info("組み込みデフォルトカーネルを資産として登録しました")

        if os.path.exists(DEFAULT_ROOTFS_PATH):
            existing = await db.execute(
                select(DefaultRootfsAsset).where(DefaultRootfsAsset.file_path == DEFAULT_ROOTFS_PATH)
            )
            if not existing.scalar_one_or_none():
                db.add(DefaultRootfsAsset(label="組み込みデフォルト (Alpine + sshd)", file_path=DEFAULT_ROOTFS_PATH))
                logger.info("組み込みデフォルト rootfs を資産として登録しました")

        await db.commit()
