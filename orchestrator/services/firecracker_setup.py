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

# --- Attack Machine (Kali Linux + XFCE + TigerVNC) ---
ATTACK_ROOTFS_PATH = os.path.join(DEFAULTS_DIR, "attack-rootfs.ext4")
ATTACK_ROOTFS_SIZE_MB = 4096  # kali-linux-core + XFCE/VNC/SSH の最小構成（convert_tar_to_ext4が実サイズに応じて自動拡張する）
KALI_DOCKER_IMAGE = "kalilinux/kali-rolling"

# Attack Machine 用 rootfs (Kali) のバックグラウンド構築状況。
# routers/attack_machine.py がこれを参照し、構築中は /api/attack-machine/ で
# 503 ではなく「準備中」状態を返す。
attack_rootfs_build_status = "idle"  # idle | building | ready | failed
attack_rootfs_build_error: str | None = None

ATTACK_VNC_PORT = 5901
ATTACK_VNC_DISPLAY = ":1"

ATTACK_MACHINE_SLUG = "attack-machine"
ATTACK_MACHINE_NAME = "Attack Machine"

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


_ATTACK_INIT_SCRIPT_TEMPLATE = """#!/bin/sh
sleep 1
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true
ip link set lo up 2>/dev/null || true
printf '{resolv_conf}' > /etc/resolv.conf
su - hackbento -c "vncserver {vnc_display} -geometry 1280x800 -depth 24 -localhost no" &
echo "=== HackBento Attack Machine 起動完了 ==="
wait
"""


def _build_attack_init_script() -> str:
    nameservers = [ns.strip() for ns in settings.FC_GUEST_DNS.split(",") if ns.strip()]
    resolv_conf = "\\n".join(f"nameserver {ns}" for ns in nameservers) + "\\n"
    return _ATTACK_INIT_SCRIPT_TEMPLATE.format(resolv_conf=resolv_conf, vnc_display=ATTACK_VNC_DISPLAY)


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


async def _run_to_file(cmd: list[str], dest_path: str, timeout: int = 600) -> None:
    """cmd の標準出力をメモリに溜め込まず dest_path へストリーミングで書き出す。

    大きな tar の `docker export` などをメモリに載せないために使用する。
    """
    with open(dest_path, "wb") as f:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=f,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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


