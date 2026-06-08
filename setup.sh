#!/usr/bin/env bash
# HackBento セットアップスクリプト
# VM_BACKEND (macvlan / bridge) を対話的に選択し、
# .env と docker-compose.override.yml を生成する。

set -euo pipefail

# root で実行されているか確認
if [[ "$EUID" -ne 0 ]]; then
    echo "このスクリプトは sudo で実行してください。"
    echo "  sudo $0"
    exit 1
fi

# sudo 実行前の呼び出しユーザを特定（ファイル所有者に使用）
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_GROUP="$(id -gn "$ACTUAL_USER")"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
OVERRIDE_FILE="${SCRIPT_DIR}/docker-compose.override.yml"

# ファイルを実行者所有に変更するヘルパー
own() { chown "${ACTUAL_USER}:${ACTUAL_GROUP}" "$@"; }

# ---------- ユーティリティ ----------

red()    { echo -e "\033[1;31m$*\033[0m"; }
green()  { echo -e "\033[1;32m$*\033[0m"; }
yellow() { echo -e "\033[1;33m$*\033[0m"; }
bold()   { echo -e "\033[1m$*\033[0m"; }

ask() {
    # ask <変数名> <プロンプト> [デフォルト値]
    local var="$1" prompt="$2" default="${3:-}"
    local display_default=""
    [[ -n "$default" ]] && display_default=" [${default}]"
    while true; do
        read -rp "$(bold "${prompt}${display_default}: ")" value
        value="${value:-$default}"
        if [[ -n "$value" ]]; then
            printf -v "$var" '%s' "$value"
            return
        fi
        yellow "  値を入力してください。"
    done
}

ask_yn() {
    # ask_yn <プロンプト>  → 0:yes 1:no
    local prompt="$1"
    while true; do
        read -rp "$(bold "${prompt} [y/n]: ")" yn
        case "$yn" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
            *) yellow "  y または n を入力してください。" ;;
        esac
    done
}

# ---------- 環境チェック ----------

check_kvm() {
    [[ -e /dev/kvm ]]
}

check_bridge_tools() {
    command -v ip &>/dev/null
}

# 実際に試験用 bridge を作成・削除して、bridge 作成が可能かどうかを判定する
check_bridge_capability() {
    local probe="vmbr-test"

    # 同名の bridge が既に存在する場合、本スクリプトの過去の中断実行による残骸である
    # 可能性が高い（実際に bridge 作成が可能な環境でしか作られないものなので、
    # 「判定不能」として bridge モードの選択肢を消してしまうと不便）。
    # 安全に削除を試み、削除できればそのまま判定を続行する。
    if ip link show "$probe" &>/dev/null; then
        yellow "  [--] 試験用 bridge '${probe}' が既に存在します。残骸とみなして削除を試みます。"
        if ! ip link delete "$probe" &>/dev/null; then
            yellow "  [--] '${probe}' を削除できなかったため、作成試験をスキップします。"
            return 1
        fi
    fi

    if ip link add "$probe" type bridge &>/dev/null; then
        ip link delete "$probe" &>/dev/null || true
        return 0
    fi
    return 1
}

# ---------- .env 書き換え ----------

env_set() {
    # key/val を sed の正規表現・置換パターンとして展開しない（val に
    # `|` `&` `\` 等の sed 特殊文字が含まれるとパターンが壊れる、または
    # 意図しない置換結果になるため）。awk に変数として渡し、文字列として
    # 一致・置換することで、val の内容に関わらず安全に書き換える。
    local key="$1" val="$2"
    if [[ -f "$ENV_FILE" ]] && grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        local tmp line found="" out=()
        tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
        : > "$tmp"
        local prefix="${key}="
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" == "$prefix"* ]]; then
                printf '%s=%s\n' "$key" "$val" >> "$tmp"
            else
                printf '%s\n' "$line" >> "$tmp"
            fi
        done < "$ENV_FILE"
        mv "$tmp" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
    fi
}

# ---------- 物理 NIC / アドレス検出 ----------

