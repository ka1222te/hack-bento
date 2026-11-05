import asyncio
import uuid
import json
import logging
import ipaddress
import os
import re
import signal
import tempfile
import time
from config import settings
from services.network import create_tap, delete_tap, _ip_pool, _get_bridge_subnet

logger = logging.getLogger(__name__)

FC_SOCKET_DIR = "/tmp/fc-sockets"                       # VMごとのUnix socketディレクトリ
FC_ROOTFS_DIR = "/data/images/vm/runtime-rootfs"        # Firecracker 起動時に作る VM ごとの writable rootfs（通常ディスク上）
# 過去バージョンはここを tmpfs 運用していた（docker-compose.yml の tmpfs: size=）。
# VM内でDockerを動かす等 rootfs への書き込みが増えるケースで、tmpfsがVM専用cgroupの
# メモリ上限の外側からホストRAMを消費しOOMでホストごと巻き込む恐れがあったため廃止し、
# 常に通常ディスク（/data ボリューム）を使う方式に変更した。このパスは、tmpfs運用
# だった旧バージョンから残っている孤児コピーを掃除する目的でのみ参照する。
FC_ROOTFS_DIR_FALLBACK = "/data/images/vm/runtime-rootfs-disk"

# 実行中 Firecracker プロセスの vm_id → PID マップ（同一プロセス内でのみ有効）。
# stop_vm 時に確実にプロセスを終了させるために保持する。
_fc_pids: dict[str, int] = {}

# vm_id → 起動時に作成した writable rootfs の実体パス
_fc_rootfs_paths: dict[str, str] = {}

# env_id → (コピー済みバイト数, コピー元の合計バイト数)。rootfsのwritableコピー中
# （cp サブプロセス実行中）にUIへ進捗を表示するため、別スレッドでコピー先の
# .tmp ファイルサイズを定期的にポーリングして更新する。コピー完了または失敗時に
# このエントリは削除される（取得側は無ければ「コピー中ではない」と判断する）。
_copy_progress: dict[int, tuple[int, int]] = {}


def get_copy_progress(env_id: int) -> tuple[int, int] | None:
    """env_id のrootfsコピー進捗（コピー済みバイト数, 合計バイト数）を返す。
    コピー中でない場合は None。"""
    return _copy_progress.get(env_id)

# VM ごとのホスト側リソース制限 (メモリ・PID数・CPU使用率) を強制するための
# cgroup v2 委譲ツリーのルート。setup.sh の hackbento-vm-cgroup.service が
# ホスト起動時に作成し、memory/pids/cpu/cpuset を委譲した上で
# docker-compose.override.yml により orchestrator コンテナへ rw バインドマウントされる。
#
# orchestrator コンテナ自身の scope cgroup 配下では cgroup v2 の
# "no internal processes" 制約（コンテナのプロセスが scope 直下に存在するため
# memory/io を子 cgroup へ委譲できない）が働くため、ここを経由しないと
# メモリ上限を強制できない。委譲ツリーが存在しない場合はホスト側の強制を諦め、
# Firecracker の machine-config（ゲスト内のみ有効なソフトな上限）に留める。
VM_CGROUP_ROOT = "/sys/fs/cgroup/hackbento-vms"


def _vm_cgroup_path(vm_id: str) -> str:
    return os.path.join(VM_CGROUP_ROOT, vm_id)


def _cgroup_available() -> bool:
    return os.path.isdir(VM_CGROUP_ROOT) and os.access(VM_CGROUP_ROOT, os.W_OK)


def _create_vm_cgroup(vm_id: str, cpu: int, memory_mb: int, pids_limit: int) -> bool:
    """VM 専用の leaf cgroup を作成し、メモリ・PID数・CPU使用率の上限をホスト側で設定する。

    成功した場合のみ True を返す。委譲ツリーが無い、または書き込みに失敗した場合は
    ログを残して False を返す（呼び出し側は Firecracker の machine-config による
    ソフトな上限のみで起動を続行する）。
    """
    if not _cgroup_available():
        logger.warning(
            f"VM 用 cgroup 委譲ツリーが見つかりません({VM_CGROUP_ROOT})。"
            " ホスト側のリソース上限は適用されません（'sudo ./setup.sh' で構成してください）: vm_id={vm_id[:8]}"
        )
        return False

    path = _vm_cgroup_path(vm_id)
    try:
        os.makedirs(path, exist_ok=True)
        # memory.max はゲストメモリ + Firecracker 自身のオーバーヘッド分の余裕を持たせる。
        # ゲストメモリちょうどに設定すると、ゲストが使い切った際に VMM(Firecracker)側の
        # アロケーションが cgroup 上限に当たり、VM ごと OOM kill される恐れがあるため。
        _MEM_OVERHEAD_MB = 128
        with open(os.path.join(path, "memory.max"), "w") as f:
            f.write(str((memory_mb + _MEM_OVERHEAD_MB) * 1024 * 1024))
        with open(os.path.join(path, "pids.max"), "w") as f:
            f.write(str(pids_limit))
        # cpu.max: "<quota> <period>" 。1コアあたり period=100000us とし、
        # cpu 個数分の使用率を上限とする（例: cpu=1 → "100000 100000" = 1コア分）。
        period_us = 100_000
        quota_us = period_us * cpu
        with open(os.path.join(path, "cpu.max"), "w") as f:
            f.write(f"{quota_us} {period_us}")
        logger.info(
            f"VM cgroup を作成しました: vm_id={vm_id[:8]} memory={memory_mb}MiB pids={pids_limit} cpu={cpu}"
        )
        return True
    except OSError as e:
        logger.warning(f"VM cgroup の設定に失敗しました（ホスト側上限なしで続行します）: vm_id={vm_id[:8]}: {e}")
        try:
            os.rmdir(path)
        except OSError:
            pass
        return False


def _attach_pid_to_cgroup(vm_id: str, pid: int) -> None:
    """Firecracker プロセスを VM 専用 cgroup へ移し、ホスト側の上限を適用させる。"""
    path = _vm_cgroup_path(vm_id)
    if not os.path.isdir(path):
        return
    try:
        with open(os.path.join(path, "cgroup.procs"), "w") as f:
            f.write(str(pid))
        logger.info(f"Firecracker プロセスを cgroup に割り当てました: vm_id={vm_id[:8]} pid={pid}")
    except OSError as e:
        logger.warning(f"cgroup へのプロセス割り当てに失敗しました（ホスト側上限なしで続行します）: vm_id={vm_id[:8]} pid={pid}: {e}")