_ATTACK_ROOTFS_BUILD_SCRIPT = """
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    kali-desktop-xfce xfce4-terminal tigervnc-standalone-server tigervnc-tools \
    openssh-client sudo bash dbus-x11 \
    kali-linux-core \
    iputils-ping nmap hydra enum4linux john gobuster dirb wireshark wordlists \
    locales fonts-noto-cjk fonts-noto-cjk-extra firefox-esr-l10n-ja \
    fcitx5 fcitx5-mozc fcitx5-config-qt \
    fcitx5-frontend-gtk2 fcitx5-frontend-gtk3 fcitx5-frontend-qt5

# 日本語ロケール生成 + タイムゾーン設定（ターミナル・ブラウザの表示言語と時刻表示に反映される）
sed -i "s/^# *ja_JP.UTF-8 UTF-8/ja_JP.UTF-8 UTF-8/" /etc/locale.gen
locale-gen
update-locale LANG=ja_JP.UTF-8 LANGUAGE=ja_JP:ja
ln -sf /usr/share/zoneinfo/Asia/Tokyo /etc/localtime
echo "Asia/Tokyo" > /etc/timezone

# キーボードレイアウトを日本語配列(JP106)に設定
cat > /etc/default/keyboard << 'KEYBOARD_EOF'
XKBMODEL="pc105"
XKBLAYOUT="jp"
XKBVARIANT=""
XKBOPTIONS=""
KEYBOARD_EOF

# wordlists: rockyou.txt.gz を展開して使えるようにする
if [ -f /usr/share/wordlists/rockyou.txt.gz ]; then
    gunzip -f /usr/share/wordlists/rockyou.txt.gz
fi

# iputils-ping / nmap は postinst で setcap/setuid が行われない（あるいは
# tar/ext4変換でfile capabilityのxattrが失われる）ため、一般ユーザが実行すると
# SOCK_RAW作成に失敗する(cap_net_raw不足)。tar/ext4変換でも確実に残る
# setuidビットを付与する。
chmod u+s /usr/bin/ping
chmod u+s /usr/lib/nmap/nmap

echo "root:hackbento" | chpasswd
useradd -m -s /bin/bash hackbento
echo "hackbento:hackbento" | chpasswd
# wiresharkパッケージはnoninteractiveインストールではwiresharkグループのpostinst
# 設定（dumpcapを非rootで実行可能にする）がスキップされ、グループ自体が
# 作成されないため、明示的に作成してhackbentoユーザを追加する。
getent group wireshark >/dev/null || groupadd wireshark
usermod -aG sudo,wireshark hackbento
if [ -x /usr/bin/dumpcap ]; then
    chgrp wireshark /usr/bin/dumpcap
    chmod 4750 /usr/bin/dumpcap
fi
echo "%sudo ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/sudo-nopasswd

# hackbento ユーザのデフォルト言語・タイムゾーンを日本語環境にする
# GTK_IM_MODULE/QT_IM_MODULE/XMODIFIERS は fcitx5 (日本語入力) を
# GTK/Qtアプリ(xfce4-terminalやFirefox等)から利用可能にするための設定。
cat >> /home/hackbento/.bashrc << 'BASHRC_EOF'
export LANG=ja_JP.UTF-8
export LANGUAGE=ja_JP:ja
export TZ=Asia/Tokyo
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
BASHRC_EOF
cat > /etc/profile.d/hackbento-locale.sh << 'PROFILE_EOF'
export LANG=ja_JP.UTF-8
export LANGUAGE=ja_JP:ja
export TZ=Asia/Tokyo
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
PROFILE_EOF

# fcitx5: デフォルトの入力メソッドをMozc(日本語)+キーボード(日本語配列)に設定し、
# Ctrl+Spaceで日本語入力のオン/オフを切り替えられるようにする。
mkdir -p /home/hackbento/.config/fcitx5
cat > /home/hackbento/.config/fcitx5/profile << 'FCITX_PROFILE_EOF'
[Groups/0]
Name=Default
Default Layout=jp
DefaultIM=mozc

[Groups/0/Items/0]
Name=keyboard-jp
Layout=

[Groups/0/Items/1]
Name=mozc
Layout=

[GroupOrder]
0=Default
FCITX_PROFILE_EOF
mkdir -p /home/hackbento/.config/fcitx5/conf
cat > /home/hackbento/.config/fcitx5/config << 'FCITX_CONFIG_EOF'
[Hotkey]
TriggerKeys=Control+space
EnumerateWithTriggerKeys=True
FCITX_CONFIG_EOF

# XFCEのキーボードレイアウト設定(xfconf)も日本語配列に設定する。
# /etc/default/keyboardはコンソール/Xサーバ起動時のデフォルトに反映されるが、
# XFCEセッションはxfce4-keyboard-settingsのxfconf設定を優先するため、
# 両方を設定しておく。
mkdir -p /home/hackbento/.config/xfce4/xfconf/xfce-perchannel-xml
cat > /home/hackbento/.config/xfce4/xfconf/xfce-perchannel-xml/keyboard-layout.xml << 'XKBLAYOUT_EOF'
<?xml version="1.0" encoding="UTF-8"?>

<channel name="keyboard-layout" version="1.0">
  <property name="Default" type="empty">
    <property name="XkbDisable" type="bool" value="false"/>
    <property name="XkbLayout" type="string" value="jp"/>
    <property name="XkbModel" type="string" value="pc105"/>
    <property name="XkbVariant" type="string" value=""/>
  </property>
</channel>
XKBLAYOUT_EOF

# xfce4-terminal: ウィンドウサイズ変更時に行折り返しが効かない問題への対策。
# デフォルトでは端末の列数(COLUMNS)がリサイズ通知と同期しないことがあるため、
# 起動時に内部ジオメトリを明示し、フォントもCJK対応の等幅フォントに固定する。
mkdir -p /home/hackbento/.config/xfce4/terminal
cat > /home/hackbento/.config/xfce4/terminal/terminalrc << 'TERMINALRC_EOF'
[Configuration]
FontName=Noto Sans Mono CJK JP 11
MiscAlwaysShowTabs=FALSE
MiscBell=FALSE
MiscBordersDefault=TRUE
MiscCursorBlinks=FALSE
MiscCursorShape=TERMINAL_CURSOR_SHAPE_BLOCK
MiscDefaultGeometry=120x32
MiscInheritGeometry=FALSE
MiscMenubarDefault=TRUE
MiscMouseAutohide=FALSE
MiscToolbarDefault=FALSE
MiscConfirmClose=TRUE
MiscCycleTabs=TRUE
MiscTabCloseButtons=TRUE
MiscTabCloseMiddleClick=TRUE
MiscTabPosition=GTK_POS_TOP
MiscHighlightUrls=TRUE
MiscMiddleClickOpensUri=FALSE
MiscCopyOnSelect=FALSE
MiscShowRelaunchDialog=TRUE
MiscRewrapOnResize=TRUE
MiscUseShiftArrowsToScroll=FALSE
MiscSlimTabs=FALSE
MiscNewTabAdjacent=FALSE
MiscSearchDialogOpacity=100
MiscShowUnsafePasteDialog=TRUE
ScrollingUnlimited=TRUE
TERMINALRC_EOF
chown -R hackbento:hackbento /home/hackbento/.config

# TigerVNC: パスワード設定 + XFCE セッション起動設定
mkdir -p /home/hackbento/.vnc
echo "hackbento" | tigervncpasswd -f > /home/hackbento/.vnc/passwd
chmod 600 /home/hackbento/.vnc/passwd
cat > /home/hackbento/.vnc/xstartup << 'XSTART_EOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export LANG=ja_JP.UTF-8
export LANGUAGE=ja_JP:ja
export TZ=Asia/Tokyo
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
setxkbmap jp 2>/dev/null || true
fcitx5 -d --replace
exec startxfce4
XSTART_EOF
chmod +x /home/hackbento/.vnc/xstartup
chown -R hackbento:hackbento /home/hackbento/.vnc

cat > /tmp/hackbento-init << 'INIT_EOF'
{init_script}
INIT_EOF
chmod +x /tmp/hackbento-init
rm -f /sbin/init
mv /tmp/hackbento-init /sbin/init
"""


