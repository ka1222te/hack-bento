import asyncio
import uuid
import json
import logging
import ipaddress
import os
import signal
import shutil
import tempfile
from config import settings
from services.network import create_tap, delete_tap, _ip_pool, _get_bridge_subnet

logger = logging.getLogger(__name__)

FC_SOCKET_DIR = "/tmp/fc-sockets"                       # VMごとのUnix socketディレクトリ
FC_ROOTFS_DIR = "/data/images/vm/runtime-rootfs"        # Firecracker 起動時に作る VM ごとの writable rootfs

# 実行中 Firecracker プロセスの vm_id → PID マップ（同一プロセス内でのみ有効）。
# stop_vm 時に確実にプロセスを終了させるために保持する。
_fc_pids: dict[str, int] = {}

# vm_id → 起動時に作成した writable rootfs の実体パス
_fc_rootfs_paths: dict[str, str] = {}

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


def _create_vm_cgroup(vm_id: str, cpu: int, memory_mb: int) -> bool:
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
        with open(os.path.join(path, "memory.max"), "w") as f:
            f.write(str(memory_mb * 1024 * 1024))
        with open(os.path.join(path, "pids.max"), "w") as f:
            f.write(str(settings.VM_PIDS_LIMIT))
        # cpu.max: "<quota> <period>" 。1コアあたり period=100000us とし、
        # cpu 個数分の使用率を上限とする（例: cpu=1 → "100000 100000" = 1コア分）。
        period_us = 100_000
        quota_us = period_us * cpu
        with open(os.path.join(path, "cpu.max"), "w") as f:
            f.write(f"{quota_us} {period_us}")
        logger.info(
            f"VM cgroup を作成しました: vm_id={vm_id[:8]} memory={memory_mb}MiB pids={settings.VM_PIDS_LIMIT} cpu={cpu}"
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


async def docker_load(archive_path: str) -> str:
    cmd = ["docker", "load", "--input", archive_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            logger.error(f"docker load failed: {stderr.decode().strip()}")
            raise RuntimeError("docker load に失敗しました")
        for line in stdout.decode().splitlines():
            if line.startswith("Loaded image:"):
                return line.split("Loaded image:", 1)[1].strip()
            if line.startswith("Loaded image ID:"):
                return line.split("Loaded image ID:", 1)[1].strip()
        logger.error(f"docker load: unexpected output: {stdout.decode()!r}")
        raise RuntimeError("docker load の出力からイメージ名を取得できませんでした")
    except FileNotFoundError:
        raise RuntimeError("docker コマンドが見つかりません。")


# ---- Docker + macvlan バックエンド ----

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


async def _start_docker(oci_ref: str, ip: str, cpu: int, memory_mb: int) -> VMStartResult:
    _validate_ip(ip)
    vm_id = str(uuid.uuid4())
    logger.info(f"docker run: oci_ref={repr(oci_ref)} ip={ip}")

    cmd = [
        "docker", "run", "-d",
        "--name", vm_id,
        "--network", settings.MACVLAN_NETWORK,
        "--ip", ip,
        "--cpus", str(cpu),
        "--memory", f"{memory_mb}m",
        "--memory-swap", f"{memory_mb}m",
        "--storage-opt", f"size={settings.VM_DISK_LIMIT_GB}g",
        "--pids-limit", str(settings.VM_PIDS_LIMIT),
        "--ulimit", "nofile=1024:1024",
        "--ulimit", f"nproc={settings.VM_PIDS_LIMIT}:{settings.VM_PIDS_LIMIT}",
        "--restart", "no",
        oci_ref,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        logger.error(f"docker run failed: {stderr.decode().strip()}")
        raise RuntimeError("コンテナの起動に失敗しました")

    container_id = stdout.decode().strip()
    logger.info(f"Container started: {container_id[:12]} image={oci_ref} ip={ip}")
    return VMStartResult(vm_id=container_id, ip=ip)


async def _stop_docker(vm_id: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "rm", "-f", vm_id,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.communicate(), timeout=30)
    logger.info(f"Container removed: {vm_id[:12]}")


# ---- Firecracker バックエンド（KVM必須）----

def _fc_socket_path(vm_id: str) -> str:
    return os.path.join(FC_SOCKET_DIR, f"{vm_id}.sock")


def _fc_rootfs_runtime_path(vm_id: str) -> str:
    return os.path.join(FC_ROOTFS_DIR, f"{vm_id}.ext4")


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
    debugfs が使えない場合や書き込み失敗は警告ログのみで続行する
    （DNS なしでも VM 自体は起動できるため）。
    """
    import subprocess
    nameservers = [ns.strip() for ns in settings.FC_GUEST_DNS.split(",") if ns.strip()]
    if not nameservers:
        return
    resolv_content = "".join(f"nameserver {ns}\n" for ns in nameservers)
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".resolv", delete=False) as f:
            f.write(resolv_content)
            tmp_resolv = f.name
        # debugfs: mkdir -p /etc は既存でも無害、write で /etc/resolv.conf を上書き
        script = f"mkdir /etc\nwrite {tmp_resolv} /etc/resolv.conf\n"
        result = subprocess.run(
            ["debugfs", "-w", rootfs_path],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(f"debugfs write resolv.conf failed: {result.stderr.strip()}")
    except FileNotFoundError:
        logger.warning("debugfs not found; skipping resolv.conf injection (install e2fsprogs)")
    except Exception as e:
        logger.warning(f"resolv.conf injection failed: {e}")
    finally:
        try:
            os.remove(tmp_resolv)
        except Exception:
            pass


def _prepare_writable_rootfs(vm_id: str, rootfs_path: str) -> str:
    """VM 専用の writable rootfs を作る。

    共有 rootfs をそのまま rw で渡すと、複数 VM 間で書き込みが競合したり
    元イメージを汚染するため、起動ごとにコピーを作って使い捨てにする。
    コピー後に FC_GUEST_DNS の内容を /etc/resolv.conf へ書き込む。
    """
    os.makedirs(FC_ROOTFS_DIR, exist_ok=True)
    runtime_path = _fc_rootfs_runtime_path(vm_id)
    tmp_path = runtime_path + ".tmp"
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if os.path.exists(runtime_path):
            os.remove(runtime_path)
        shutil.copy2(rootfs_path, tmp_path)
        os.replace(tmp_path, runtime_path)
        _fc_rootfs_paths[vm_id] = runtime_path
        _write_resolv_conf(runtime_path)
        return runtime_path
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def _cleanup_writable_rootfs(vm_id: str) -> None:
    path = _fc_rootfs_paths.pop(vm_id, None)
    if path is None:
        path = _fc_rootfs_runtime_path(vm_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


async def _start_firecracker(kernel_path: str, rootfs_path: str, ip: str, cpu: int, memory_mb: int) -> VMStartResult:
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

    vm_id = str(uuid.uuid4())
    socket_path = _fc_socket_path(vm_id)
    os.makedirs(FC_SOCKET_DIR, mode=0o700, exist_ok=True)
    # shutil.copy2 は数百MB単位のブロッキングI/Oになり得るため、event loop を
    # 専有しないよう executor で実行する（他リクエスト・watchdog 等が詰まるのを防ぐ）。
    writable_rootfs_path = await asyncio.get_event_loop().run_in_executor(
        None, _prepare_writable_rootfs, vm_id, rootfs_path
    )
    tap = None
    fc_proc = None

    try:
        # TAP インターフェースを作成してゲストのネットワークに使用
        tap = await create_tap(vm_id)

        # ホスト側のメモリ・PID数・CPU使用率上限を cgroup で強制する（委譲ツリーが無ければスキップ）
        _create_vm_cgroup(vm_id, cpu, memory_mb)

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
        await _fc_api(socket_path, "PUT", "/boot-source", {
            "kernel_image_path": kernel_path,
            "boot_args": (
                f"console=ttyS0 reboot=k panic=1 pci=off "
                f"ip={ip}::{gw}:{netmask}::eth0:off "
                "rw"
            ),
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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


async def _get_macvlan_container_ids() -> set[str]:
    """hackbento-vm（macvlan）ネットワークに接続中のコンテナIDを取得する。"""
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-aq", "--filter", f"network={settings.MACVLAN_NETWORK}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in stdout.decode().splitlines() if line.strip()}


async def _cleanup_orphaned_docker_vms() -> None:
    """オーケストレータ起動時に、前回プロセスが残した Docker コンテナ・DBレコードを後始末する。

    双方向で突き合わせる:
      1. DB が starting/running のままだが対応コンテナが実在しない → 異常終了とみなし stopped にする
      2. macvlan ネットワーク上にコンテナは実在するが DB に対応する有効な Environment が無い
         → IP・リソースを占有し続ける孤児として強制削除する（docker rm -f）
    """
    from database import AsyncSessionLocal
    from models import Environment, EnvStatus
    from sqlalchemy import select

    container_ids = await _get_macvlan_container_ids()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            # stopping は前回プロセスが停止処理の途中でクラッシュした場合に残り得るため、
            # starting/running と同様に後始末の対象に含める（孤児化防止）。
            select(Environment).where(Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]))
        )
        stale = result.scalars().all()

        tracked_ids: set[str] = set()
        for env in stale:
            vm_id = env.vm_id
            if vm_id and vm_id in container_ids:
                tracked_ids.add(vm_id)
                continue
            # DB 上は起動中だが対応するコンテナが存在しない（異常終了）
            logger.warning(f"対応コンテナが見つからない孤立環境を後始末します: env_id={env.id} vm_id={(vm_id or '')[:12]}")
            env.status = EnvStatus.stopped
        if stale:
            await db.commit()

    # DB に追跡されていないが macvlan 上に残っているコンテナを削除する
    # （IPプールやリソース上限を圧迫するゾンビコンテナのため）
    orphan_ids = container_ids - tracked_ids
    for cid in orphan_ids:
        logger.warning(f"DBに記録の無い孤立コンテナを削除します: container={cid[:12]}")
        try:
            await _stop_docker(cid)
        except Exception as e:
            logger.error(f"孤立コンテナの削除に失敗しました: container={cid[:12]}: {e}")


async def cleanup_orphaned_vms() -> None:
    """オーケストレータ起動時に、前回プロセスが残した VM・コンテナ・TAP・DBレコードを後始末する。

    プロセス再起動でメモリ上の状態（_fc_pids など）は失われるため、
    DB 上 starting/running のまま残っている Environment と実際の VM/コンテナの
    状態を突き合わせ、不整合を解消する。
    """
    if settings.VM_BACKEND != "bridge":
        await _cleanup_orphaned_docker_vms()
        return

    _cleanup_stale_vm_cgroups()

    from database import AsyncSessionLocal
    from models import Environment, EnvStatus
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
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


def _ip_to_mac(ip: str) -> str:
    """IP アドレスから決定論的な MAC アドレスを生成する（06:00:xx:xx:xx:xx）。"""
    parts = ip.split(".")
    return "06:00:{:02x}:{:02x}:{:02x}:{:02x}".format(*map(int, parts))


# ---- 公開インターフェース ----

async def start_vm(image, ip: str, cpu: int = 1, memory_mb: int = 1024) -> VMStartResult:
    """
    Image レコードの backend に応じて必要なデータ（oci_ref または kernel/rootfs）を
    取り出し、ホストの VM_BACKEND に対応する実行パスへ渡す。

    backend と VM_BACKEND は本来一致している前提（ホストは一方のモードのみで運用）。
    """
    if settings.VM_BACKEND == "bridge":
        return await _start_firecracker(image.kernel_path, image.rootfs_path, ip, cpu, memory_mb)
    return await _start_docker(image.oci_ref, ip, cpu, memory_mb)


async def stop_vm(vm_id: str) -> None:
    if settings.VM_BACKEND == "bridge":
        await _stop_firecracker(vm_id)
    else:
        await _stop_docker(vm_id)