list_physical_nics() {
    echo "利用可能なネットワークインターフェース:"
    ip link show | awk '/^[0-9]+:/{
        name=$2; gsub(/[:@].*/, "", name)
        if (name != "lo" && name !~ /^docker/ && name !~ /^br/ && name !~ /^veth/ && name !~ /^vmbr/)
            print "  " name
    }'
    echo ""
}

# 検索して見つかった最初の物理 NIC 名を返す
first_physical_nic() {
    local out
    out="$(ip link show 2>/dev/null)" || true
    awk '/^[0-9]+:/{
        name=$2; gsub(/[:@].*/, "", name)
        if (name != "lo" && name !~ /^docker/ && name !~ /^br/ && name !~ /^veth/ && name !~ /^vmbr/) {
            print name
            exit
        }
    }' <<< "$out"
}

# 指定 NIC (または NIC を master にしている bridge) の IPv4 アドレス(CIDR) を取得
detect_subnet() {
    local nic="$1"
    local cidr out master
    out="$(ip -4 -o addr show dev "$nic" scope global 2>/dev/null)" || true
    cidr="$(awk '{print $4; exit}' <<< "$out")"
    if [[ -z "$cidr" ]]; then
        out="$(ip -o link show dev "$nic" 2>/dev/null)" || true
        master="$(grep -oP 'master \K\S+' <<< "$out" || true)"
        if [[ -n "$master" ]]; then
            out="$(ip -4 -o addr show dev "$master" scope global 2>/dev/null)" || true
            cidr="$(awk '{print $4; exit}' <<< "$out")"
        fi
    fi
    echo "$cidr"
}

# 指定 NIC が既に何らかの master (bridge/bond等) に従属しているか確認し、master名を返す
detect_master() {
    local nic="$1"
    local out
    out="$(ip -o link show dev "$nic" 2>/dev/null)" || true
    grep -oP 'master \K\S+' <<< "$out" || true
}

detect_gateway() {
    local out
    out="$(ip route show default 2>/dev/null)" || true
    awk '/default/{print $3; exit}' <<< "$out"
}

detect_dns() {
    local out
    if command -v resolvectl &>/dev/null; then
        out="$(resolvectl status 2>/dev/null)" || true
        awk '/DNS Servers:/{print $3; exit}' <<< "$out"
    fi
}

# IPv4 ドット表記アドレスを32bit整数に変換する
ip_to_int() {
    local a b c d
    IFS='.' read -r a b c d <<< "$1"
    echo $(( (a << 24) + (b << 16) + (c << 8) + d ))
}

# 指定IPアドレスがCIDRサブネット内に含まれるか判定する (ip_in_subnet <ip> <cidr>)
ip_in_subnet() {
    local ip="$1" cidr="$2"
    local net prefix
    net="${cidr%/*}"
    prefix="${cidr#*/}"
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    [[ "$net" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && "$prefix" =~ ^[0-9]+$ ]] || return 1
    (( prefix >= 0 && prefix <= 32 )) || return 1

    local ip_int net_int mask
    ip_int="$(ip_to_int "$ip")"
    net_int="$(ip_to_int "$net")"
    if (( prefix == 0 )); then
        mask=0
    else
        mask=$(( 0xFFFFFFFF << (32 - prefix) & 0xFFFFFFFF ))
    fi
    (( (ip_int & mask) == (net_int & mask) ))
}

# サブネット内に収まる IPv4 アドレスを聞き取る (ask_ip_in_subnet <変数名> <プロンプト> <CIDR> [デフォルト値])
ask_ip_in_subnet() {
    local var="$1" prompt="$2" cidr="$3" default="${4:-}"
    local value
    while true; do
        ask value "$prompt" "$default"
        if ip_in_subnet "$value" "$cidr"; then
            printf -v "$var" '%s' "$value"
            return
        fi
        yellow "  '${value}' はサブネット ${cidr} 内のIPv4アドレスではありません。再入力してください。"
    done
}