def _block_device_major_minor(path: str) -> list[str]:
    """path が存在するファイルシステムの背後にあるブロックデバイスの "major:minor" を
    全て返す（LVM等の場合、論理デバイスと、その下位にある実デバイスの両方）。

    cgroup v2 の io.max はデバイス単位（major:minor）で指定する必要があるが、
    LVM（device-mapper）上のファイルシステムの場合、I/Oを最終的に発行する実デバイス
    （例: dm-0 の下にある sda）がパーティション単位の番号（8:3 等）ではなく、
    ホールディスク単位（8:0）でなければ io.max の制限が効かないことを確認した。
    そのため、論理デバイス自身（dm-N）と、/sys/block/<dm-N>/slaves/ から辿れる
    実デバイスのホールディスク（パーティション番号を除いた親デバイス）の両方に
    制限をかける。
    """
    try:
        st = os.stat(path)
    except OSError:
        return []
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    devices = [f"{major}:{minor}"]
    try:
        # /sys/dev/block/<major>:<minor> はこのデバイスの /sys/block 配下エントリへのリンク
        sys_dev_link = f"/sys/dev/block/{major}:{minor}"
        dev_name = os.path.basename(os.readlink(sys_dev_link))
        slaves_dir = f"/sys/block/{dev_name}/slaves"
        if os.path.isdir(slaves_dir):
            for slave_name in os.listdir(slaves_dir):
                # パーティション（例: sda3）ではなくホールディスク（sda）の
                # major:minor を使う。/sys/block/<slave>/dev にホールディスクの
                # major:minor が入っている（パーティションの場合は親ディレクトリを辿る）。
                slave_path = f"/sys/block/{slave_name}"
                if not os.path.isdir(slave_path):
                    # sda3 等パーティションは /sys/block 直下に無く、親ディスクの
                    # 配下（/sys/block/sda/sda3）にあるため、名前から親ディスク名を
                    # 推測するのではなく /sys/class/block/<slave>/../dev を見る。
                    parent_dev_file = f"/sys/class/block/{slave_name}/../dev"
                else:
                    parent_dev_file = f"{slave_path}/dev"
                try:
                    with open(parent_dev_file) as f:
                        devices.append(f.read().strip())
                except OSError:
                    continue
    except OSError:
        pass
    return devices


# VM_CGROUP_ROOT（hackbento-vms）自体にio.maxを設定したかどうかのプロセス内キャッシュ。
# 複数VM起動のたびに毎回設定し直す必要はなく、アプリ起動後一度設定すればその配下の
# 全子cgroup（個々のVM専用cgroup、起動処理中のコピー・debugfsプロセス）に効き続ける。
_vm_cgroup_root_io_limit_applied = False

# rootfsコピー・debugfs呼び出し専用の共有cgroup名。VM_CGROUP_ROOT自体にio.maxを
# 設定しても、デフォルトではcp/debugfsプロセスはorchestratorコンテナのメイン
# プロセスのcgroup（hackbento-vmsの外）に残るため制限が効かない。このcgroup
# （hackbento-vms配下）へ明示的に移すことで、VM_CGROUP_ROOTのio.max（ホスト全体
# 合計の上限）の対象に含める。このcgroup自身にはio.maxを設定しない
# （VM_CGROUP_ROOT側の設定が子孫cgroup全体に継承されるため、個別設定は不要）。
_IO_LIMITED_CGROUP_NAME = "rootfs-copy-shared"


def ensure_vm_cgroup_root_io_limit() -> None:
    """VM_CGROUP_ROOT（hackbento-vms）自体にio.maxを設定し、その配下で動く全プロセス
    （起動処理中のrootfsコピー・debugfs、稼働中の全Firecracker VM）が使うディスク帯域
    の合計をホスト全体で一括制限する。VM_IO_LIMIT_MBPS が未設定(0)、または委譲ツリーが
    無い場合は何もしない。

    VMごとに個別のcgroupへ制限をかける方式ではなく、委譲ツリーのルート自体に設定する
    ことで、「同時に何台動いていても合計でVM_IO_LIMIT_MBPSを超えない」という挙動になる
    （cgroup v2のio.maxは子孫cgroup全体の合計に効く）。ホスト側のsetup.shは
    io コントローラを委譲するだけで、具体的な帯域の値はここ（アプリ側）で決める。
    """
    global _vm_cgroup_root_io_limit_applied
    if _vm_cgroup_root_io_limit_applied:
        return
    if settings.VM_IO_LIMIT_MBPS <= 0:
        return
    if not _cgroup_available():
        return
    devices = _block_device_major_minor(FC_ROOTFS_DIR)
    if not devices:
        return
    try:
        limit_bytes = settings.VM_IO_LIMIT_MBPS * 1024 * 1024
        # LVM等では論理デバイス（dm-N）だけに制限をかけても、下位の実デバイス
        # （パーティションを除いたホールディスク）側では素通りすることを確認した
        # ため、_block_device_major_minor が返す全デバイスに同じ上限を設定する。
        # io.max は1回のwrite()呼び出しにつき1デバイス分の設定のみ受け付ける
        # （複数行をまとめて書き込むとInvalid argumentになる）ため、デバイスごとに
        # 個別にopen/writeする。
        io_max_path = os.path.join(VM_CGROUP_ROOT, "io.max")
        for dev in devices:
            with open(io_max_path, "w") as f:
                f.write(f"{dev} rbps={limit_bytes} wbps={limit_bytes}")
        logger.info(f"VM用cgroup全体のディスクI/O帯域を制限しました: {settings.VM_IO_LIMIT_MBPS}MB/s (devices={devices})")
        _vm_cgroup_root_io_limit_applied = True
    except OSError as e:
        logger.warning(f"VM用cgroup全体のio.max設定に失敗しました（制限なしで続行します）: {e}")


