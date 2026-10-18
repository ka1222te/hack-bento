import asyncio
import logging
import os
import tarfile
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

# --- rootfs 変換専用ベース (OS イメージ rootfs tar → ext4 変換をビルド用VM内で行うための土台) ---
# アップロードされた信用できない tar をホスト上で展開せず、ネットワークなしの使い捨てVMの
# 中で展開することで、tar の内容（symlink traversal 等）がホストに影響し得ない構成にする。
# このベース自体は空の Alpine（tar コマンドのみ）で、本番運用には使わない。
ROOTFS_CONVERTER_BASE_PATH = os.path.join(DEFAULTS_DIR, "rootfs-converter-base.ext4")
ROOTFS_CONVERTER_BASE_SIZE_MB = 64  # tar コマンドが入った最小限の Alpine のみ
ROOTFS_CONVERTER_BASE_LABEL = "rootfs 変換用ベース（内部専用・選択肢には出さない）"

# --- docker-base rootfs (Compose バンドル用の共通土台) ---
# Debian + Docker + docker compose を含み、/sbin/init が2モードで動く:
#   build モード (カーネル引数に hb_build): scratch(/dev/vdb) のバンドルを docker load /
#     overlay 展開 / compose 設定を正規化出力し、結果を scratch に書き戻して poweroff。
#   run モード (通常起動): dockerd を起動し /opt/app の compose を up して居座る。
# ゲストカーネル(6.1)に nftables が無いため iptables は legacy に固定する。
DOCKER_BASE_ROOTFS_PATH = os.path.join(DEFAULTS_DIR, "docker-base.ext4")
DOCKER_BASE_ROOTFS_SIZE_MB = 2048   # Debian + docker-ce の最小構成（convert_tar_to_ext4 が実サイズで自動拡張）
DOCKER_BASE_LABEL = "Debian + Docker + compose (Compose バンドル用土台)"
DOCKER_BASE_DEBIAN_IMAGE = "debian:bookworm-slim"

# --- Attack Machine (Kali Linux + XFCE + TigerVNC) ---
ATTACK_ROOTFS_PATH = os.path.join(DEFAULTS_DIR, "attack-rootfs.ext4")
ATTACK_ROOTFS_SIZE_MB = 4096  # kali-linux-core + XFCE/VNC の最小構成（convert_tar_to_ext4が実サイズに応じて自動拡張する）
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


# docker-base の /sbin/init（build/run 2 モード）。
# __RESOLV_CONF__ は実行時に FC_GUEST_DNS から差し込む（置換方式）。
# dockerd は overlay2 + legacy iptables 前提（ゲストカーネル 6.1 に nftables 無し）。
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


# ホストの Docker ソケット（デフォルト資産・Attack Machine の rootfs 構築にのみ使用）。
# セキュリティ上、定常運用では docker-compose.yml のマウントを外せるようにしており、
# 外した場合は資産ビルドだけが無効化され、既存資産での VM 起動には影響しない。
_DOCKER_SOCK = "/var/run/docker.sock"


def _docker_available() -> bool:
    return os.path.exists(_DOCKER_SOCK)


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


def _resolve_within(dest_real: str, norm_path: str) -> str:
    """dest_real（展開先の実パス）を基準に norm_path を1階層ずつ解決し、実パスを返す。

    途中の階層が既存のシンボリックリンクであれば os.path.realpath と同様に解決するが、
    「解決結果が dest_real の外に出た時点で例外を上げる」点が os.path.realpath と異なる
    （realpath は外に出ても黙って解決してしまい、呼び出し後に文字列比較するしかない。
    ここでは1階層ごとに検証するため、最終コンポーネント自身がシンボリックリンクで
    外を指す場合も確実に捕まえる）。
    """
    current = dest_real
    parts = [p for p in norm_path.split(os.sep) if p not in ("", ".")]
    for part in parts:
        candidate = os.path.join(current, part)
        if os.path.islink(candidate):
            link_target = os.readlink(candidate)
            if os.path.isabs(link_target):
                resolved = os.path.normpath(link_target)
            else:
                resolved = os.path.normpath(os.path.join(current, link_target))
            # リンク先自体がさらにリンクの場合に備え realpath で完全解決する。
            # ただし起点は必ず current（展開先内の既知の位置）に固定しているため、
            # ホスト上の無関係な絶対パスへは、リンク先が展開先の中を指す限り出ない。
            resolved = os.path.realpath(resolved) if os.path.exists(resolved) else resolved
            if not (resolved == dest_real or resolved.startswith(dest_real + os.sep)):
                raise tarfile.TarError(f"展開先の外を指すシンボリックリンクです: {part} -> {link_target}")
            current = resolved
        else:
            current = candidate
    return current