# 物理 NIC を聞き取り、IPアドレス(サブネット)・ゲートウェイ・DNSを探索してメモする
# 結果は NIC / SUBNET / GATEWAY / DNS_SERVER に格納される
detect_network_info() {
    echo ""
    list_physical_nics
    ask NIC "設定対象の物理 NIC" "$(first_physical_nic)"

    if [[ "$BACKEND" == "bridge" ]]; then
        local existing_master
        existing_master="$(detect_master "$NIC")"
        if [[ -n "$existing_master" ]]; then
            echo ""
            red "[ERROR] 物理 NIC '${NIC}' は既に master '${existing_master}' に従属しています。"
            echo "        bridge モードのセットアップでは、この NIC を新しい bridge に付け替える必要がありますが、"
            echo "        既存の master 設定と競合するためセットアップを中断します。"
            echo "        既存の master 設定を確認・解除してから再実行してください。"
            exit 1
        fi
    fi

    local detected_subnet detected_gw detected_dns
    detected_subnet="$(detect_subnet "$NIC")"
    detected_gw="$(detect_gateway)"
    detected_dns="$(detect_dns)"

    while true; do
        echo ""
        bold "--- 検出結果 ---"
        echo "  物理 NIC         : ${NIC}"
        echo "  IPアドレス(CIDR) : ${detected_subnet:-(検出できませんでした)}"
        echo "  デフォルトGW     : ${detected_gw:-(検出できませんでした)}"
        echo "  DNS サーバ       : ${detected_dns:-(検出できませんでした)}"
        echo ""
        if ask_yn "この内容で間違いありませんか？"; then
            break
        fi
        ask detected_subnet "IPアドレス(CIDR例: 192.168.181.31/20)" "$detected_subnet"
        ask detected_gw     "デフォルトゲートウェイ"               "$detected_gw"
        ask detected_dns    "DNS サーバ"                            "$detected_dns"
    done

    SUBNET="$detected_subnet"
    GATEWAY="$detected_gw"
    DNS_SERVER="$detected_dns"
}

# ---------- macvlan セットアップ ----------

setup_macvlan() {
    echo ""
    bold "=== macvlan 設定 ==="
    echo ""
    echo "macvlan モードでは検出済みのアドレス情報をそのまま利用します。"
    echo ""

    ask_ip_in_subnet POOL_START "VM IP プール 開始アドレス" "${SUBNET}"
    ask_ip_in_subnet POOL_END   "VM IP プール 終了アドレス" "${SUBNET}"
    ask NET_NAME   "Docker ネットワーク名" "hackbento-vm"

    local subnet_cidr="${SUBNET}"
    echo ""
    bold "--- 確認 ---"
    echo "  親 NIC         : ${NIC}"
    echo "  サブネット     : ${subnet_cidr}"
    echo "  ゲートウェイ   : ${GATEWAY}"
    echo "  IP プール      : ${POOL_START} - ${POOL_END}"
    echo "  Docker net名   : ${NET_NAME}"
    echo ""
    if ! ask_yn "この内容で設定しますか？"; then
        yellow "キャンセルしました。"
        exit 1
    fi

    # .env 更新
    env_set "VM_BACKEND"    "macvlan"
    env_set "MACVLAN_NETWORK" "${NET_NAME}"
    env_set "IP_POOL_START" "${POOL_START}"
    env_set "IP_POOL_END"   "${POOL_END}"

    # docker-compose.override.yml 生成
    cat > "$OVERRIDE_FILE" <<EOF
# 自動生成 by setup.sh (macvlan モード)
# 再生成するには: sudo ./setup.sh

networks:
  vm_net:
    name: ${NET_NAME}
    driver: macvlan
    driver_opts:
      parent: ${NIC}
    ipam:
      config:
        - subnet: ${subnet_cidr}
          gateway: ${GATEWAY}
EOF

    own "$OVERRIDE_FILE"
    green "[OK] docker-compose.override.yml を生成しました (macvlan モード)"
}

# ---------- bridge ネットワーク設定の永続化 ----------