async def _build_attack_rootfs() -> None:
    global attack_rootfs_build_status, attack_rootfs_build_error

    if os.path.exists(ATTACK_ROOTFS_PATH):
        logger.info(f"Attack Machine 用 rootfs は既に存在します: {ATTACK_ROOTFS_PATH}")
        attack_rootfs_build_status = "ready"
        return
    logger.info("Attack Machine 用 rootfs (Kali) を構築中...")
    attack_rootfs_build_status = "building"
    attack_rootfs_build_error = None

    tmpdir = tempfile.mkdtemp(prefix="hackbento-attack-rootfs-")
    container_name = f"hackbento-attack-rootfs-build-{os.path.basename(tmpdir)}"
    try:
        build_script = _ATTACK_ROOTFS_BUILD_SCRIPT.format(init_script=_build_attack_init_script())
        await _run(["docker", "rm", "-f", container_name])  # 残骸があれば除去（失敗は無視）
    except Exception:
        pass
    try:
        await _run([
            "docker", "run", "--name", container_name,
            KALI_DOCKER_IMAGE,
            "sh", "-c", build_script,
        ], timeout=7200)

        # コンテナ内で tar を作ると書き込みレイヤーにファイルシステム全体が二重に乗り
        # ディスクを圧迫するため、`docker export` でホスト側に直接ストリーミング出力する。
        tar_path = os.path.join(tmpdir, "attack-rootfs.tar")
        await _run_to_file(["docker", "export", container_name], tar_path, timeout=600)

        await convert_tar_to_ext4(tar_path, ATTACK_ROOTFS_PATH, size_mb=ATTACK_ROOTFS_SIZE_MB)
        logger.info(f"Attack Machine 用 rootfs を保存しました: {ATTACK_ROOTFS_PATH}")
        attack_rootfs_build_status = "ready"
    except Exception as e:
        attack_rootfs_build_status = "failed"
        attack_rootfs_build_error = str(e)
        raise
    finally:
        try:
            await _run(["docker", "rm", "-f", container_name])
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


async def ensure_attack_machine_assets() -> None:
    """Attack Machine 用 rootfs を準備し、Image テンプレートを DB に登録する。"""
    global attack_rootfs_build_status, attack_rootfs_build_error

    if settings.VM_BACKEND != "bridge":
        return
    if not settings.ATTACK_MACHINE_ENABLED:
        return
    if not os.path.exists("/dev/kvm"):
        logger.warning("/dev/kvm が見つからないため、Attack Machine アセットの準備をスキップします。")
        attack_rootfs_build_status = "failed"
        attack_rootfs_build_error = "/dev/kvm が見つかりません"
        return
    try:
        os.makedirs(DEFAULTS_DIR, exist_ok=True)
        await _download_default_kernel()  # カーネルは既存デフォルトを共有
        await _build_attack_rootfs()
        await _register_attack_machine_image()
    except Exception as e:
        attack_rootfs_build_status = "failed"
        attack_rootfs_build_error = str(e)
        logger.error(f"Attack Machine アセットの準備に失敗しました: {e}")


async def _register_attack_machine_image() -> None:
    """システム提供の Attack Machine テンプレート（Image）を作成・更新する。"""
    from database import AsyncSessionLocal
    from models import Image, Backend, Visibility
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Image).where(Image.is_attack_machine == True)
        )
        image = result.scalar_one_or_none()
        if image is None:
            image = Image(
                owner_id=None,
                created_by=None,
                name=ATTACK_MACHINE_NAME,
                slug=ATTACK_MACHINE_SLUG,
                description="Kali Linux ベースの攻撃用 VM（GUI / VNC）",
                backend=Backend.bridge,
                kernel_path=DEFAULT_KERNEL_PATH,
                rootfs_path=ATTACK_ROOTFS_PATH,
                cpu_limit=settings.ATTACK_MACHINE_CPU_LIMIT,
                memory_limit_mb=settings.ATTACK_MACHINE_MEMORY_LIMIT_MB,
                timeout_minutes=settings.ATTACK_MACHINE_TIMEOUT_MINUTES,
                visibility=Visibility.private,
                is_active=True,
                is_attack_machine=True,
            )
            db.add(image)
        else:
            image.kernel_path = DEFAULT_KERNEL_PATH
            image.rootfs_path = ATTACK_ROOTFS_PATH
            image.is_active = True
        await db.commit()


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
