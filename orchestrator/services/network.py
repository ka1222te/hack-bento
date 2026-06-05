import ipaddress
import asyncio
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import Environment, EnvStatus
from config import settings

logger = logging.getLogger(__name__)
_lock = asyncio.Lock()


def _ip_pool() -> list[str]:
    start = ipaddress.IPv4Address(settings.IP_POOL_START)
    end = ipaddress.IPv4Address(settings.IP_POOL_END)
    return [str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1)]


async def _get_docker_used_ips() -> set[str]:
    """docker network inspect でネットワーク上の実コンテナが使用中のIPを取得する。"""
    import json
    proc = await asyncio.create_subprocess_exec(
        "docker", "network", "inspect", settings.MACVLAN_NETWORK,
        "--format", "{{json .Containers}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return set()
    try:
        containers = json.loads(stdout.decode())
        ips = set()
        for c in containers.values():
            ipv4 = c.get("IPv4Address", "")
            if ipv4:
                ips.add(ipv4.split("/")[0])
        return ips
    except Exception:
        return set()


async def allocate_ip(db: AsyncSession, flush_record) -> Optional[str]:
    """IP を確保し、lock 保持中に flush_record() を呼んで DB に仮記録する。

    flush_record は async callable で、割り当てた IP を受け取り
    Environment レコードを db に add して flush する責務を持つ。
    これにより lock 解放前に DB が更新されるため TOCTOU を防ぐ。
    """
    async with _lock:
        pool = _ip_pool()
        pool_set = set(pool)

        result = await db.execute(
            select(Environment.ip_address).where(
                Environment.status.in_([EnvStatus.starting, EnvStatus.running])
            )
        )
        # DB に記録されたIPのうちプール内のものだけを使用中とみなす
        db_used = {row[0] for row in result.fetchall() if row[0] and row[0] in pool_set}

        # macvlan 上の実使用IPもプール内のものだけを考慮する
        docker_used = {ip for ip in await _get_docker_used_ips() if ip in pool_set}

        used = db_used | docker_used
        for ip in pool:
            if ip not in used:
                await flush_record(ip)
                return ip
        return None


async def _run(cmd: list[str], check: bool = True) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {stderr.decode().strip()}")
    return proc.returncode, stderr.decode().strip()


async def _get_macvlan_subnet() -> Optional[ipaddress.IPv4Network]:
    """macvlan ネットワークの subnet を Docker API から取得する。"""
    import json
    proc = await asyncio.create_subprocess_exec(
        "docker", "network", "inspect", settings.MACVLAN_NETWORK,
        "--format", "{{json .IPAM}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        ipam = json.loads(stdout.decode())
        for cfg in ipam.get("Config", []):
            subnet = cfg.get("Subnet")
            if subnet:
                return ipaddress.IPv4Network(subnet, strict=False)
    except Exception:
        pass
    return None


async def ensure_macvlan_network() -> None:
    """hackbento-vm macvlan ネットワークの存在確認と IP プールの妥当性検証を行う。"""
    proc = await asyncio.create_subprocess_exec(
        "docker", "network", "inspect", settings.MACVLAN_NETWORK,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"macvlan ネットワーク '{settings.MACVLAN_NETWORK}' が見つかりません。"
            " docker compose up で起動してください。"
        )

    # IP プールが macvlan の subnet 内に収まっているか検証
    subnet = await _get_macvlan_subnet()
    if subnet is None:
        logger.warning("macvlan の subnet を取得できませんでした。IP プールの検証をスキップします。")
        return

    pool = _ip_pool()
    invalid = [ip for ip in pool if ipaddress.IPv4Address(ip) not in subnet]
    if invalid:
        raise RuntimeError(
            f"IP_POOL の一部が macvlan subnet ({subnet}) の範囲外です: {invalid[:3]}{'...' if len(invalid) > 3 else ''}。"
            " .env の IP_POOL_START / IP_POOL_END を subnet 内のアドレスに修正してください。"
        )
    logger.info(f"IP pool validated: {pool[0]} - {pool[-1]} ({len(pool)} addresses) within {subnet}")


async def detach_container_network(container_id: str) -> None:
    """コンテナを macvlan ネットワークから切断する。"""
    rc, err = await _run(
        ["docker", "network", "disconnect", "-f", settings.MACVLAN_NETWORK, container_id],
        check=False,
    )
    if rc == 0:
        logger.info(f"macvlan detached: container={container_id[:12]}")
    else:
        logger.warning(f"macvlan disconnect failed (may already be gone): {err}")


async def release_ip(ip: str) -> None:
    pass


# ---- Firecracker バックエンド用 TAP 操作（将来用）----

def tap_name(vm_id: str) -> str:
    return f"tap-{vm_id[:8]}"


async def create_tap(vm_id: str) -> str:
    name = tap_name(vm_id)
    await _run(["ip", "tuntap", "add", name, "mode", "tap"])
    await _run(["ip", "link", "set", name, "up"])
    logger.info(f"TAP created: {name}")
    return name


async def delete_tap(vm_id: str) -> None:
    name = tap_name(vm_id)
    rc, _ = await _run(["ip", "link", "del", name], check=False)
    if rc == 0:
        logger.info(f"TAP deleted: {name}")
    else:
        logger.warning(f"TAP delete failed (may already be gone): {name}")