# setup_bridge で行った bridge 構成 (NIC / BRIDGE_NAME / SUBNET / GATEWAY / DNS_SERVER)
# を netplan 設定として書き出し、再起動後も維持されるようにする
persist_bridge_network() {
    if ! command -v netplan &>/dev/null; then
        yellow "[--] netplan が見つかりません。永続化をスキップします。"
        echo "     お使いのディストリビューションのネットワーク設定方法に合わせて、"
        echo "     以下の構成を手動で永続化してください:"
        echo "       物理 NIC       : ${NIC} (${BRIDGE_NAME} のメンバー)"
        echo "       bridge         : ${BRIDGE_NAME}"
        echo "       IPアドレス     : ${SUBNET}"
        echo "       デフォルトGW   : ${GATEWAY}"
        echo "       DNS サーバ     : ${DNS_SERVER:-(未設定)}"
        return
    fi

    local netplan_file="/etc/netplan/90-hackbento-bridge.yaml"

    {
        echo "network:"
        echo "  version: 2"
        echo "  ethernets:"
        echo "    ${NIC}:"
        echo "      dhcp4: false"
        echo "      dhcp6: false"
        echo "  bridges:"
        echo "    ${BRIDGE_NAME}:"
        echo "      interfaces: [${NIC}]"
        echo "      dhcp4: false"
        echo "      dhcp6: false"
        echo "      addresses: [${SUBNET}]"
        echo "      routes:"
        echo "        - to: default"
        echo "          via: ${GATEWAY}"
        if [[ -n "$DNS_SERVER" ]]; then
            echo "      nameservers:"
            echo "        addresses: [${DNS_SERVER}]"
        fi
    } > "$netplan_file"

    chmod 600 "$netplan_file"

    echo ""
    echo "netplan 設定を生成しました: ${netplan_file}"
    echo ""
    bold "--- ${netplan_file} ---"
    cat "$netplan_file"
    echo ""

    echo "netplan を適用しています（netplan apply）..."
    if netplan apply 2>/dev/null; then
        green "[OK] ネットワーク設定を永続化しました（${netplan_file}）。"
        echo "     再起動後もこの bridge 構成が自動的に適用されます。"
    else
        red "[ERROR] netplan apply に失敗しました。"
        echo "        ${netplan_file} の内容を確認し、必要に応じて手動で 'sudo netplan apply' を実行してください。"
    fi
}

# ---------- VM 用 cgroup 委譲ツリーのセットアップ ----------

# Firecracker VM ごとにホスト側でメモリ・PID数・CPU使用率の上限を
# Linux cgroup v2 で強制するため、ルート cgroup 直下に専用ツリー
# /sys/fs/cgroup/hackbento-vms を作成し、memory/pids/cpu/cpuset を委譲する。
#
# コンテナの scope cgroup 配下では cgroup v2 の "no internal processes" 制約
# （コンテナ自身のプロセスが scope 直下に存在するため memory/io を子へ委譲できない）
# により実現できないため、ホスト側のルート直下に独立したツリーを作成し、
# そのサブツリーだけを orchestrator コンテナへ rw バインドマウントする。
#
# cgroup ツリーは tmpfs 上にあり再起動で消えるため、起動毎に再作成する
# systemd oneshot サービスとして永続化する。
VM_CGROUP_NAME="hackbento-vms"
VM_CGROUP_PATH="/sys/fs/cgroup/${VM_CGROUP_NAME}"
VM_CGROUP_UNIT="/etc/systemd/system/hackbento-vm-cgroup.service"

setup_vm_cgroup() {
    bold "=== VM 用 cgroup 委譲ツリーのセットアップ ==="
    echo "Firecracker VM のメモリ・PID数・CPU使用率をホスト側で強制するため、"
    echo "cgroup v2 の専用ツリー '${VM_CGROUP_PATH}' を作成します。"
    echo ""

    cat > "$VM_CGROUP_UNIT" <<EOF
[Unit]
Description=HackBento VM cgroup 委譲ツリーの作成 (${VM_CGROUP_PATH})
DefaultDependencies=no
After=sysinit.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '\\
    set -e; \\
    mkdir -p ${VM_CGROUP_PATH}; \\
    echo "+memory +pids +cpu +cpuset" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true; \\
    echo "+memory +pids +cpu +cpuset" > ${VM_CGROUP_PATH}/cgroup.subtree_control \\
'
ExecStop=/bin/sh -c 'rmdir ${VM_CGROUP_PATH}/* 2>/dev/null || true; rmdir ${VM_CGROUP_PATH} 2>/dev/null || true'

[Install]
WantedBy=sysinit.target
EOF

    chmod 644 "$VM_CGROUP_UNIT"
    systemctl daemon-reload
    systemctl enable hackbento-vm-cgroup.service &>/dev/null || true

    if systemctl restart hackbento-vm-cgroup.service; then
        green "[OK] cgroup 委譲ツリーを作成しました: ${VM_CGROUP_PATH}"
        echo "     (systemd ユニット 'hackbento-vm-cgroup.service' により次回起動時も自動作成されます)"
        cat "${VM_CGROUP_PATH}/cgroup.subtree_control" 2>/dev/null | sed 's/^/     委譲済みコントローラ: /'
    else
        red "[ERROR] cgroup 委譲ツリーの作成に失敗しました。"
        echo "        VM のホスト側リソース制限（メモリ/PID数/CPU）が無効化された状態で動作します。"
    fi
    echo ""
}

