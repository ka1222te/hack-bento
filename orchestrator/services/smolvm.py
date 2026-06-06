import asyncio
import uuid
import logging
import ipaddress
from config import settings
from services.network import create_tap, delete_tap, _ip_pool

logger = logging.getLogger(__name__)


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

    # 起動時に macvlan ネットワークと IP を直接指定する
    cmd = [
        "docker", "run", "-d",
        "--name", vm_id,
        "--network", settings.MACVLAN_NETWORK,
        "--ip", ip,
        "--cpus", str(cpu),
        "--memory", f"{memory_mb}m",
        "--memory-swap", f"{memory_mb}m",          # swap を CPU 制限内に封じる
        "--storage-opt", f"size={settings.VM_DISK_LIMIT_GB}g",  # rootfs ディスク上限
        "--pids-limit", str(settings.VM_PIDS_LIMIT),            # fork bomb 対策
        "--ulimit", f"nofile=1024:1024",                        # FD 枯渇攻撃防止
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


# ---- Firecracker バックエンド（将来用・KVM必須）----

async def _start_firecracker(oci_ref: str, ip: str, cpu: int, memory_mb: int) -> VMStartResult:
    vm_id = str(uuid.uuid4())
    tap = await create_tap(vm_id)
    try:
        from smolvm import Machine, MachineConfig, NetworkConfig, ResourceSpec
        config = MachineConfig(
            name=vm_id,
            image=oci_ref,
            network=NetworkConfig(tap_name=tap, guest_ip=ip),
            resources=ResourceSpec(cpus=cpu, memory_mb=memory_mb),
        )
        machine = await Machine.create(config)
        await machine.start()
        logger.info(f"VM started: vm_id={vm_id} ip={ip}")
    except Exception as e:
        await delete_tap(vm_id)
        raise RuntimeError(f"Firecracker VM起動に失敗しました: {e}") from e
    return VMStartResult(vm_id=vm_id, ip=ip)


async def _stop_firecracker(vm_id: str) -> None:
    try:
        from smolvm import Machine
        machine = await Machine.get(vm_id)
        await machine.stop()
        await machine.delete()
    except Exception as e:
        logger.warning(f"VM stop failed: {vm_id} {e}")
    await delete_tap(vm_id)


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
