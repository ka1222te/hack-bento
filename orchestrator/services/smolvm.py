import asyncio
import uuid
import json
import logging
import ipaddress
import os
from config import settings
from services.network import create_tap, delete_tap, _ip_pool

logger = logging.getLogger(__name__)

# Firecracker ゲストカーネル・rootfs の配置ディレクトリ
# ホスト上の /srv/hackbento/vm/ を orchestrator コンテナにマウントして使用
FC_VM_DIR = "/srv/hackbento/vm"
FC_KERNEL  = os.path.join(FC_VM_DIR, "vmlinux")        # ゲストカーネル
FC_ROOTFS  = os.path.join(FC_VM_DIR, "rootfs.ext4")    # ベース rootfs（読み取り専用）
FC_SOCKET_DIR = "/tmp/fc-sockets"                       # VMごとのUnix socketディレクトリ


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


async def _fc_api(socket_path: str, method: str, path: str, body: dict | None = None) -> dict:
    """Firecracker Unix socket REST API を呼び出す。"""
    import http.client
    import socket as _socket

    payload = json.dumps(body).encode() if body is not None else b""

    def _call() -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("localhost")
        # Unix socket に差し替え
        conn.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.sock.connect(socket_path)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()

    loop = asyncio.get_event_loop()
    status, data = await loop.run_in_executor(None, _call)
    if status >= 300:
        raise RuntimeError(f"Firecracker API {method} {path} → HTTP {status}: {data.decode()}")
    return json.loads(data) if data else {}


async def _fc_wait_socket(socket_path: str, timeout: float = 10.0) -> None:
    """Firecracker プロセスの Unix socket が ready になるまで待つ。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if os.path.exists(socket_path):
            return
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError(f"Firecracker socket が {timeout}秒以内に現れませんでした: {socket_path}")
        await asyncio.sleep(0.2)


async def _start_firecracker(oci_ref: str, ip: str, cpu: int, memory_mb: int) -> VMStartResult:
    """
    Firecracker microVM を起動する。

    前提条件:
      - ホスト上に /dev/kvm が存在し、orchestrator コンテナから読み書き可能
      - FC_KERNEL (vmlinux) と FC_ROOTFS (rootfs.ext4) が配置済み
      - TAP インターフェース操作のため iproute2 が利用可能
      - ゲスト IP のルーティングは macvlan 相当の設定をホスト側で実施済み

    oci_ref はここでは「どのイメージか」の識別子として記録するのみ。
    Firecracker はゲストカーネル + rootfs (ext4) で動作するため、
    Docker イメージのマウントには別途 snapshot/overlay の仕組みが必要
    （本実装では共通 rootfs を使用）。
    """
    _validate_ip(ip)

    if not os.path.exists(FC_KERNEL):
        raise RuntimeError(f"ゲストカーネルが見つかりません: {FC_KERNEL}")
    if not os.path.exists(FC_ROOTFS):
        raise RuntimeError(f"ベース rootfs が見つかりません: {FC_ROOTFS}")
    if not os.path.exists("/dev/kvm"):
        raise RuntimeError("/dev/kvm が存在しません。KVM が有効か確認してください。")

    vm_id = str(uuid.uuid4())
    socket_path = _fc_socket_path(vm_id)
    os.makedirs(FC_SOCKET_DIR, mode=0o700, exist_ok=True)

    # TAP インターフェースを作成してゲストのネットワークに使用
    tap = await create_tap(vm_id)

    try:
        # Firecracker プロセスを起動（--no-api はなし、REST API 経由で設定）
        fc_proc = await asyncio.create_subprocess_exec(
            "firecracker",
            "--api-sock", socket_path,
            "--log-path", f"/tmp/fc-{vm_id[:8]}.log",
            "--level", "Warning",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        await _fc_wait_socket(socket_path, timeout=10.0)

        # ゲストカーネル設定
        # ip= フォーマット: <client>:<server>:<gw>:<mask>::<iface>:<autoconf>
        gw = settings.FC_GUEST_GATEWAY
        await _fc_api(socket_path, "PUT", "/boot-source", {
            "kernel_image_path": FC_KERNEL,
            "boot_args": (
                f"console=ttyS0 reboot=k panic=1 pci=off "
                f"ip={ip}::{gw}:255.255.240.0::eth0:off "
                "ro"
            ),
        })

        # rootfs（読み取り専用の共通 ext4 を使用）
        await _fc_api(socket_path, "PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": FC_ROOTFS,
            "is_root_device": True,
            "is_read_only": True,
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

        logger.info(f"Firecracker VM started: vm_id={vm_id[:8]} ip={ip} tap={tap} image={oci_ref}")
        return VMStartResult(vm_id=vm_id, ip=ip)

    except Exception as e:
        # 失敗時のクリーンアップ
        try:
            if os.path.exists(socket_path):
                os.remove(socket_path)
            fc_proc.kill()
        except Exception:
            pass
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
    try:
        # プロセスが残っていれば SIGKILL
        # socket からプロセスを特定できないため socket ファイル削除で代替
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


def _ip_to_mac(ip: str) -> str:
    """IP アドレスから決定論的な MAC アドレスを生成する（06:00:xx:xx:xx:xx）。"""
    parts = ip.split(".")
    return "06:00:{:02x}:{:02x}:{:02x}:{:02x}".format(*map(int, parts))


# ---- 公開インターフェース ----

async def start_vm(oci_ref: str, ip: str, cpu: int = 1, memory_mb: int = 1024) -> VMStartResult:
    if settings.VM_BACKEND == "firecracker":
        return await _start_firecracker(oci_ref, ip, cpu, memory_mb)
    return await _start_docker(oci_ref, ip, cpu, memory_mb)


async def stop_vm(vm_id: str) -> None:
    if settings.VM_BACKEND == "firecracker":
        await _stop_firecracker(vm_id)
    else:
        await _stop_docker(vm_id)
