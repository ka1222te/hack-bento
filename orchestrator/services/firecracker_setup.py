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

# 資産一覧での表示ラベル。バージョン番号は KERNEL_URL のファイル名から取得し、
# rootfs はログイン情報を明示してすぐに使えることが伝わるようにする
# （認証情報は _ROOTFS_BUILD_SCRIPT で設定している値と一致させること）。
DEFAULT_KERNEL_LABEL = os.path.basename(KERNEL_URL)
DEFAULT_ROOTFS_LABEL = "Alpine + sshd (user:hackbento, password:hackbento)"

_INIT_SCRIPT_TEMPLATE = """#!/bin/sh
sleep 1
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true
ip link set lo up 2>/dev/null || true
printf '{resolv_conf}' > /etc/resolv.conf
/usr/sbin/sshd -D &
echo "=== HackBento Firecracker VM 起動完了 ==="
wait
"""


def _build_init_script() -> str:
    nameservers = [ns.strip() for ns in settings.FC_GUEST_DNS.split(",") if ns.strip()]
    resolv_conf = "\\n".join(f"nameserver {ns}" for ns in nameservers) + "\\n"
    return _INIT_SCRIPT_TEMPLATE.format(resolv_conf=resolv_conf)

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


async def convert_tar_to_ext4(tar_path: str, dest_path: str, size_mb: int = ROOTFS_SIZE_MB) -> None:
    """単純なファイルシステム tar を ext4 イメージへ変換し、dest_path へ os.replace する。

    mount/loop デバイスを使わず mkfs.ext4 -d で直接構築する（_build_default_rootfs と同じ方式）。
    イメージサイズは展開後のディレクトリ実サイズを元に自動計算し、固定値より大きい場合はそちらを使う。
    """
    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-convert-")
    try:
        rootfs_root = os.path.join(tmpdir, "rootfs")
        os.makedirs(rootfs_root, exist_ok=True)
        await _run(["tar", "-xf", tar_path, "-C", rootfs_root])
        for d in ("proc", "sys", "dev", "tmp"):
            os.makedirs(os.path.join(rootfs_root, d), exist_ok=True)

        # 展開後の実サイズを計測し、20% + 64MB のオーバーヘッドを加えたサイズを使う
        du_result = await asyncio.create_subprocess_exec(
            "du", "-sb", rootfs_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        du_stdout, _ = await du_result.communicate()
        actual_bytes = int(du_stdout.split()[0]) if du_stdout.strip() else 0
        auto_mb = int(actual_bytes * 1.2 / (1024 * 1024)) + 64
        image_mb = max(size_mb, auto_mb)

        tmp_image = dest_path + ".tmp"
        if os.path.exists(tmp_image):
            os.remove(tmp_image)
        await _run(["dd", "if=/dev/zero", f"of={tmp_image}", "bs=1M", f"count={image_mb}"])
        await _run(["mkfs.ext4", "-F", "-d", rootfs_root, tmp_image])
        os.replace(tmp_image, dest_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _detect_archive_kind(path: str) -> str:
    """アップロードされたファイルの種類を判定する。

    戻り値: "ext4"（変換不要・そのまま利用可能） / "tar"（単純なファイルシステムtar・変換対象）
            / "docker-save"（複数レイヤーのOCIアーカイブ・非対応）
    """
    import struct
    import tarfile

    try:
        with open(path, "rb") as f:
            f.seek(1024 + 56)
            magic = f.read(2)
        if struct.unpack("<H", magic)[0] == 0xEF53:
            return "ext4"
    except (OSError, struct.error):
        pass

    if tarfile.is_tarfile(path):
        try:
            with tarfile.open(path) as tf:
                names = {os.path.normpath(n).lstrip("./") for n in tf.getnames()}
        except tarfile.TarError:
            return "tar"
        if "manifest.json" in names and "repositories" in names:
            return "docker-save"
        return "tar"

    return "ext4"


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
        build_script = _ROOTFS_BUILD_SCRIPT.format(init_script=_build_init_script())
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

        await convert_tar_to_ext4(tar_path, DEFAULT_ROOTFS_PATH)
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
            asset = existing.scalar_one_or_none()
            if asset is None:
                db.add(DefaultKernelAsset(label=DEFAULT_KERNEL_LABEL, file_path=DEFAULT_KERNEL_PATH))
                logger.info("組み込みデフォルトカーネルを資産として登録しました")
            elif asset.label != DEFAULT_KERNEL_LABEL:
                # 過去のバージョンで登録されたラベルを最新の表示形式に揃える
                asset.label = DEFAULT_KERNEL_LABEL

        if os.path.exists(DEFAULT_ROOTFS_PATH):
            existing = await db.execute(
                select(DefaultRootfsAsset).where(DefaultRootfsAsset.file_path == DEFAULT_ROOTFS_PATH)
            )
            asset = existing.scalar_one_or_none()
            if asset is None:
                db.add(DefaultRootfsAsset(label=DEFAULT_ROOTFS_LABEL, file_path=DEFAULT_ROOTFS_PATH))
                logger.info("組み込みデフォルト rootfs を資産として登録しました")
            elif asset.label != DEFAULT_ROOTFS_LABEL:
                asset.label = DEFAULT_ROOTFS_LABEL

        await db.commit()