def _safe_extract_tar(tar_path: str, dest_dir: str) -> None:
    """信用できない tar を安全に展開する（ブロッキングI/O・executor経由で呼ぶこと）。

    方針: シンボリックリンクの *作成* 自体は許可する（リンク先がどこを指す文字列であれ、
    作成時点では何も読み書きしないため無害）。Alpine/Debian 等の実在の rootfs は
    `/bin/ls -> /bin/busybox` のような絶対パスのシンボリックリンクを標準的に含むため、
    tarfile の filter="data" のように絶対パスリンクを一律拒否すると正当な rootfs の
    変換そのものが壊れる。
    その代わり、通常ファイル/ディレクトリを書き込む *直前* に、その書き込み先の親ディレクトリを
    1階層ずつ解決し、既存のシンボリックリンクを経由して展開先ディレクトリの外に出ていないかを
    検証する（symlink traversal 対策）。エントリ名自体の絶対パス・親参照（../）は常に拒否する。
    """
    dest_real = os.path.realpath(dest_dir)
    os.makedirs(dest_real, exist_ok=True)

    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            norm = os.path.normpath(m.name)
            if m.name.startswith("/") or os.path.isabs(norm) or norm.startswith(".."):
                raise tarfile.TarError(f"不正なパスを含むエントリです: {m.name}")
            if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise tarfile.TarError(f"デバイス/FIFOファイルを含むエントリです: {m.name}")

            parent_norm = os.path.dirname(norm)
            # 親ディレクトリを1階層ずつ解決し、途中の symlink が外を指していないか検証する。
            # （エントリ自身の名前がまだ存在しない通常のケースなので、親までを検証すれば十分）
            parent_real = _resolve_within(dest_real, parent_norm) if parent_norm else dest_real
            target_path = os.path.join(parent_real, os.path.basename(norm))

            if m.issym():
                # シンボリックリンクの作成自体は許可する（中身はリンク先の文字列のみで、
                # 作成時点では何も読み書きしない）。既存の同名パスがあれば削除してから作る。
                os.makedirs(parent_real, exist_ok=True)
                if os.path.lexists(target_path):
                    os.remove(target_path)
                os.symlink(m.linkname, target_path)
                continue
            if m.islnk():
                # ハードリンクは tar 内の別エントリを指す。リンク先が展開先の内側にあるかのみ検証。
                link_norm = os.path.normpath(m.linkname)
                if os.path.isabs(m.linkname) or link_norm.startswith(".."):
                    raise tarfile.TarError(f"展開先の外を指すハードリンクです: {m.name} -> {m.linkname}")
                link_target_real = _resolve_within(dest_real, link_norm)
                if not os.path.exists(link_target_real):
                    raise tarfile.TarError(f"ハードリンクの参照先が見つかりません: {m.name} -> {m.linkname}")
                os.makedirs(parent_real, exist_ok=True)
                if os.path.lexists(target_path):
                    os.remove(target_path)
                os.link(link_target_real, target_path)
                continue

            if m.isdir():
                os.makedirs(target_path, exist_ok=True)
                continue

            # 通常ファイル等。tarfile に実体の書き込みを任せるが、展開先は検証済みの実パスにする。
            os.makedirs(parent_real, exist_ok=True)
            if os.path.lexists(target_path) and not os.path.isdir(target_path):
                os.remove(target_path)
            m_copy = tarfile.TarInfo(name=os.path.basename(norm))
            m_copy.type, m_copy.mode, m_copy.size = m.type, m.mode, m.size
            m_copy.mtime, m_copy.uid, m_copy.gid = m.mtime, m.uid, m.gid
            if m.isfile():
                with tf.extractfile(m) as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(target_path, m.mode & 0o7777)
            # isfile 以外（ソケット等）は無視する（ゲスト rootfs に不要かつ扱いが煩雑なため）


