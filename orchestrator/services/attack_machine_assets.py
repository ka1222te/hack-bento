"""Attack Machine（Kali Linux + XFCE + TigerVNC）の準備。

services/firecracker_setup.py から分離した責務のひとつ。Kali rootfs の
バックグラウンド構築と、起動用 Image テンプレートの DB 登録を行う。
"""
import logging
import os
import uuid

from config import settings
from services.rootfs_tar import convert_tar_to_ext4, run_builder_job
from services import vm_assets
from services.vm_assets import (
    ATTACK_MACHINE_NAME,
    ATTACK_MACHINE_SLUG,
    ATTACK_ROOTFS_PATH,
    ATTACK_ROOTFS_SIZE_MB,
    ATTACK_VNC_DISPLAY,
    DEFAULT_KERNEL_PATH,
    DEFAULTS_DIR,
    KALI_DOCKER_IMAGE,
)

logger = logging.getLogger(__name__)

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
# /etc/resolv.conf が 0600 のままだと一般ユーザ(hackbento)での getaddrinfo が
# DNSサーバ未設定とみなされ、サーバが正常応答してもNOTFOUNDになる。
chmod 644 /etc/resolv.conf
# hostname の自己解決に失敗すると vncserver が
# "Could not acquire fully qualified host name of this machine." で
# エラー終了し、init(PID1) が終了して kernel panic
# (Attempted to kill init!) になる。/etc/hosts に自身のホスト名を
# 登録して hostname コマンドを解決可能にしておく。
hostname hackbento 2>/dev/null || true
echo "127.0.0.1 localhost hackbento" >> /etc/hosts
# rootfs イメージのビルド時（apt-getのpostinst等でXサーバが一時起動した場合）に
# /tmp 配下へ X11 ソケット・ロックファイルが残っていると、vncserver が
# 「既に起動中」と誤認してエラー終了し、wait 対象が消えて init(PID1) が
# 終了 -> kernel panic (Attempted to kill init!) になる。起動時に必ず除去する。
rm -rf /tmp/.X11-unix /tmp/.X*-lock /tmp/.ICE-unix
# vncserver はXサーバプロセスをforkしてデーモン化した後、ラッパー自身は
# すぐにexit 0で終了するコマンドであり、バックグラウンドジョブとして
# `wait` し続ける対象にはならない。`&` + `wait` のままだと vncserver
# 終了時点で wait 対象が無くなり init(PID1) が終了 -> kernel panic
# (Attempted to kill init!) になるため、フォアグラウンドで一度だけ
# 実行し、その後 init 自身が無限に居座る。
su - hackbento -c "vncserver {vnc_display} -geometry 1280x800 -depth 24 -localhost no"
echo "=== HackBento Attack Machine 起動完了 ==="
while true; do sleep 3600; done
"""


def _build_attack_init_script() -> str:
    nameservers = [ns.strip() for ns in settings.FC_GUEST_DNS.split(",") if ns.strip()]
    resolv_conf = "\\n".join(f"nameserver {ns}" for ns in nameservers) + "\\n"
    return _ATTACK_INIT_SCRIPT_TEMPLATE.format(resolv_conf=resolv_conf, vnc_display=ATTACK_VNC_DISPLAY)


_ATTACK_ROOTFS_BUILD_SCRIPT = """
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    kali-desktop-xfce xfce4-terminal tigervnc-standalone-server tigervnc-tools \
    x11-xserver-utils \
    openssh-client sudo bash dbus-x11 \
    kali-linux-core \
    iputils-ping nmap hydra enum4linux john gobuster dirb wireshark wordlists \
    nano vim git curl wget unzip net-tools \
    python3 python3-pip python3-venv \
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

# XFCEの電源管理(xfce4-power-manager)・画面ロック(light-locker)を無効化し、
# 画面が常に表示されたまま(スリープ/ロックなし)になるようにする。
# DPMS/ブランク/サスペンド/ハイバネートを全てOFFにし、light-lockerは
# ロック無しで起動するようxstartup側でも対応する。
mkdir -p /home/hackbento/.config/xfce4/xfconf/xfce-perchannel-xml
cat > /home/hackbento/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml << 'POWERMGR_EOF'
<?xml version="1.0" encoding="UTF-8"?>

<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="dpms-enabled" type="bool" value="false"/>
    <property name="dpms-on-ac-sleep" type="uint" value="0"/>
    <property name="dpms-on-ac-off" type="uint" value="0"/>
    <property name="dpms-on-battery-sleep" type="uint" value="0"/>
    <property name="dpms-on-battery-off" type="uint" value="0"/>
    <property name="blank-on-ac" type="int" value="0"/>
    <property name="blank-on-battery" type="int" value="0"/>
    <property name="lock-screen-suspend-hibernate" type="bool" value="false"/>
    <property name="logind-handle-lid-switch" type="bool" value="false"/>
  </property>
</channel>
POWERMGR_EOF

