"""デフォルトのゲストカーネル・デフォルト rootfs・rootfs 変換専用ベースの準備。

services/firecracker_setup.py から分離した責務のひとつ。
通常のOSイメージプロジェクト作成時に「デフォルト資産を使う」を選んだ場合に
参照される最小構成（Alpine + sshd）と、rootfs tar→ext4 変換（rootfs_tar.py）が
内部で使う変換専用ベースの、ビルド処理をまとめる。
"""
import logging
import os
import shutil
import tempfile

from config import settings
from services.rootfs_tar import (
    convert_tar_to_ext4,
    convert_tar_to_ext4_host_unsafe,
    docker_available,
    run as _run,
)
from services.vm_assets import (
    DEFAULT_KERNEL_PATH,
    DEFAULT_ROOTFS_PATH,
    DEFAULTS_DIR,
    KERNEL_URL,
    ROOTFS_CONVERTER_BASE_PATH,
    ROOTFS_CONVERTER_BASE_SIZE_MB,
)

logger = logging.getLogger(__name__)

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
# /etc/resolv.conf が 0600 のままだと一般ユーザでの getaddrinfo が
# DNSサーバ未設定とみなされ、サーバが正常応答してもNOTFOUNDになる。
chmod 644 /etc/resolv.conf
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


# rootfs 変換専用ベースの /sbin/init。
# 常にビルドモードのみで動く（本番起動には使わないため run モードは無い）。
#
# 4 本のディスクを使う:
#   1 本目（/dev/vda, root）  : この変換専用ベース自身（tar コマンドのみの Alpine）
#   2 本目（/dev/vdb, scratch）: 空の ext4。*ルート直下に直接* tar を展開し、
#                                ディスクそのものが最終的な rootfs として host へ回収される
#   3 本目（/dev/vdc, input）  : 読み取り専用。アップロードされた rootfs tar の生バイト列
#                                （ファイルシステムではなく tar ファイルそのものを dd した
#                                ブロックデバイス）
#   4 本目（/dev/vdd, status） : 書き込み可能な小さな ext4。status/error/convert.log/
#                                extracted_bytes を書く。scratch（2本目）は丸ごと成果物として
#                                回収されるため、おまけ情報をそこに混在させないために分離する。
# scratch のマウントポイント自体を展開先にすることで、サブディレクトリへの展開 → 再抽出が
# 不要になり、1 本目（変換用ベース自身のファイル）も結果に混在しない。展開はこの VM の中で
# 完結し、VM にはネットワークも他ディスクへの経路も無いため、tar の内容（symlink 等）が
# ホストへ影響することはない。
_ROOTFS_CONVERTER_INIT_SCRIPT = r"""#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /mnt/out
mount /dev/vdb /mnt/out 2>/dev/null || true
mkdir -p /mnt/status
mount /dev/vdd /mnt/status 2>/dev/null || true

STATUS=1
{
    echo "=== hackbento rootfs-convert start ==="
    if [ -e /dev/vdc ]; then
        if tar -xf /dev/vdc -C /mnt/out; then
            mkdir -p /mnt/out/proc /mnt/out/sys /mnt/out/dev /mnt/out/tmp
            du -sb /mnt/out 2>/dev/null | cut -f1 > /mnt/status/extracted_bytes
            STATUS=0
        else
            echo "tar展開に失敗しました" > /mnt/status/error
            STATUS=2
        fi
    else
        echo "/dev/vdc (入力ディスク) が見つかりません" > /mnt/status/error
        STATUS=3
    fi
    echo "$STATUS" > /mnt/status/status
    echo "=== rootfs-convert end status=$STATUS ==="
} 2>&1 | tee /mnt/status/convert.log >/dev/null

sync
umount /mnt/out 2>/dev/null || true
umount /mnt/status 2>/dev/null || true
# Firecracker は ACPI/APM 等のハードウェア電源管理を仮想化していないため、busybox の
# poweroff/reboot は（コマンドが存在する環境では）カーネルを HALT 状態に落とすだけで
# Firecracker プロセス自体は終了しない（ハングし、呼び出し側のハードタイムアウトまで
# 待たされる）。SysRq 経由の電源断（echo o > /proc/sysrq-trigger）は
# kernel_power_off() を直接呼び、Firecracker がこれを確実に検知して正常終了する
# （実機検証で確認済み）。
# 重要: sysrq は電源断シーケンスを*非同期に*開始するだけで、echo コマンド自体は
# すぐに返ってくる。続けて poweroff/reboot 等のフォールバックを実行すると、
# シャットダウン処理の最中に別の電源管理コマンドが割り込み、却って Firecracker が
# 終了しないままハングすることを実機で確認した。そのため sysrq 発行後は何も実行せず、
# シェルをそのまま終了させる（結果的に init(PID1) が終了し、カーネルパニック経由でも
# Firecracker の終了が検知される。どちらの経路でも確実に終了することを実機確認済み）。
echo o > /proc/sysrq-trigger
"""