# rootfs tar → ext4 変換で使う scratch（展開先）のサイズを、入力 tar の圧縮サイズから
# 事前計算するための倍率・余白。tar は展開すると（圧縮効率次第で）数倍に増えることがあるため、
# 十分に大きい倍率を取った上で固定の余白も足す。実サイズに応じた動的な再計算は行わない
# （ビルド用VMの中では事前にサイズを確定したディスクが必要なため）。
_ROOTFS_CONVERT_SIZE_MULTIPLIER = 4
_ROOTFS_CONVERT_SIZE_MARGIN_MB = 256


async def convert_tar_to_ext4(tar_path: str, dest_path: str, size_mb: int = ROOTFS_SIZE_MB) -> None:
    """信用できない rootfs tar（ユーザアップロード）を、ビルド用VMの中で安全に展開し
    ext4 イメージへ変換する。dest_path へ os.replace する。

    tar のパース・展開処理そのものをネットワークなしの使い捨てVMの中で実行するため、
    tar の内容（symlink traversal に限らず、tar パーサー自体の未知の脆弱性を含む）が
    ホストに影響することはない。

    手順:
      1. 入力 tar の圧縮サイズから、十分な余白を持つ scratch（展開先）サイズを事前計算
      2. scratch（空の ext4）・input（tar の生バイト列を書き込んだブロックデバイス）・
         status（結果報告用の小さな ext4）をホスト上に用意
      3. rootfs 変換専用ベース（ROOTFS_CONVERTER_BASE_PATH）をベースに image_build.run_build_vm
         を実行。ゲストの中で input を展開して scratch に書き込み、status に結果を報告する
      4. status を読んで成否判定。成功なら scratch を dest_path として回収する
    """
    from services import image_build

    if not os.path.exists(ROOTFS_CONVERTER_BASE_PATH):
        raise RuntimeError(
            "rootfs 変換専用ベースが見つかりません（サーバの初期化が完了していない可能性があります）: "
            f"{ROOTFS_CONVERTER_BASE_PATH}"
        )

    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-convert-")
    try:
        input_disk = os.path.join(tmpdir, "input.raw")
        scratch_disk = os.path.join(tmpdir, "scratch.ext4")
        status_disk = os.path.join(tmpdir, "status.ext4")

        # 1. scratch サイズを tar の圧縮サイズから事前計算する
        try:
            tar_size = os.path.getsize(tar_path)
        except OSError as e:
            raise RuntimeError(f"rootfs tar が読み取れません: {e}")
        auto_mb = int(tar_size * _ROOTFS_CONVERT_SIZE_MULTIPLIER / (1024 * 1024)) + _ROOTFS_CONVERT_SIZE_MARGIN_MB
        image_mb = max(size_mb, auto_mb)

        # 2. 各ディスクを用意する（いずれもブロッキングI/O。executor 経由で実行）
        loop = asyncio.get_event_loop()

        def _prepare_disks() -> None:
            # input: tar の生バイト列をそのままコピーしたファイル（ブロックデバイスとして渡す）
            shutil.copy2(tar_path, input_disk)
            # scratch: 空の ext4（ここに tar を展開した結果がそのまま最終 rootfs になる）
            subprocess_run(["dd", "if=/dev/zero", f"of={scratch_disk}", "bs=1M", f"count={image_mb}"])
            subprocess_run(["mkfs.ext4", "-F", "-O", "^metadata_csum", scratch_disk])
            # status: 結果報告用の小さな ext4
            subprocess_run(["dd", "if=/dev/zero", f"of={status_disk}", "bs=1M", "count=4"])
            subprocess_run(["mkfs.ext4", "-F", "-O", "^metadata_csum", status_disk])

        await loop.run_in_executor(None, _prepare_disks)

        # 3. ビルド用VMで変換を実行する
        try:
            await image_build.run_build_vm(
                kernel_path=DEFAULT_KERNEL_PATH,
                base_rootfs_path=ROOTFS_CONVERTER_BASE_PATH,
                scratch_path=scratch_disk,
                input_disk_path=input_disk,
                status_disk_path=status_disk,
                boot_marker="hb_rootfs_convert=1",
                result_rootfs_path=dest_path,
                result_from_scratch=True,
            )
        except image_build.BuildVMError as e:
            raise RuntimeError(f"rootfs 変換用VMの実行に失敗しました: {e}")

        # 4. status を読んで成否判定する（debugfs 経由。loop マウント不要）
        status_text = await _debugfs_cat(status_disk, "status")
        if status_text.strip() != "0":
            error_text = await _debugfs_cat(status_disk, "error")
            log_text = await _debugfs_cat(status_disk, "convert.log")
            detail = error_text.strip() or log_text.strip()[-400:] or f"status={status_text.strip()!r}"
            # 失敗時は dest_path に書き込まれてしまった中途半端な成果物を削除する
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
            raise RuntimeError(f"rootfs tar の変換に失敗しました: {detail}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def subprocess_run(cmd: list[str]) -> None:
    """同期的にコマンドを実行する（run_in_executor 経由で呼ぶための小さなラッパー）。"""
    import subprocess
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {result.stderr.decode(errors='replace').strip()}")


async def _debugfs_cat(ext4_path: str, filename: str) -> str:
    """debugfs 経由で ext4 イメージ内のファイルを読む（loop マウント不要）。"""
    proc = await asyncio.create_subprocess_exec(
        "debugfs", "-R", f"cat /{filename}", ext4_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


async def _convert_tar_to_ext4_host_unsafe(tar_path: str, dest_path: str, size_mb: int = ROOTFS_SIZE_MB) -> None:
    """単純なファイルシステム tar を、ホスト上で直接展開して ext4 イメージへ変換する。

    「_host_unsafe」の名の通り、信用できない（ユーザがアップロードした）tar には
    絶対に使わないこと。tar のパース・展開処理自体をホストのプロセスとして実行するため、
    今回の symlink traversal 対策（_safe_extract_tar）でカバーしていない未知の攻撃面
    （tar パーサー自体の脆弱性等）にはこの関数単体では対処できない。
    用途は HackBento 自身が管理者操作（docker.sock 経由）で生成する、内容を把握できる
    tar の変換に限定する（現時点では rootfs 変換専用ベース自身の初回ビルドのみ）。
    ユーザがアップロードする rootfs tar の変換は convert_tar_to_ext4（ビルド用VM経由）を使うこと。

    mount/loop デバイスを使わず mkfs.ext4 -d で直接構築する（_build_default_rootfs と同じ方式）。
    イメージサイズは展開後のディレクトリ実サイズを元に自動計算し、固定値より大きい場合はそちらを使う。
    """
    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-convert-")
    try:
        rootfs_root = os.path.join(tmpdir, "rootfs")
        os.makedirs(rootfs_root, exist_ok=True)
        # tar 展開はブロッキングI/O（数百MB規模になり得る）のため event loop を専有しないよう
        # executor で実行する。かつ filter="data" で展開先の外への書き込みを防ぐ。
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _safe_extract_tar, tar_path, rootfs_root
            )
        except tarfile.TarError as e:
            raise RuntimeError(f"rootfs tar の展開に失敗しました（不正な構造の可能性があります): {e}")
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
        # metadata_csum を無効化してから -d で populate する。
        # libext2fs の populate 経路は大きい/深いツリー（docker export された Debian 等）で
        # "Directory block checksum does not match" を起こすことがあり、
        # ディレクトリブロックのチェックサム機能を切ることで回避する。
        # （loop マウントでの展開は CAP_SYS_ADMIN が必要で、このコンテナでは使えない）
        await _run([
            "mkfs.ext4", "-F",
            "-O", "^metadata_csum",
            "-d", rootfs_root,
            tmp_image,
        ])
        os.replace(tmp_image, dest_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _detect_archive_kind(path: str) -> str:
    """アップロードされたファイルの種類を判定する。

    戻り値: "ext4"（変換不要・そのまま利用可能） / "tar"（単純なファイルシステムtar・変換対象）
            / "docker-save"（複数レイヤーのOCIアーカイブ・非対応）
            / "compose-bundle"（hackbento-pack の Compose バンドル・ビルド用VMで組み立て）
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
        # Compose バンドル（目印: hackbento-bundle.json + images.tar + app/）
        if "hackbento-bundle.json" in names and "images.tar" in names \
           and any(n == "app" or n.startswith("app/") for n in names):
            return "compose-bundle"
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
    if not _docker_available():
        raise RuntimeError(
            "デフォルト rootfs の構築には Docker (docker.sock) が必要ですが、"
            f"{_DOCKER_SOCK} が見つかりません。資産ビルド時のみ docker-compose.yml の "
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


def _build_rootfs_converter_init_script() -> str:
    return _ROOTFS_CONVERTER_INIT_SCRIPT


async def _build_rootfs_converter_base() -> None:
    """rootfs 変換専用ベース（tar コマンドのみの最小 Alpine）を構築する。

    この資産自体はビルド時に docker.sock（信頼できる管理者操作）を使って作るが、
    出来上がったベースは「ユーザがアップロードする信用できない rootfs tar」を
    ビルド用VMの中で展開するための土台として使う（services.rootfs_convert から利用）。
    """
    if os.path.exists(ROOTFS_CONVERTER_BASE_PATH):
        logger.info(f"rootfs 変換専用ベースは既に存在します: {ROOTFS_CONVERTER_BASE_PATH}")
        return
    if not _docker_available():
        raise RuntimeError(
            "rootfs 変換専用ベースの構築には Docker (docker.sock) が必要ですが、"
            f"{_DOCKER_SOCK} が見つかりません。資産ビルド時のみ docker.sock マウントを有効にしてください。"
        )
    logger.info("rootfs 変換専用ベースを構築中...")

    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-converter-")
    container_name = f"hackbento-rootfs-converter-build-{os.path.basename(tmpdir)}"
    try:
        build_script = _ROOTFS_CONVERTER_BASE_BUILD_SCRIPT.format(
            init_script=_build_rootfs_converter_init_script()
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
        await _convert_tar_to_ext4_host_unsafe(tar_path, ROOTFS_CONVERTER_BASE_PATH, size_mb=ROOTFS_CONVERTER_BASE_SIZE_MB)
        logger.info(f"rootfs 変換専用ベースを保存しました: {ROOTFS_CONVERTER_BASE_PATH}")
    finally:
        try:
            await _run(["docker", "rm", "-f", container_name])
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _build_docker_base_rootfs() -> None:
    """Compose バンドル用の docker-base rootfs（Debian + Docker + compose）を構築する。

    Debian コンテナ内で docker-ce 一式を導入し、2 モード init を仕込んで docker export で
    取り出し、ext4 へ変換する。dockerd はこのビルドコンテナ内では起動しない
    （インストールのみ）。実際に dockerd が動くのはビルド用/本番用 VM の中。
    """
    if os.path.exists(DOCKER_BASE_ROOTFS_PATH):
        logger.info(f"docker-base rootfs は既に存在します: {DOCKER_BASE_ROOTFS_PATH}")
        return
    if not _docker_available():
        raise RuntimeError(
            "docker-base rootfs の構築には Docker (docker.sock) が必要ですが、"
            f"{_DOCKER_SOCK} が見つかりません。資産ビルド時のみ docker.sock マウントを有効にしてください。"
        )
    logger.info("docker-base rootfs を構築中...（数分かかる場合があります）")

    tmpdir = tempfile.mkdtemp(prefix="hackbento-docker-base-")
    container_name = f"hackbento-docker-base-build-{os.path.basename(tmpdir)}"
    try:
        # daemon.json の { } を含むため str.format は使わず置換で init を差し込む
        build_script = _DOCKER_BASE_BUILD_SCRIPT.replace("__INIT_SCRIPT__", _build_docker_base_init_script())
        try:
            await _run(["docker", "rm", "-f", container_name])
        except Exception:
            pass
        await _run([
            "docker", "run", "--name", container_name,
            DOCKER_BASE_DEBIAN_IMAGE,
            "bash", "-c", build_script,
        ], timeout=1800)

        tar_path = os.path.join(tmpdir, "rootfs.tar")
        # docker export でホスト側に直接ストリーミング（大きいためメモリに載せない）
        await _run_to_file(["docker", "export", container_name], tar_path, timeout=900)
        await convert_tar_to_ext4(tar_path, DOCKER_BASE_ROOTFS_PATH, size_mb=DOCKER_BASE_ROOTFS_SIZE_MB)
        logger.info(f"docker-base rootfs を保存しました: {DOCKER_BASE_ROOTFS_PATH}")
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
    x11-xserver-utils \
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


async def _build_attack_rootfs() -> None:
    global attack_rootfs_build_status, attack_rootfs_build_error

    if os.path.exists(ATTACK_ROOTFS_PATH):
        logger.info(f"Attack Machine 用 rootfs は既に存在します: {ATTACK_ROOTFS_PATH}")
        attack_rootfs_build_status = "ready"
        return
    if not _docker_available():
        attack_rootfs_build_status = "failed"
        attack_rootfs_build_error = (
            f"Attack Machine の rootfs 構築には Docker (docker.sock) が必要ですが、"
            f"{_DOCKER_SOCK} が見つかりません。資産ビルド時のみ docker.sock マウントを有効にしてください。"
        )
        logger.warning(attack_rootfs_build_error)
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
        await db.commit()


async def ensure_default_vm_assets() -> None:
    """デフォルトゲストカーネル・rootfs を準備する（無ければ生成）。

    プロジェクト登録時に「デフォルトのカーネル/rootfsを使う」を選択した場合に
    参照される共通ファイル。失敗してもアプリ起動はブロックしない。
    """
    if not os.path.exists("/dev/kvm"):
        logger.warning("/dev/kvm が見つからないため、デフォルトVMアセットの準備をスキップします。")
        return

    os.makedirs(DEFAULTS_DIR, exist_ok=True)

    # rootfs 変換専用ベース（アップロードされた rootfs tar をビルド用VM内で安全に
    # 展開するための土台）を最初に準備する。convert_tar_to_ext4（デフォルト rootfs・
    # docker-base・Attack Machine の構築、および rootfs_convert からのユーザアップロード
    # 変換すべてが内部で使う）がこのベースに依存するため、他のどの資産構築よりも先に
    # 用意できていないと以降の変換が全て失敗する。
    try:
        await _build_rootfs_converter_base()
    except Exception as e:
        logger.error(f"rootfs 変換専用ベースの準備に失敗しました（rootfs の tar→ext4 変換が全て失敗します）: {e}")

    try:
        await _download_default_kernel()
        await _build_default_rootfs()
        await _register_builtin_asset_records()
    except Exception as e:
        logger.error(f"デフォルトVMアセットの準備に失敗しました（プロジェクト作成時に手動アップロードで代替できます）: {e}")

    # docker-base（Compose バンドル用土台）は独立して準備する。
    # 失敗しても通常の OS イメージ運用には影響しないため、別 try で握りつぶす。
    try:
        await _build_docker_base_rootfs()
        await _register_docker_base_asset()
    except Exception as e:
        logger.error(f"docker-base rootfs の準備に失敗しました（Compose バンドル機能のみ無効・手動登録で代替可）: {e}")


async def _register_docker_base_asset() -> None:
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