# xfce4-screensaver(導入されている場合)もロック・スクリーンセーバーを無効化
cat > /home/hackbento/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml << 'SCREENSAVER_EOF'
<?xml version="1.0" encoding="UTF-8"?>

<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="idle-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
  <property name="lock" type="empty">
    <property name="enabled" type="bool" value="false"/>
  </property>
</channel>
SCREENSAVER_EOF

# xfce4-session: 自動ロック(LockCommand)・画面保存を無効化
cat > /home/hackbento/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml << 'SESSION_EOF'
<?xml version="1.0" encoding="UTF-8"?>

<channel name="xfce4-session" version="1.0">
  <property name="general" type="empty">
    <property name="LockCommand" type="string" value="true"/>
  </property>
</channel>
SESSION_EOF

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
# X11レベルでもDPMS(画面オフ)・スクリーンセーバーを無効化しておく。
# xfconf設定だけだとセッション開始タイミングによっては反映前に
# 画面オフ判定が走ることがあるため、X起動直後にも明示的にOFFにする。
xset s off
xset s noblank
xset -dpms
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


async def build_attack_rootfs() -> None:
    """Attack Machine 用 rootfs（Kali + XFCE + TigerVNC）を構築する。

    ビルド（Docker 操作）自体は docker-base-builder コンテナへ委譲する。
    数十分〜2時間程度かかることがあるため、ビルダー側のタイムアウトも長めに取る。
    """
    if os.path.exists(ATTACK_ROOTFS_PATH):
        logger.info(f"Attack Machine 用 rootfs は既に存在します: {ATTACK_ROOTFS_PATH}")
        vm_assets.attack_rootfs_build_status = "ready"
        return
    logger.info("Attack Machine 用 rootfs (Kali) を構築中...")
    vm_assets.attack_rootfs_build_status = "building"
    vm_assets.attack_rootfs_build_error = None

    build_script = _ATTACK_ROOTFS_BUILD_SCRIPT.format(init_script=_build_attack_init_script())
    job_id = uuid.uuid4().hex
    tar_filename = f"{job_id}.tar"
    tar_path = os.path.join(DEFAULTS_DIR, tar_filename)
    try:
        # nano/git/python3-pip等の追加パッケージがQt6系の依存を多数引き込み、
        # unpackフェーズだけで実測1時間46分かかるようになったため、configure・
        # tar化を含めた全体のタイムアウトを7200s(2時間)から14400s(4時間)に延長した。
        await run_builder_job(
            settings.DOCKER_BASE_BUILDER_URL,
            "/export-tar",
            {
                "job_id": job_id,
                "base_image": KALI_DOCKER_IMAGE,
                "build_script": build_script,
                "dest_filename": tar_filename,
                "method": "export",
                "build_timeout_s": 14400,
            },
            job_id,
            timeout_s=14400,
        )
        await convert_tar_to_ext4(tar_path, ATTACK_ROOTFS_PATH, size_mb=ATTACK_ROOTFS_SIZE_MB)
        logger.info(f"Attack Machine 用 rootfs を保存しました: {ATTACK_ROOTFS_PATH}")
        vm_assets.attack_rootfs_build_status = "ready"
    except Exception as e:
        vm_assets.attack_rootfs_build_status = "failed"
        vm_assets.attack_rootfs_build_error = str(e)
        raise
    finally:
        if os.path.exists(tar_path):
            try:
                os.remove(tar_path)
            except OSError:
                pass


async def register_attack_machine_image() -> None:
    """システム提供の Attack Machine テンプレート（Image）を作成・更新する。"""
    from database import AsyncSessionLocal
    from models import Image, Visibility
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
            # timeout_minutes（ATTACK_MACHINE_TIMEOUT_MINUTES）はコンテナ起動時に毎回
            # .env の値へ同期する（CPU/メモリ上限は起動中VMのcgroup設定と紐づくため
            # 初回作成時の値を維持する）。
            image.timeout_minutes = settings.ATTACK_MACHINE_TIMEOUT_MINUTES
        await db.commit()


async def ensure_attack_machine_assets() -> None:
    """Attack Machine 用 rootfs を準備し、Image テンプレートを DB に登録する。"""
    if not settings.ATTACK_MACHINE_ENABLED:
        return
    if not os.path.exists("/dev/kvm"):
        logger.warning("/dev/kvm が見つからないため、Attack Machine アセットの準備をスキップします。")
        vm_assets.attack_rootfs_build_status = "failed"
        vm_assets.attack_rootfs_build_error = "/dev/kvm が見つかりません"
        return
    try:
        os.makedirs(DEFAULTS_DIR, exist_ok=True)
        from services.default_assets import download_default_kernel
        await download_default_kernel()  # カーネルは既存デフォルトを共有
        await build_attack_rootfs()
        await register_attack_machine_image()
    except Exception as e:
        vm_assets.attack_rootfs_build_status = "failed"
        vm_assets.attack_rootfs_build_error = str(e)
        logger.error(f"Attack Machine アセットの準備に失敗しました: {e}")