# ---------- bridge セットアップ ----------

setup_bridge() {
    echo ""
    bold "=== bridge 設定 ==="
    echo ""
    echo "bridge モードでは orchestrator が TAP デバイスを作成し、"
    echo "ホスト上の Linux bridge に接続して VM にネットワークを提供します。"
    echo ""

    ask BRIDGE_NAME "作成する bridge 名" "vmbr-hackbento"
    ask_ip_in_subnet POOL_START "VM IP プール 開始アドレス" "${SUBNET}"
    ask_ip_in_subnet POOL_END   "VM IP プール 終了アドレス" "${SUBNET}"

    echo ""
    bold "--- 確認 ---"
    echo "  bridge 名        : ${BRIDGE_NAME}"
    echo "  物理 NIC         : ${NIC}"
    echo "  IPアドレス(CIDR) : ${SUBNET}"
    echo "  デフォルトGW     : ${GATEWAY}"
    echo "  DNS サーバ       : ${DNS_SERVER}"
    echo "  IP プール        : ${POOL_START} - ${POOL_END}"
    echo ""
    if ! ask_yn "この内容で bridge を作成し、ネットワーク設定を切り替えますか？"; then
        yellow "キャンセルしました。"
        exit 1
    fi

    # 既に同名の bridge が存在する場合は事前に確認・削除する
    if ip link show "${BRIDGE_NAME}" &>/dev/null; then
        yellow "[--] bridge '${BRIDGE_NAME}' は既に存在します。"
        if ask_yn "  既存の '${BRIDGE_NAME}' を削除して作り直しますか？"; then
            ip link delete "${BRIDGE_NAME}" &>/dev/null || true
        else
            red "[ERROR] 既存の bridge と競合するためセットアップを中断します。"
            exit 1
        fi
    fi

    # まず bridge の作成・UP のみを単独実行し、作成可能かどうかを確認する
    echo ""
    echo "bridge を作成しています..."
    echo "  sudo ip link add ${BRIDGE_NAME} type bridge && sudo ip link set ${BRIDGE_NAME} up"
    if ip link add "${BRIDGE_NAME}" type bridge && ip link set "${BRIDGE_NAME}" up; then
        green "[OK] bridge '${BRIDGE_NAME}' を作成しました。"
    else
        red "[ERROR] bridge '${BRIDGE_NAME}' の作成に失敗しました。"
        echo "        権限・既存リンクとの競合などを確認してください。"
        ip link delete "${BRIDGE_NAME}" &>/dev/null || true
        exit 1
    fi

    local dns_cmds=""
    if [[ -n "$DNS_SERVER" ]] && command -v resolvectl &>/dev/null; then
        dns_cmds=" && \\
  sudo resolvectl dns ${BRIDGE_NAME} ${DNS_SERVER} && \\
  sudo resolvectl domain ${BRIDGE_NAME} \"~.\""
    fi

    echo ""
    yellow "続けて以下のコマンドをワンライナーで実行します（SSH切断を避けるため一括実行）:"
    echo ""
    echo "  sudo ip link set ${NIC} master ${BRIDGE_NAME} && \\"
    echo "  sudo ip addr add ${SUBNET} dev ${BRIDGE_NAME} && \\"
    echo "  sudo ip addr del ${SUBNET} dev ${NIC} && \\"
    echo "  sudo ip route replace default via ${GATEWAY} dev ${BRIDGE_NAME}${dns_cmds}"
    echo ""

    # SSH 切断を避けるため、NIC付け替えからDNS設定までを一括実行する
    # 既に同じアドレスが付与されている場合は重複エラーを避けるため一旦削除する
    if ip -4 -o addr show dev "${BRIDGE_NAME}" | grep -qF "${SUBNET}"; then
        ip addr del "${SUBNET}" dev "${BRIDGE_NAME}" &>/dev/null || true
    fi

    if [[ -n "$dns_cmds" ]]; then
        if ip link set "${NIC}" master "${BRIDGE_NAME}" \
            && ip addr add "${SUBNET}" dev "${BRIDGE_NAME}" \
            && ip addr del "${SUBNET}" dev "${NIC}" \
            && ip route replace default via "${GATEWAY}" dev "${BRIDGE_NAME}" \
            && resolvectl dns "${BRIDGE_NAME}" "${DNS_SERVER}" \
            && resolvectl domain "${BRIDGE_NAME}" "~."; then
            green "[OK] ネットワーク設定を ${BRIDGE_NAME} に切り替え、DNS を設定しました。"
        else
            red "[ERROR] ネットワーク設定の切り替えに失敗しました。"
            echo "        現在のネットワーク状態を確認し、必要に応じて手動で復旧してください。"
            exit 1
        fi
    else
        yellow "[--] DNS サーバが未検出のため、DNS 設定はスキップします（後で手動設定してください）。"
        if ip link set "${NIC}" master "${BRIDGE_NAME}" \
            && ip addr add "${SUBNET}" dev "${BRIDGE_NAME}" \
            && ip addr del "${SUBNET}" dev "${NIC}" \
            && ip route replace default via "${GATEWAY}" dev "${BRIDGE_NAME}"; then
            green "[OK] ネットワーク設定を ${BRIDGE_NAME} に切り替えました。"
        else
            red "[ERROR] ネットワーク設定の切り替えに失敗しました。"
            echo "        現在のネットワーク状態を確認し、必要に応じて手動で復旧してください。"
            exit 1
        fi
    fi

    echo ""
    bold "--- 物理 NIC (${NIC}) の状態 ---"
    ip addr show "${NIC}"
    echo ""
    bold "--- bridge (${BRIDGE_NAME}) の IPアドレス・デフォルトゲートウェイ ---"
    ip -4 addr show dev "${BRIDGE_NAME}"
    ip route show default
    echo ""
    bold "--- bridge (${BRIDGE_NAME}) の DNS 設定 ---"
    if command -v resolvectl &>/dev/null; then
        resolvectl status "${BRIDGE_NAME}" 2>/dev/null | grep -E "Link|DNS Servers|DNS Domain" || echo "  (取得できませんでした)"
    else
        echo "  (resolvectl が見つかりません)"
    fi
    echo ""
    green "[OK] 物理 NIC は bridge '${BRIDGE_NAME}' に接続され、ネットワークが正常に切り替わりました。"

    echo ""
    yellow "ネットワークの接続が正常か確認してください（別端末から ping や ssh で疎通確認するなど）。"
    if ask_yn "ネットワークの設定を永続化しますか？（再起動後も bridge 構成を維持します）"; then
        persist_bridge_network
    else
        yellow "[--] 永続化をスキップしました。再起動するとネットワーク設定が元に戻る可能性があります。"
    fi

    # .env 更新
    env_set "VM_BACKEND"      "bridge"
    env_set "BRIDGE_NAME"     "${BRIDGE_NAME}"
    env_set "IP_POOL_START"   "${POOL_START}"
    env_set "IP_POOL_END"     "${POOL_END}"
    env_set "FC_GUEST_GATEWAY" "${GATEWAY}"

        # VM ごとのホスト側リソース制限 (memory/pids/cpu) 用 cgroup 委譲ツリー
    setup_vm_cgroup

    # docker-compose.override.yml 生成 (ネットワーク定義なし)
    cat > "$OVERRIDE_FILE" <<EOF
# 自動生成 by setup.sh (bridge モード)
# bridge はホスト OS で管理するため Docker ネットワーク定義なし。
# 再生成するには: sudo ./setup.sh
#
# cgroup: host は ${VM_CGROUP_PATH} のバインドマウント用に必要。
# デフォルト (cgroupns: private) では /sys/fs/cgroup が読み取り専用になり、
# その配下にマウントポイントを作成できないため。

services:
  orchestrator:
    devices:
      - /dev/kvm:/dev/kvm
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
    cgroup: host
    volumes:
      - ${VM_CGROUP_PATH}:${VM_CGROUP_PATH}:rw
EOF

    own "$OVERRIDE_FILE"
    green "[OK] docker-compose.override.yml を生成しました (bridge モード)"
}