# rootfs 変換専用ベースを Alpine コンテナ内で構築するスクリプト（docker export で取り出す）。
# tar コマンドのみを含む最小構成（apk 等の追加パッケージは不要）。
_ROOTFS_CONVERTER_BASE_BUILD_SCRIPT = """
set -e
cat > /tmp/hackbento-init << 'INIT_EOF'
{init_script}
INIT_EOF
chmod +x /tmp/hackbento-init
rm -f /sbin/init
mv /tmp/hackbento-init /sbin/init
tar -cf /rootfs.tar -C / --exclude=./proc --exclude=./sys --exclude=./dev --exclude=./rootfs.tar .
"""


async def download_default_kernel() -> None:
    if os.path.exists(DEFAULT_KERNEL_PATH):
        logger.info(f"デフォルトゲストカーネルは既に存在します: {DEFAULT_KERNEL_PATH}")
        return
    logger.info("デフォルトゲストカーネルをダウンロード中...")
    tmp_path = DEFAULT_KERNEL_PATH + ".tmp"
    await _run(["curl", "-fsSL", KERNEL_URL, "-o", tmp_path])
    os.replace(tmp_path, DEFAULT_KERNEL_PATH)
    logger.info(f"デフォルトゲストカーネルを保存しました: {DEFAULT_KERNEL_PATH}")


async def build_default_rootfs() -> None:
    if os.path.exists(DEFAULT_ROOTFS_PATH):
        logger.info(f"デフォルト rootfs は既に存在します: {DEFAULT_ROOTFS_PATH}")
        return
    if not docker_available():
        raise RuntimeError(
            "デフォルト rootfs の構築には Docker (docker.sock) が必要ですが、"
            "/var/run/docker.sock が見つかりません。資産ビルド時のみ docker-compose.yml の "
            "docker.sock マウントを有効にするか、rootfs を手動でアップロードしてください。"
        )
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


async def build_rootfs_converter_base() -> None:
    """rootfs 変換専用ベース（tar コマンドのみの最小 Alpine）を構築する。

    この資産自体はビルド時に docker.sock（信頼できる管理者操作）を使って作るが、
    出来上がったベースは「ユーザがアップロードする信用できない rootfs tar」を
    ビルド用VMの中で展開するための土台として使う（services.rootfs_tar から利用）。
    """
    if os.path.exists(ROOTFS_CONVERTER_BASE_PATH):
        logger.info(f"rootfs 変換専用ベースは既に存在します: {ROOTFS_CONVERTER_BASE_PATH}")
        return
    if not docker_available():
        raise RuntimeError(
            "rootfs 変換専用ベースの構築には Docker (docker.sock) が必要ですが、"
            "/var/run/docker.sock が見つかりません。資産ビルド時のみ docker.sock マウントを有効にしてください。"
        )
    logger.info("rootfs 変換専用ベースを構築中...")

    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-converter-")
    container_name = f"hackbento-rootfs-converter-build-{os.path.basename(tmpdir)}"
    try:
        build_script = _ROOTFS_CONVERTER_BASE_BUILD_SCRIPT.format(
            init_script=_ROOTFS_CONVERTER_INIT_SCRIPT
        )
        await _run(["docker", "rm", "-f", container_name])
    except Exception:
        pass
    try:
        await _run([
            "docker", "run", "--name", container_name,
            "alpine:3.19",
            "sh", "-c", build_script,
        ], timeout=300)

        tar_path = os.path.join(tmpdir, "rootfs.tar")
        await _run(["docker", "cp", f"{container_name}:/rootfs.tar", tar_path])

        # 変換専用ベース自身の初回ビルドは「ニワトリと卵」（まだベースが無い）のため、
        # この1箇所だけはホスト上展開版を使う。入力は HackBento 自身が docker.sock
        # 経由で生成した tar（内容を把握できる）であり、ユーザアップロードではない。
        await convert_tar_to_ext4_host_unsafe(tar_path, ROOTFS_CONVERTER_BASE_PATH, size_mb=ROOTFS_CONVERTER_BASE_SIZE_MB)
        logger.info(f"rootfs 変換専用ベースを保存しました: {ROOTFS_CONVERTER_BASE_PATH}")
    finally:
        try:
            await _run(["docker", "rm", "-f", container_name])
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


async def register_builtin_asset_records() -> None:
    """組み込みデフォルトカーネル/rootfs を選択肢として使えるよう、DB に資産レコードを登録する（無ければ作成）。"""
    from database import AsyncSessionLocal
    from models import DefaultKernelAsset, DefaultRootfsAsset
    from sqlalchemy import select
    from services.vm_assets import DEFAULT_KERNEL_LABEL, DEFAULT_ROOTFS_LABEL

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