def _get_shared_copy_cgroup() -> str | None:
    """rootfsコピー・debugfs呼び出し用の共有cgroup（VM_CGROUP_ROOT配下）を返す
    （無ければ作成する）。VM_IO_LIMIT_MBPS が未設定、または委譲ツリーが無い場合は
    Noneを返し、呼び出し元はプロセスをどこにも移さず続行する。

    このcgroup自体にio.maxは設定しない。ensure_vm_cgroup_root_io_limit()が
    VM_CGROUP_ROOT自体に設定した上限が、子孫であるこのcgroupにもそのまま継承される。
    """
    if settings.VM_IO_LIMIT_MBPS <= 0:
        return None
    if not _cgroup_available():
        return None
    ensure_vm_cgroup_root_io_limit()
    path = os.path.join(VM_CGROUP_ROOT, _IO_LIMITED_CGROUP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError as e:
        logger.warning(f"rootfsコピー用cgroupの作成に失敗しました（制限なしで続行します）: {e}")
        return None


def _attach_current_process_to_cgroup(cgroup_path: str | None, pid: int | None = None) -> None:
    """指定プロセス（省略時は呼び出し元プロセス自身）を cgroup_path の cgroup.procs に書き込む。
    cgroup_path が None（制限なし）の場合は何もしない。"""
    if cgroup_path is None:
        return
    target_pid = pid if pid is not None else os.getpid()
    try:
        with open(os.path.join(cgroup_path, "cgroup.procs"), "w") as f:
            f.write(str(target_pid))
    except OSError as e:
        logger.warning(f"cgroupへのプロセス割り当てに失敗しました（制限なしで続行します）: {cgroup_path}: pid={target_pid}: {e}")


def _cleanup_stale_vm_cgroups() -> None:
    """委譲ツリー直下に残っている leaf cgroup のうち、プロセスが既にいないものを削除する。

    オーケストレータ異常終了時など、_remove_vm_cgroup が呼ばれずに残った
    空の leaf cgroup を起動時に掃除する（プロセスが残っているものは
    cleanup_orphaned_vms 側の通常フローで処理されるため、ここでは触らない）。
    """
    if not _cgroup_available():
        return
    try:
        entries = os.listdir(VM_CGROUP_ROOT)
    except OSError:
        return
    for name in entries:
        path = os.path.join(VM_CGROUP_ROOT, name)
        if not os.path.isdir(path):
            continue
        try:
            with open(os.path.join(path, "cgroup.procs")) as f:
                if f.read().strip():
                    continue  # プロセスが残っている → 通常のオーファン処理に任せる
            os.rmdir(path)
            logger.info(f"残留した VM cgroup を削除しました: {name}")
        except OSError:
            continue


def _remove_vm_cgroup(vm_id: str) -> None:
    """VM 停止後に leaf cgroup を削除する。プロセスが残っていると失敗するため、
    呼び出し側で確実にプロセスを終了させてから呼ぶこと。"""
    path = _vm_cgroup_path(vm_id)
    if not os.path.isdir(path):
        return
    try:
        os.rmdir(path)
    except OSError as e:
        logger.warning(f"VM cgroup の削除に失敗しました（プロセスが残っている可能性があります）: vm_id={vm_id[:8]}: {e}")


class VMStartResult:
    def __init__(self, vm_id: str, ip: str):
        self.vm_id = vm_id
        self.ip = ip


# ---- Firecracker (bridge) バックエンド ----

_FALLBACK_GUEST_NETMASK = "255.255.240.0"  # /20 (ホストブリッジの subnet が取得できない場合のフォールバック。元のハードコード値を踏襲)

_guest_netmask_cache: str | None = None


async def _guest_netmask() -> str:
    """ゲストに渡すネットマスクをホストブリッジの subnet から動的に求める。

    ブリッジの subnet はホスト構成（setup.sh）で決まり、オーケストレータの
    プロセス生存中に変わることはないため、初回取得後はキャッシュして
    VM起動のたびに subprocess を呼ぶコストを避ける。
    """
    global _guest_netmask_cache
    if _guest_netmask_cache is not None:
        return _guest_netmask_cache

    subnet = await _get_bridge_subnet()
    if subnet is None:
        logger.warning(
            f"ブリッジの subnet を取得できなかったため、ネットマスクをフォールバック値 ({_FALLBACK_GUEST_NETMASK}) で代用します。"
        )
        # 取得失敗はキャッシュしない（ブリッジが後から準備される可能性があるため次回再試行する）
        return _FALLBACK_GUEST_NETMASK
    _guest_netmask_cache = str(subnet.netmask)
    return _guest_netmask_cache


def _validate_ip(ip: str) -> None:
    """IPがIPv4形式かつプール内に含まれることを確認する。"""
    try:
        ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        raise ValueError(f"Invalid IP address: {ip}")
    if ip not in _ip_pool():
        raise ValueError(f"IP address not in pool: {ip}")


# ---- Firecracker バックエンド（KVM必須）----

def _fc_socket_path(vm_id: str) -> str:
    return os.path.join(FC_SOCKET_DIR, f"{vm_id}.sock")


def _fc_rootfs_runtime_path(vm_id: str, base_dir: str = FC_ROOTFS_DIR) -> str:
    return os.path.join(base_dir, f"{vm_id}.ext4")


_FC_API_TIMEOUT = 10.0


async def _fc_api(socket_path: str, method: str, path: str, body: dict | None = None) -> dict:
    """Firecracker Unix socket REST API を呼び出す。

    Firecracker プロセスがハング状態になっていると、ソケット通信がブロックされ
    呼び出し元（VM起動・停止フロー）が無期限に停止してしまう可能性がある。
    ソケット自体に read/write タイムアウトを設定した上で、executor 呼び出し全体にも
    asyncio.wait_for でタイムアウトをかけ、二重に詰まりを防ぐ。
    """
    import http.client
    import socket as _socket

    payload = json.dumps(body).encode() if body is not None else b""

    def _call() -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("localhost", timeout=_FC_API_TIMEOUT)
        try:
            # Unix socket に差し替え
            conn.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            conn.sock.settimeout(_FC_API_TIMEOUT)
            conn.sock.connect(socket_path)
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            # close() しないと Unix ソケットの FD がリークし続け、VM起動毎に
            # 複数回呼ばれるため長期運用でファイルディスクリプタを枯渇させる
            conn.close()

    loop = asyncio.get_event_loop()
    try:
        status, data = await asyncio.wait_for(
            loop.run_in_executor(None, _call), timeout=_FC_API_TIMEOUT + 5.0
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Firecracker API {method} {path} がタイムアウトしました（応答なし）: {socket_path}")
    if status >= 300:
        raise RuntimeError(f"Firecracker API {method} {path} → HTTP {status}: {data.decode()}")
    return json.loads(data) if data else {}


def _fc_socket_accepts(socket_path: str) -> bool:
    """Unix socket が実際に accept 可能か（listen 済みか）を確認する。

    ソケットファイルは bind() 時点で生成され、Firecracker が listen()/accept()
    を呼ぶ前にも存在し得る。ファイルの存在だけで判定すると、起動直後の
    ConnectionRefusedError を _fc_api 呼び出しがそのまま例外として外へ漏らし、
    本来は数百ms待てば成功するはずの VM 起動を失敗させてしまう。
    実際に connect できることまで確認することで、この競合を防ぐ。
    """
    import socket as _socket

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        sock.connect(socket_path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


async def _fc_wait_socket(socket_path: str, timeout: float = 10.0) -> None:
    """Firecracker プロセスの Unix socket が ready になるまで待つ。"""
    deadline = asyncio.get_event_loop().time() + timeout
    loop = asyncio.get_event_loop()
    while True:
        if os.path.exists(socket_path) and await loop.run_in_executor(None, _fc_socket_accepts, socket_path):
            return
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError(f"Firecracker socket が {timeout}秒以内に応答しませんでした: {socket_path}")
        await asyncio.sleep(0.2)


def _write_resolv_conf(rootfs_path: str) -> None:
    """ext4 rootfs の /etc/resolv.conf を FC_GUEST_DNS の内容で上書きする。

    debugfs の write コマンドで一時ファイルをイメージ内へ転送する。

    debugfs はディスクI/O待ちで D 状態（SIGKILLでも終了不可）にハングする
    ことがある。これを subprocess.run(timeout=...) でタイムアウトさせても
    プロセス本体はバックグラウンドでファイルへのI/Oを継続するため、直後に
    Firecracker が同じファイルをドライブとしてアタッチするとファイルが
    不整合になりクラッシュする。そのため Popen + poll でハングを検知し、
    プロセスが完全に終了する（D状態から抜ける）まで待ってから返す。
    タイムアウト・異常終了時は rootfs が不整合な可能性があるとみなし、
    例外を上げて呼び出し元に writable rootfs を破棄させる。
    """
    import subprocess
    nameservers = [ns.strip() for ns in settings.FC_GUEST_DNS.split(",") if ns.strip()]
    if not nameservers:
        return
    resolv_content = "".join(f"nameserver {ns}\n" for ns in nameservers)
    tmp_resolv = None
    proc = None
    io_cgroup = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".resolv", delete=False) as f:
            f.write(resolv_content)
            tmp_resolv = f.name
        # debugfs の write は対象ファイルが既存だと失敗するため、先に rm してから write する。
        # （/etc は通常どの rootfs にも存在するため mkdir は不要。むしろ既存ディレクトリに
        # mkdir すると孤立 inode が生成されファイルシステムが不整合になることを確認済み）
        # rm の対象が存在しない場合は debugfs がエラーを出すが、returncode は 0 のまま
        # 継続するため問題ない。
        # debugfs の write で新規作成された inode のデフォルトモードは 0600 (root only) になる。
        # 一般ユーザ（hackbento等）で実行する getent/curl 等の glibc リゾルバは
        # /etc/resolv.conf を読めないと「DNSサーバ未設定」とみなし、DNSサーバが
        # 正常に応答していても getaddrinfo が NOTFOUND を返す。sif で 0644 に変更する。
        script = (
            f"rm /etc/resolv.conf\n"
            f"write {tmp_resolv} /etc/resolv.conf\n"
            f"sif /etc/resolv.conf mode 0100644\n"
        )
        proc = subprocess.Popen(
            ["debugfs", "-w", rootfs_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # debugfsは複数VM同時起動時にホストのディスクI/O帯域を奪い合い、他の全リクエスト
        # （ログイン等）がイベントループをブロックされて応答不能になる事故が起きたため、
        # VM_IO_LIMIT_MBPSが設定されていればこのプロセスの帯域を制限する。
        io_cgroup = _get_shared_copy_cgroup()
        _attach_current_process_to_cgroup(io_cgroup, proc.pid)
        try:
            stdout, stderr = proc.communicate(input=script, timeout=300)
        except subprocess.TimeoutExpired:
            # D状態でハングしている可能性がある。SIGKILLは送るが、ここで
            # 即座に諦めると裏でI/Oが続いたままFirecrackerに渡してしまう
            # ため、プロセスが実際に終了するまで（ブロッキングで）待つ。
            proc.kill()
            proc.wait()
            raise RuntimeError(f"debugfs timed out and was killed (rootfs={rootfs_path})")
        if proc.returncode != 0:
            raise RuntimeError(f"debugfs write resolv.conf failed: {stderr.strip()}")
    except FileNotFoundError:
        logger.warning("debugfs not found; skipping resolv.conf injection (install e2fsprogs)")
    finally:
        if tmp_resolv is not None:
            try:
                os.remove(tmp_resolv)
            except Exception:
                pass


def _free_bytes(path: str) -> int | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def _select_rootfs_copy_dir(rootfs_path: str) -> str:
    """rootfs の writable コピー先ディレクトリ（FC_ROOTFS_DIR）の空き容量を確認する。

    空きがコピー元サイズ+マージンを下回る場合は RuntimeError を上げ、ディスクフル
    によるホスト全体への波及を未然に防ぐ（起動前に確実に弾く）。
    """
    try:
        need = os.path.getsize(rootfs_path)
    except OSError:
        return FC_ROOTFS_DIR  # サイズ取得不可なら確認をスキップ（起動は試みる）

    margin = 512 * 1024 * 1024  # 512MiB の余裕
    os.makedirs(FC_ROOTFS_DIR, exist_ok=True)
    free = _free_bytes(FC_ROOTFS_DIR)
    if free is None or free >= need + margin:
        return FC_ROOTFS_DIR

    need_mb = need // (1024 * 1024)
    free_mb = free // (1024 * 1024)
    raise RuntimeError(
        f"ホストのディスク空き容量が不足しています（必要 約{need_mb}MiB+余裕、"
        f"空き {free_mb}MiB）。"
        " 停止済み環境の整理や不要な rootfs の削除を行ってください。"
    )


def _copy_rootfs_io_limited(src: str, dst: str, env_id: int | None = None) -> None:
    """rootfsのwritableコピーを作る。shutil.copy2ではなく `cp` サブプロセスを使うのは、
    複数VM同時起動時にホストのディスクI/O帯域を奪い合い、他の全リクエスト（ログイン等）
    がイベントループをブロックされて応答不能になる事故が起きたため、
    VM_IO_LIMIT_MBPSが設定されていればこのプロセスの帯域を制限できるように
    するため（shutil.copy2はorchestratorプロセス自身のスレッドで実行されるため、
    cgroupでの帯域制限をプロセス単位で適用できない）。

    env_id が指定されている場合、コピー先(dst)のファイルサイズを定期的にポーリングし
    _copy_progress へ進捗（コピー済みバイト数, 合計バイト数）を記録する。UIの
    「rootfsコピー中」表示に使われる。
    """
    import subprocess
    io_cgroup = _get_shared_copy_cgroup()
    try:
        total_bytes = os.path.getsize(src)
        if env_id is not None:
            _copy_progress[env_id] = (0, total_bytes)
        # --preserve=timestamps,mode は shutil.copy2 相当（メタデータも保持）。
        proc = subprocess.Popen(["cp", "--preserve=timestamps,mode", src, dst])
        _attach_current_process_to_cgroup(io_cgroup, proc.pid)
        while proc.poll() is None:
            if env_id is not None:
                try:
                    copied = os.path.getsize(dst)
                except OSError:
                    copied = 0
                _copy_progress[env_id] = (copied, total_bytes)
            time.sleep(1)
        returncode = proc.returncode
        if returncode != 0:
            raise RuntimeError(f"cp failed with exit code {returncode} (src={src}, dst={dst})")
        if env_id is not None:
            _copy_progress[env_id] = (total_bytes, total_bytes)
        # cpで書き込んだ分のページキャッシュ（dirty→writeback済みのclean page）を
        # 即座に解放する。数GB〜数十GBのrootfsコピーがホストのページキャッシュに
        # 居座ると、他のcgroup（PostgreSQL等）がメモリ確保のためreclaimを
        # トリガーした際にLRUスキャンが長時間化し、ホスト全体がハングする事故が
        # あったため（KVM async page fault待ちでcontainerd-shimが固着した事例）。
        try:
            fd = os.open(dst, os.O_RDONLY)
            try:
                # POSIX_FADV_DONTNEEDはdirty pageには効かない（writeback待ちのページは
                # ドロップされない）ため、先にfsyncでディスクへ書き切らせてから解放する。
                os.fsync(fd)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)
        except OSError as e:
            logger.warning(f"rootfsコピー後のページキャッシュ解放に失敗しました（続行します）: dst={dst}: {e}")
    finally:
        if env_id is not None:
            _copy_progress.pop(env_id, None)


def _prepare_writable_rootfs(vm_id: str, rootfs_path: str, env_id: int | None = None) -> str:
    """VM 専用の writable rootfs を作る。

    共有 rootfs をそのまま rw で渡すと、複数 VM 間で書き込みが競合したり
    元イメージを汚染するため、起動ごとにコピーを作って使い捨てにする。
    コピー後に FC_GUEST_DNS の内容を /etc/resolv.conf へ書き込む。

    resolv.conf の注入（debugfs）に失敗した場合、書き込み中だった
    コピーは不整合な可能性があるため破棄し、注入なしのクリーンな
    コピーを作り直して VM 起動を続行する（デフォルト/Attack Machine
    の rootfs はゲスト内の起動スクリプトが自前で /etc/resolv.conf を
    生成するため問題なく動作する。カスタム rootfs の場合は DNS 設定
    なしで起動する）。
    """
    copy_dir = _select_rootfs_copy_dir(rootfs_path)
    os.makedirs(copy_dir, exist_ok=True)
    runtime_path = _fc_rootfs_runtime_path(vm_id, copy_dir)
    tmp_path = runtime_path + ".tmp"
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if os.path.exists(runtime_path):
            os.remove(runtime_path)
        _copy_rootfs_io_limited(rootfs_path, tmp_path, env_id=env_id)
        os.replace(tmp_path, runtime_path)
        _fc_rootfs_paths[vm_id] = runtime_path
        try:
            _write_resolv_conf(runtime_path)
        except Exception as e:
            logger.warning(
                f"resolv.conf injection failed; rebuilding clean rootfs copy without it: {e}"
            )
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            _copy_rootfs_io_limited(rootfs_path, tmp_path, env_id=env_id)
            os.replace(tmp_path, runtime_path)
        return runtime_path
    except Exception:
        _fc_rootfs_paths.pop(vm_id, None)
        for p in (tmp_path, runtime_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        raise


def _cleanup_writable_rootfs(vm_id: str) -> None:
    path = _fc_rootfs_paths.pop(vm_id, None)
    # オーケストレータ再起動後（_fc_rootfs_paths が空）の孤児整理等では、どちらの
    # ディレクトリにコピーされたか記録が無いため、両方を確認して存在する方を消す。
    candidates = [path] if path is not None else [
        _fc_rootfs_runtime_path(vm_id, FC_ROOTFS_DIR),
        _fc_rootfs_runtime_path(vm_id, FC_ROOTFS_DIR_FALLBACK),
    ]
    for p in candidates:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


_MANAGED_BOOT_ARG_PREFIXES = ("ip=", "init=", "console=", "root=", "reboot=", "panic=", "rw", "ro")


def _sanitize_extra_boot_args(extra: str | None) -> str:
    """プロジェクト指定の追加カーネル引数から、HackBento が管理する引数を除去する。

    ip=/init=/console=/root=/reboot=/panic=/rw/ro を上書きさせない（ネットワーク・起動の
    整合性を壊さないため）。それ以外（nokaslr, nosmep, nosmap, kpti=0 等）は許可する。
    """
    if not extra:
        return ""
    tokens = []
    for tok in extra.split():
        if any(tok == p or tok.startswith(p) for p in _MANAGED_BOOT_ARG_PREFIXES):
            logger.warning(f"管理対象のカーネル引数を無視しました: {tok}")
            continue
        tokens.append(tok)
    return " ".join(tokens)


async def _start_firecracker(kernel_path: str, rootfs_path: str, ip: str, cpu: int, memory_mb: int,
                             pids_limit: int, extra_boot_args: str | None = None,
                             env_id: int | None = None) -> VMStartResult:
    """
    Firecracker microVM を起動する。

    前提条件:
      - ホスト上に /dev/kvm が存在し、orchestrator コンテナから読み書き可能
      - kernel_path (vmlinux) と rootfs_path (ext4) が配置済み
      - TAP インターフェース操作のため iproute2 が利用可能
      - ゲスト IP のルーティングは bridge 相当の設定をホスト側で実施済み

    kernel_path / rootfs_path はプロジェクト（Image）ごとにアップロードされた
    ゲストカーネル・rootfs ファイルのパス。
    """
    _validate_ip(ip)

    if not kernel_path or not os.path.exists(kernel_path):
        raise RuntimeError(f"ゲストカーネルが見つかりません: {kernel_path}")
    if not rootfs_path or not os.path.exists(rootfs_path):
        raise RuntimeError(f"rootfs が見つかりません: {rootfs_path}")
    if not os.path.exists("/dev/kvm"):
        raise RuntimeError("/dev/kvm が存在しません。KVM が有効か確認してください。")

    # rootfs の writable コピー先の空き容量確認は _prepare_writable_rootfs
    # （_select_rootfs_copy_dir）が行う。

    vm_id = str(uuid.uuid4())
    socket_path = _fc_socket_path(vm_id)
    os.makedirs(FC_SOCKET_DIR, mode=0o700, exist_ok=True)
    # rootfsコピー（_copy_rootfs_io_limited）は数百MB単位のブロッキングI/Oになり得る
    # ため、event loop を専有しないよう executor で実行する（他リクエスト・watchdog
    # 等が詰まるのを防ぐ）。
    writable_rootfs_path = await asyncio.get_event_loop().run_in_executor(
        None, _prepare_writable_rootfs, vm_id, rootfs_path, env_id
    )
    tap = None
    fc_proc = None

    try:
        # TAP インターフェースを作成してゲストのネットワークに使用
        # （ip を渡して、割り当て IP/MAC 以外を名乗るフレームをホスト側で遮断する）
        tap = await create_tap(vm_id, ip)

        # ホスト側のメモリ・PID数・CPU使用率上限を cgroup で強制する（委譲ツリーが無ければスキップ）
        _create_vm_cgroup(vm_id, cpu, memory_mb, pids_limit)

        # Firecracker プロセスを起動（--no-api はなし、REST API 経由で設定）
        fc_proc = await asyncio.create_subprocess_exec(
            "firecracker",
            "--api-sock", socket_path,
            "--log-path", f"/tmp/fc-{vm_id[:8]}.log",
            "--level", "Warning",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        _fc_pids[vm_id] = fc_proc.pid
        # プロセスを VM 専用 cgroup へ移し、ホスト側の上限を有効化する
        _attach_pid_to_cgroup(vm_id, fc_proc.pid)
        await _fc_wait_socket(socket_path, timeout=10.0)

        # ゲストカーネル設定
        # ip= フォーマット: <client>:<server>:<gw>:<mask>::<iface>:<autoconf>
        gw = settings.FC_GUEST_GATEWAY
        netmask = await _guest_netmask()
        _extra = _sanitize_extra_boot_args(extra_boot_args)
        boot_args = (
            f"console=ttyS0 reboot=k panic=1 pci=off "
            f"ip={ip}::{gw}:{netmask}::eth0:off "
            "rw"
        )
        if _extra:
            boot_args = f"{boot_args} {_extra}"
        await _fc_api(socket_path, "PUT", "/boot-source", {
            "kernel_image_path": kernel_path,
            "boot_args": boot_args,
        })

        # rootfs（VM ごとの writable copy を使用）
        await _fc_api(socket_path, "PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": writable_rootfs_path,
            "is_root_device": True,
            "is_read_only": False,
        })

        # ネットワークインターフェース（TAP 経由でゲストに eth0 を提供）
        await _fc_api(socket_path, "PUT", "/network-interfaces/eth0", {
            "iface_id": "eth0",
            "guest_mac": _ip_to_mac(ip),
            "host_dev_name": tap,
        })

        # vCPU・メモリ
        await _fc_api(socket_path, "PUT", "/machine-config", {
            "vcpu_count": cpu,
            "mem_size_mib": memory_mb,
            "smt": False,
        })

        # VM 起動
        await _fc_api(socket_path, "PUT", "/actions", {"action_type": "InstanceStart"})

        logger.info(f"Firecracker VM started: vm_id={vm_id[:8]} ip={ip} tap={tap} kernel={kernel_path} rootfs={writable_rootfs_path}")
        return VMStartResult(vm_id=vm_id, ip=ip)

    except Exception as e:
        # 失敗時のクリーンアップ。
        # ゲストはまだ起動完了していないため graceful shutdown は不要で、即座に
        # SIGKILL してよい。_terminate_pid（SIGTERM → 最大 grace_seconds 待機 →
        # SIGKILL）は通常停止用であり、ここで使うと既に fc_proc.kill() で死んだ
        # プロセスの生存確認に猶予時間いっぱい待たされ、起動失敗のたびに数秒を浪費する。
        pid = _fc_pids.pop(vm_id, None)
        try:
            if os.path.exists(socket_path):
                os.remove(socket_path)
        except Exception:
            pass
        if fc_proc is not None:
            try:
                fc_proc.kill()
                await fc_proc.wait()
            except Exception:
                pass
        if pid is not None and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as e2:
                logger.warning(f"SIGKILL 送信に失敗しました: vm_id={vm_id[:8]} pid={pid}: {e2}")
        # cgroup の削除はプロセスが完全に終了していないと失敗するため、確実に止めてから行う
        _remove_vm_cgroup(vm_id)
        _cleanup_writable_rootfs(vm_id)
        if tap is not None:
            await delete_tap(vm_id)
        raise RuntimeError(f"Firecracker VM起動に失敗しました: {e}") from e


async def _stop_firecracker(vm_id: str) -> None:
    socket_path = _fc_socket_path(vm_id)
    try:
        # SendCtrlAltDel で graceful shutdown を試みる
        await _fc_api(socket_path, "PUT", "/actions", {"action_type": "SendCtrlAltDel"})
        await asyncio.sleep(2)
    except Exception:
        pass

    # プロセスを確実に終了させる（SIGTERM → 猶予 → SIGKILL）。
    # _fc_pids はメモリ上の対応表のため、何らかの理由で記録を失っていた場合に備えて
    # /proc から該当 vm_id の firecracker プロセスを探すフォールバックを行う。
    pid = _fc_pids.pop(vm_id, None)
    if pid is None:
        pid = _find_orphan_fc_pid(vm_id)
        if pid is not None:
            logger.warning(f"_fc_pids に記録が無いため /proc から PID を探索しました: vm_id={vm_id[:8]} pid={pid}")
    await _terminate_pid(pid, vm_id)
    # プロセス終了後に leaf cgroup を削除（残っていると rmdir が失敗するため終了確認後に行う）
    _remove_vm_cgroup(vm_id)
    _cleanup_writable_rootfs(vm_id)

    try:
        if os.path.exists(socket_path):
            os.remove(socket_path)
    except Exception:
        pass
    # ログファイルを削除
    log_path = f"/tmp/fc-{vm_id[:8]}.log"
    try:
        if os.path.exists(log_path):
            os.remove(log_path)
    except Exception:
        pass
    await delete_tap(vm_id)
    logger.info(f"Firecracker VM stopped: vm_id={vm_id[:8]}")


def _pid_is_zombie(pid: int) -> bool:
    """PID が zombie（defunct・終了済みだが未 reap）状態かを /proc から判定する。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        # "<pid> (comm) <state> ..." の state 文字。comm に空白/括弧があり得るため
        # 最後の ')' 以降を見る。
        rparen = data.rfind(b")")
        if rparen == -1:
            return False
        state = data[rparen + 2:rparen + 3]
        return state == b"Z"
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # プロセステーブルに存在しても zombie（終了済み）は「生きていない」とみなす
    return not _pid_is_zombie(pid)


def vm_is_alive(vm_id: str) -> bool:
    """vm_id の Firecracker プロセスが実際に生きているか判定する。

    メモリ上の _fc_pids を優先し、見つからなければ /proc 走査（_find_orphan_fc_pid）で
    フォールバックする。watchdog が「DB 上 running なのに VM が死んでいる」状態
    （カーネルパニック・ゲストの自発 poweroff 等）を検知するために使う。
    """
    pid = _fc_pids.get(vm_id)
    if pid is not None:
        return _pid_alive(pid)
    # メモリ上に記録が無い場合でも、/proc 上に該当プロセスがあれば生存とみなす
    return _find_orphan_fc_pid(vm_id) is not None


async def _terminate_pid(pid: int | None, vm_id: str, grace_seconds: float = 3.0) -> None:
    """指定 PID の Firecracker プロセスを SIGTERM → (猶予後) SIGKILL で確実に終了させる。"""
    if pid is None:
        logger.warning(f"Firecracker プロセスの PID が不明なため、終了確認をスキップします: vm_id={vm_id[:8]}")
        return
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception as e:
        logger.warning(f"SIGTERM 送信に失敗しました: vm_id={vm_id[:8]} pid={pid}: {e}")

    deadline = asyncio.get_event_loop().time() + grace_seconds
    while asyncio.get_event_loop().time() < deadline:
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.2)

    if _pid_alive(pid):
        logger.warning(f"Firecracker プロセスが SIGTERM で終了しなかったため SIGKILL します: vm_id={vm_id[:8]} pid={pid}")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.error(f"SIGKILL 送信に失敗しました: vm_id={vm_id[:8]} pid={pid}: {e}")


def _find_orphan_fc_pid(vm_id: str) -> int | None:
    """/proc を走査し、指定 vm_id の --api-sock を引数に持つ firecracker プロセスの PID を探す。

    オーケストレータ再起動などでプロセスを見失った場合（_fc_pids に記録が無い場合）に使用する。
    """
    socket_path = _fc_socket_path(vm_id)
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    for pid_str in pids:
        try:
            with open(f"/proc/{pid_str}/cmdline", "rb") as f:
                args = f.read().split(b"\x00")
        except OSError:
            continue
        if b"firecracker" not in args[0:1]:
            continue
        if socket_path.encode() in args:
            return int(pid_str)
    return None


_UUID_FILENAME_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.ext4(\.tmp)?$"
)


def _cleanup_orphaned_rootfs_files(active_vm_ids: set[str]) -> int:
    """FC_ROOTFS_DIR / FC_ROOTFS_DIR_FALLBACK 配下の writable rootfs コピーのうち、
    active_vm_ids（現在 running な Environment の vm_id）に対応しないものを削除する。

    対象になるのは、DB レコード自体が削除された後に残った .ext4 や、
    _prepare_writable_rootfs 実行中のクラッシュで中途半端に残った .ext4.tmp。
    ファイル名が vm_id（UUID）.ext4[.tmp] の形式であるものだけを対象にする
    （デフォルト資産等、別の命名規則のファイルは誤って触らないため）。

    戻り値: 削除したファイル数。
    """
    removed = 0
    for base_dir in (FC_ROOTFS_DIR, FC_ROOTFS_DIR_FALLBACK):
        try:
            entries = os.listdir(base_dir)
        except OSError:
            continue
        for name in entries:
            m = _UUID_FILENAME_RE.match(name)
            if not m:
                continue
            vm_id = m.group(1)
            if vm_id in active_vm_ids:
                continue
            path = os.path.join(base_dir, name)
            try:
                size = os.path.getsize(path)
                os.remove(path)
                removed += 1
                logger.info(f"孤立した writable rootfs コピーを削除しました: {path} ({size // (1024*1024)}MiB)")
            except OSError as e:
                logger.warning(f"孤立した writable rootfs コピーの削除に失敗しました: {path}: {e}")
    return removed


async def cleanup_orphaned_vms() -> None:
    """オーケストレータ起動時に、前回プロセスが残した VM・TAP・DBレコードを後始末する。

    プロセス再起動でメモリ上の状態（_fc_pids など）は失われるため、
    DB 上 starting/running のまま残っている Environment と実際の VM の
    状態を突き合わせ、不整合を解消する。
    """
    _cleanup_stale_vm_cgroups()
    ensure_vm_cgroup_root_io_limit()

    # 起動直後は稼働中の VM は存在しないため、残っている anti-spoof テーブル
    # （bridge hb_*）は全て前回の残骸とみなして掃除する。
    from services.network import cleanup_orphaned_antispoof
    await cleanup_orphaned_antispoof(active_vm_ids=set())

    # 前回のビルド VM が残した作業ディレクトリ（rootfs コピー等）を掃除する。
    # ビルド用 cgroup（build-*）は _cleanup_stale_vm_cgroups が空 leaf として回収する。
    try:
        from services.image_build import cleanup_stale_build_work
        cleanup_stale_build_work()
    except Exception as e:
        logger.warning(f"ビルド作業ディレクトリの掃除に失敗しました（続行）: {e}")

    from database import AsyncSessionLocal
    from models import Environment, EnvStatus
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # running な Environment の vm_id 一覧を基準に、FC_ROOTFS_DIR 配下の孤立した
        # writable rootfs コピー（対応する Environment が無い .ext4/.tmp）を掃除する。
        # DB レコード自体が削除された後や、_prepare_writable_rootfs 実行中のクラッシュで
        # Environment 行が存在しないまま残った残骸はこの経路でしか回収できない。
        running_result = await db.execute(
            select(Environment.vm_id).where(Environment.status == EnvStatus.running)
        )
        active_vm_ids = {vm_id for (vm_id,) in running_result.all() if vm_id}
        _cleanup_orphaned_rootfs_files(active_vm_ids)

        result = await db.execute(
            # stopping は前回プロセスが停止処理の途中でクラッシュした場合に残り得る。
            # 含めないと該当環境が永久に stopping のまま孤児化するため、
            # starting/running と同様に「異常終了済み」とみなして後始末する。
            select(Environment).where(Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]))
        )
        stale = result.scalars().all()
        if not stale:
            return

        logger.warning(f"前回起動時の VM が {len(stale)} 件残っています。後始末します。")
        for env in stale:
            vm_id = env.vm_id
            if vm_id:
                pid = _find_orphan_fc_pid(vm_id)
                if pid is not None:
                    logger.warning(f"孤立した Firecracker プロセスを終了します: env_id={env.id} vm_id={vm_id[:8]} pid={pid}")
                    await _terminate_pid(pid, vm_id)
                _fc_pids.pop(vm_id, None)
                _remove_vm_cgroup(vm_id)
                _cleanup_writable_rootfs(vm_id)

                socket_path = _fc_socket_path(vm_id)
                try:
                    if os.path.exists(socket_path):
                        os.remove(socket_path)
                except Exception:
                    pass
                log_path = f"/tmp/fc-{vm_id[:8]}.log"
                try:
                    if os.path.exists(log_path):
                        os.remove(log_path)
                except Exception:
                    pass
                await delete_tap(vm_id)

            env.status = EnvStatus.stopped
            logger.info(f"孤立環境を後始末しました: env_id={env.id} vm_id={(vm_id or '')[:8]}")
        await db.commit()


_ROOTFS_CLEANUP_INTERVAL_SECONDS = 300


async def run_rootfs_cleanup_worker() -> None:
    """FC_ROOTFS_DIR 配下の孤立 writable rootfs コピーを定期的に掃除するバックグラウンドループ。

    cleanup_orphaned_vms は起動時に一度だけ実行されるため、長時間稼働中に発生した
    残骸（_prepare_writable_rootfs 実行中のクラッシュによる .ext4.tmp、DB レコードが
    削除された後に残った .ext4 等）は次回再起動まで残り続けてしまう。
    watchdog（services/watchdog.py）と同じ「while True + try/except でログに握り込み +
    末尾 asyncio.sleep」パターンで、稼働中もこの種の残骸を定期回収する。
    """
    from database import AsyncSessionLocal
    from models import Environment, EnvStatus
    from sqlalchemy import select, func

    logger.info("Rootfs cleanup worker started")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                # VM起動処理（_start_firecracker）は vm_id を UUID として生成し
                # writable rootfs コピーを作った後、TAP作成・Firecracker起動を経てから
                # ようやく env.vm_id を確定・commit する（status=running への遷移はさらに後）。
                # つまり starting の間は DB 上の vm_id が NULL のままで、ファイル名
                # （vm_id）とDBで対応付けられない。running のみ・starting+running の
                # vm_id 集合のどちらで判定しても、起動処理中は「対応するDBレコードがない
                # ように見える」ため、このワーカーの定期実行（300秒間隔）が起動処理の
                # 最中に重なると writable rootfs コピーを孤立ファイルと誤判定して削除し、
                # 直後のFirecracker起動が「No such file or directory」で失敗する事故が
                # 起きた（実際に発生）。starting の環境が1件でもある間は、
                # 削除ではなく今回のサイクルをスキップして安全側に倒す。
                starting_count = await db.scalar(
                    select(func.count()).where(Environment.status == EnvStatus.starting)
                )
                if starting_count:
                    logger.info(
                        f"起動処理中の環境が{starting_count}件あるため、今回のrootfs掃除サイクルをスキップします"
                    )
                else:
                    running_result = await db.execute(
                        select(Environment.vm_id).where(Environment.status == EnvStatus.running)
                    )
                    active_vm_ids = {vm_id for (vm_id,) in running_result.all() if vm_id}
                    _cleanup_orphaned_rootfs_files(active_vm_ids)
        except Exception as e:
            logger.error(f"Rootfs cleanup worker error: {e}")
        await asyncio.sleep(_ROOTFS_CLEANUP_INTERVAL_SECONDS)


def _ip_to_mac(ip: str) -> str:
    """IP アドレスから決定論的な MAC アドレスを生成する（06:00:xx:xx:xx:xx）。"""
    parts = ip.split(".")
    return "06:00:{:02x}:{:02x}:{:02x}:{:02x}".format(*map(int, parts))


# ---- 公開インターフェース ----

async def start_vm(image, ip: str, cpu: int = 1, memory_mb: int = 1024, pids_limit: int = 256,
                    env_id: int | None = None) -> VMStartResult:
    """
    Image レコードのゲストカーネル・rootfs を Firecracker microVM として起動する。

    env_id を渡すと、rootfsのwritableコピー中の進捗を get_copy_progress(env_id) で
    参照できるようになる（UIの「rootfsコピー中」表示に使われる）。
    """
    return await _start_firecracker(
        image.kernel_path, image.rootfs_path, ip, cpu, memory_mb, pids_limit,
        extra_boot_args=getattr(image, "extra_boot_args", None),
        env_id=env_id,
    )


async def stop_vm(vm_id: str) -> None:
    await _stop_firecracker(vm_id)