# ---------- メイン ----------

# 既にネットワーク (bridge/macvlan) を設定済みで、VM 用 cgroup 委譲ツリーだけを
# (再) セットアップしたい場合のショートカット。
if [[ "${1:-}" == "--vm-cgroup-only" ]]; then
    setup_vm_cgroup
    if [[ -f "$OVERRIDE_FILE" ]] && ! grep -q "${VM_CGROUP_PATH}" "$OVERRIDE_FILE"; then
        yellow "[--] ${OVERRIDE_FILE} に ${VM_CGROUP_PATH} のバインドマウントがありません。"
        echo "     services.orchestrator.volumes に以下を追加してください:"
        echo "       - ${VM_CGROUP_PATH}:${VM_CGROUP_PATH}:rw"
        echo "     追加後、コンテナを再作成してください: docker compose up -d orchestrator"
    fi
    exit 0
fi

echo ""
bold "============================================"
bold "  HackBento セットアップ"
bold "============================================"
echo ""

# .env 初期化
if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "${SCRIPT_DIR}/.env.example" ]]; then
        cp "${SCRIPT_DIR}/.env.example" "$ENV_FILE"
        own "$ENV_FILE"
        green "[OK] .env.example を .env にコピーしました"
    else
        red "[ERROR] .env も .env.example も見つかりません。"
        exit 1
    fi
