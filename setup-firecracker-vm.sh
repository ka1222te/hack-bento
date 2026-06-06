#!/usr/bin/env bash
# Firecracker ゲストカーネル + SSH rootfs セットアップスクリプト
#
# 実行前提:
#   - /dev/kvm が存在すること（Proxmox LXC: lxc.mount.entry で /dev/kvm をパススルー）
#   - Docker が使えること（rootfs ビルドに使用）
#   - iproute2 がインストール済みであること
#
# 実行方法:
#   sudo bash setup-firecracker-vm.sh

set -euo pipefail

VM_DIR="/srv/hackbento/vm"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.102"
KERNEL_PATH="${VM_DIR}/vmlinux"
ROOTFS_PATH="${VM_DIR}/rootfs.ext4"
ROOTFS_SIZE_MB=512

echo "=== Firecracker VM セットアップ ==="

# --- KVM 確認 ---
if [ ! -e /dev/kvm ]; then
    echo "[ERROR] /dev/kvm が見つかりません。"
    echo "Proxmox ホスト上で LXC コンテナの設定に以下を追加してコンテナを再起動してください:"
    echo ""
    echo "  # /etc/pve/lxc/<CT_ID>.conf に追記"
    echo "  lxc.cgroup2.devices.allow: c 10:232 rwm"
    echo "  lxc.mount.entry: /dev/kvm dev/kvm none bind,create=file 0 0"
    echo "  lxc.cgroup2.devices.allow: c 10:200 rwm"
    echo "  lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file 0 0"
    exit 1
fi
echo "[OK] /dev/kvm 確認済み"

# --- ディレクトリ作成 ---
mkdir -p "${VM_DIR}"
echo "[OK] ${VM_DIR} 作成済み"

# --- ゲストカーネル ダウンロード ---
if [ -f "${KERNEL_PATH}" ]; then
    echo "[SKIP] カーネルは既に存在します: ${KERNEL_PATH}"
else
    echo "[INFO] ゲストカーネルをダウンロード中... (約 25MB)"
    curl -fsSL --progress-bar "${KERNEL_URL}" -o "${KERNEL_PATH}"
    echo "[OK] カーネル保存: ${KERNEL_PATH}"
fi

# --- rootfs 作成 ---
if [ -f "${ROOTFS_PATH}" ]; then
    echo "[SKIP] rootfs は既に存在します: ${ROOTFS_PATH}"
else
    echo "[INFO] SSH rootfs を作成中..."

    # Docker コンテナ上で Alpine + OpenSSH の rootfs を作成
    TMPDIR=$(mktemp -d)
    trap "rm -rf ${TMPDIR}" EXIT

    # Alpine で rootfs をビルド
    docker run --rm \
        -v "${TMPDIR}:/output" \
        alpine:3.19 \
        sh -c '
            apk add --no-cache openssh-server bash sudo

            # root パスワード設定（変更推奨）
            echo "root:hackbento" | chpasswd

            # SSH 設定
            sed -i "s/#PermitRootLogin prohibit-password/PermitRootLogin yes/" /etc/ssh/sshd_config
            sed -i "s/#PasswordAuthentication yes/PasswordAuthentication yes/" /etc/ssh/sshd_config
            echo "PermitRootLogin yes" >> /etc/ssh/sshd_config

            # ホストキー生成
            ssh-keygen -A

            # 起動スクリプト（init として使用）
            cat > /sbin/init <<'"'"'INIT_EOF'"'"'
#!/bin/sh
# ネットワーク待機（カーネルの ip= パラメータで設定済みのはず）
sleep 1

# sysfs/proc マウント
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true

# ループバック
ip link set lo up 2>/dev/null || true

# SSH デーモン起動
/usr/sbin/sshd -D &

# シェル待機
echo "=== HackBento Firecracker VM 起動完了 ==="
wait
INIT_EOF
            chmod +x /sbin/init

            # ファイルシステムを tar でエクスポート
            tar -cf /output/rootfs.tar --exclude=/proc --exclude=/sys --exclude=/dev --exclude=/output /
        '

    # ext4 イメージを作成して rootfs を展開
    dd if=/dev/zero of="${ROOTFS_PATH}" bs=1M count=${ROOTFS_SIZE_MB} status=progress
    mkfs.ext4 -F "${ROOTFS_PATH}"

    # マウントして展開
    MNTDIR=$(mktemp -d)
    mount -o loop "${ROOTFS_PATH}" "${MNTDIR}"
    tar -xf "${TMPDIR}/rootfs.tar" -C "${MNTDIR}"
    # 必要なディレクトリを作成
    mkdir -p "${MNTDIR}/proc" "${MNTDIR}/sys" "${MNTDIR}/dev" "${MNTDIR}/tmp"
    umount "${MNTDIR}"
    rmdir "${MNTDIR}"

    echo "[OK] rootfs 作成完了: ${ROOTFS_PATH} (${ROOTFS_SIZE_MB}MB)"
fi

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "次のステップ:"
echo "  1. .env を編集: VM_BACKEND=firecracker"
echo "  2. docker compose -f docker-compose.yml -f docker-compose.firecracker.yml up -d"
echo "  3. Webからプロジェクトを起動 → 割り当てIPにSSH接続"
echo "     ssh root@<IP>  (パスワード: hackbento)"
echo ""
echo "注意: Firecracker は現在「共通 rootfs」のみ対応。"
echo "      Docker イメージの直接起動には別途スナップショット機能が必要。"
