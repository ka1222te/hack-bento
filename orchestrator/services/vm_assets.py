"""VM （カーネル・各種 rootfs）のパス・ラベル・既定値を集めた定数モジュール。

services/firecracker_setup.py から分離した責務のひとつ。各のビルド処理
（default_assets.py・docker_base_assets.py・attack_machine_assets.py）と
起動時オーケストレーション（firecracker_setup.py）の両方がこれらの定数を参照する。

Attack Machine の rootfs 構築状況（attack_rootfs_build_status/_error）もここに置く。
routers/attack_machine.py が `from services import vm_assets` で直接参照・
services/attack_machine_assets.py が構築中に書き換えるモジュールレベル変数。
"""
import os

VM_DIR = "/data/images/vm"
DEFAULTS_DIR = os.path.join(VM_DIR, "defaults")
DEFAULT_KERNEL_PATH = os.path.join(DEFAULTS_DIR, "vmlinux")
DEFAULT_ROOTFS_PATH = os.path.join(DEFAULTS_DIR, "rootfs.ext4")

KERNEL_URL = "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128"

# 一覧での表示ラベル。バージョン番号は KERNEL_URL のファイル名から取得し、
# rootfs はログイン情報を明示してすぐに使えることが伝わるようにする
# （認証情報は default_assets._ROOTFS_BUILD_SCRIPT で設定している値と一致させること）。
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