fi

# KVM チェック
if check_kvm; then
    green "[OK] /dev/kvm が見つかりました。bridge モードが利用可能です。"
    KVM_OK=true
else
    yellow "[--] /dev/kvm が見つかりません。macvlan モードのみ利用可能です。"
    KVM_OK=false
fi

# bridge 作成試験（試験用 bridge "vmbr-test" を作成→削除して可否を判定）
if check_bridge_tools; then
    echo "試験用 bridge 'vmbr-test' を作成して bridge 作成可否を確認しています..."
    if check_bridge_capability; then
        BRIDGE_OK=true
        green "[OK] bridge の作成・削除に成功しました。bridge モードが利用可能です。"
    else
        BRIDGE_OK=false
        yellow "[--] bridge の作成に失敗しました（権限不足または非対応）。macvlan モードを推奨します。"
    fi
else
    BRIDGE_OK=false
    yellow "[--] ip コマンドが見つかりません。iproute2 をインストールしてください。"
fi

echo ""

# バックエンド選択
if [[ "$KVM_OK" == true && "$BRIDGE_OK" == true ]]; then
    bold "利用可能なバックエンド:"
    echo "  1) bridge   - TAP + Linux bridge 経由で VM にネットワーク提供（推奨）"
    echo "  2) macvlan  - macvlan ネットワークでコンテナに直接 IP を割り当て"
    echo ""
    while true; do
        read -rp "$(bold "バックエンドを選択してください [1/2]: ")" choice
        case "$choice" in
            1) BACKEND="bridge";  break ;;
            2) BACKEND="macvlan"; break ;;
            *) yellow "  1 または 2 を入力してください。" ;;
        esac
    done
else
    echo ""
    if ask_yn "macvlan モードでセットアップしますか？"; then
        BACKEND="macvlan"
    else
        yellow "セットアップをキャンセルしました。"
        exit 1
    fi
fi

# 物理 NIC とアドレス情報を聞き取り・検出（macvlan / bridge 共通）
detect_network_info

echo ""

case "$BACKEND" in
    bridge)  setup_bridge  ;;
    macvlan) setup_macvlan ;;
esac

echo ""
bold "=== セットアップ完了 ==="
echo ""
echo "次のステップ:"
echo "  docker compose -f docker-compose.yml -f docker-compose.override.yml up -d"
echo ""
