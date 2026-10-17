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


async def _get_bridge_tap_count() -> int:
    """ブリッジに接続されている tap-* インターフェースの数を返す。"""
    proc = await asyncio.create_subprocess_exec(
        "ip", "-o", "link", "show", "master", settings.BRIDGE_NAME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        return -1
    if proc.returncode != 0:
        return -1
    count = 0
    for line in stdout.decode().splitlines():
        # "N: tap-xxxxxxxx@if...: <...>" 形式
        name = line.split(":", 2)[1].strip().split("@")[0] if ":" in line else ""
        if name.startswith("tap-"):
            count += 1
    return count


async def _get_bridge_used_ips() -> set[str]:
    """ブリッジ配下の TAP インターフェース上で実際に使用中の IP を ARP/近隣テーブルから取得する。"""
    # ARP/近隣テーブルの STALE エントリは、TAP/VM が削除された後も残り続けることがある
    # （FAILED と異なり再確認されない限り消えない）。tap-* インターフェースが
    # ブリッジ上に1つも存在しない場合、起動中のVMは存在しないため、
    # ARPテーブルの内容に関わらず実使用IPは無いとみなす（ゾンビ占有回避）。
    tap_count = await _get_bridge_tap_count()
    if tap_count == 0:
        return set()

    proc = await asyncio.create_subprocess_exec(
        "ip", "neigh", "show", "dev", settings.BRIDGE_NAME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        return set()
    if proc.returncode != 0:
        return set()
    ips = set()
    for line in stdout.decode().splitlines():
        parts = line.split()
        # FAILED は近隣探索に応答がなかったエントリ（TAP削除後等に残るゴミ）であり、
        # 実体が存在しないため使用中とみなさない。それ以外(STALE/REACHABLE等)は
        # 過去に到達できた実体があるとみなし使用中として扱う。
        if parts and "FAILED" not in parts:
            ips.add(parts[0])
    return ips


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
                # stopping は destroy_env（stop_vm の完了待ち）の途中でまだ実体が
                # 残っている可能性が高いため、使用中とみなして含める。
                # 含めないと、停止処理中の IP を空きと誤認して二重割当てしてしまう恐れがある。
                Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping])
            )
        )
        # DB に記録されたIPのうちプール内のものだけを使用中とみなす
        db_used = {row[0] for row in result.fetchall() if row[0] and row[0] in pool_set}

        # ホストの実使用IP（ブリッジ上のARP/近隣テーブル）もプール内のものだけを
        # 考慮する（ゾンビ占有回避）
        real_used = {ip for ip in await _get_bridge_used_ips() if ip in pool_set}

        used = db_used | real_used
        for ip in pool:
            if ip not in used:
                await flush_record(ip)
                return ip
        return None


async def _run(cmd: list[str], check: bool = True, stdin: Optional[str] = None,
               want_stdout: bool = False) -> tuple[int, str]:
    """外部コマンドを実行する。

    戻り値は (returncode, text)。text は既定で stderr（エラーメッセージ用）だが、
    want_stdout=True のときは stdout を返す（list 系コマンドの出力取得用）。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=stdin.encode() if stdin is not None else None),
        timeout=30,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {stderr.decode().strip()}")
    return proc.returncode, (stdout if want_stdout else stderr).decode().strip()


async def _get_bridge_subnet() -> Optional[ipaddress.IPv4Network]:
    """ホストのブリッジインターフェースに割り当てられた IPv4 subnet を取得する。"""
    proc = await asyncio.create_subprocess_exec(
        "ip", "-4", "-o", "addr", "show", "dev", settings.BRIDGE_NAME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    for line in stdout.decode().splitlines():
        parts = line.split()
        for part in parts:
            if "/" in part:
                try:
                    return ipaddress.IPv4Network(part, strict=False)
                except ValueError:
                    continue
    return None


async def ensure_bridge_network() -> None:
    """ホストブリッジ（BRIDGE_NAME）の存在確認と IP プールの妥当性検証を行う。"""
    proc = await asyncio.create_subprocess_exec(
        "ip", "link", "show", settings.BRIDGE_NAME,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ブリッジ '{settings.BRIDGE_NAME}' が見つかりません。"
            " setup.sh で bridge ネットワークを構成してください。"
        )

    subnet = await _get_bridge_subnet()
    if subnet is None:
        logger.warning("ブリッジの subnet を取得できませんでした。IP プールの検証をスキップします。")
        return

    pool = _ip_pool()
    invalid = [ip for ip in pool if ipaddress.IPv4Address(ip) not in subnet]
    if invalid:
        raise RuntimeError(
            f"IP_POOL の一部がブリッジ subnet ({subnet}) の範囲外です: {invalid[:3]}{'...' if len(invalid) > 3 else ''}。"
            " .env の IP_POOL_START / IP_POOL_END を subnet 内のアドレスに修正してください。"
        )
    logger.info(f"IP pool validated: {pool[0]} - {pool[-1]} ({len(pool)} addresses) within {subnet}")


async def ensure_vm_network() -> None:
    """ホスト側のブリッジネットワークの妥当性を検証する。"""
    await ensure_bridge_network()


async def release_ip(ip: str) -> None:
    pass


# ---- Firecracker バックエンド用 TAP 操作 ----

def tap_name(vm_id: str) -> str:
    return f"tap-{vm_id[:8]}"


def _nft_table_name(vm_id: str) -> str:
    """VM ごとの nftables テーブル名（bridge family）。

    テーブル単位で作成・削除できるため、停止時は delete table 一発で
    その VM の全ルールをアトミックに除去できる。
    """
    return f"hb_{vm_id[:8]}"


def ip_to_mac(ip: str) -> str:
    """IP アドレスから決定論的な MAC アドレスを生成する（06:00:xx:xx:xx:xx）。

    smolvm._ip_to_mac と同一の規則。network.py 側でも anti-spoof ルールの
    許可 MAC を自前で導出できるよう、循環 import を避けて複製している。
    """
    parts = ip.split(".")
    return "06:00:{:02x}:{:02x}:{:02x}:{:02x}".format(*map(int, parts))


async def _apply_tap_antispoof(vm_id: str, tap: str, ip: str) -> None:
    """TAP から入るフレームを「割り当て IP / MAC」のみに制限する nftables ルールを適用する。

    ホスト側（orchestrator コンテナは network_mode: host + NET_ADMIN）で bridge family の
    専用テーブルを作り、prerouting フックで該当 TAP からの入力だけを検査する:
      - 送信元 MAC が期待値と異なる     → drop（MAC なりすまし）
      - ARP の送信元 IP が割り当てと異なる → drop（ARP スプーフィング）
      - IPv4 の送信元 IP が割り当てと異なる → drop（IP なりすまし）
    その TAP 以外のトラフィックには一切影響しない（iif で限定）。

    nft が無い/失敗した場合は警告にとどめ、VM 起動自体はブロックしない
    （ネットワーク分離は多層で担保する方針。将来 jailer/ネットワーク namespace も追加）。
    """
    table = _nft_table_name(vm_id)
    mac = ip_to_mac(ip)
    # 1トランザクションで投入（途中失敗で半端なテーブルが残らないよう stdin 経由）
    ruleset = f"""
table bridge {table} {{
    chain input {{
        type filter hook prerouting priority -300; policy accept;
        iif != "{tap}" accept
        ether saddr != {mac} drop
        arp saddr ip != {ip} drop
        ip saddr != {ip} drop
    }}
}}
"""
    rc, err = await _run(["nft", "-f", "-"], check=False, stdin=ruleset)
    if rc != 0:
        logger.warning(f"TAP anti-spoof ルールの適用に失敗しました（スキップ）: tap={tap} ip={ip}: {err}")
    else:
        logger.info(f"TAP anti-spoof 適用: tap={tap} ip={ip} mac={mac} table=bridge/{table}")


async def _remove_tap_antispoof(vm_id: str) -> None:
    """VM の anti-spoof テーブルを削除する（存在しなくてもエラーにしない）。"""
    table = _nft_table_name(vm_id)
    rc, _ = await _run(["nft", "delete", "table", "bridge", table], check=False)
    if rc == 0:
        logger.info(f"TAP anti-spoof 削除: table=bridge/{table}")


async def cleanup_orphaned_antispoof(active_vm_ids: set[str]) -> None:
    """起動時に、対応する VM が存在しない anti-spoof テーブル（bridge hb_*）を掃除する。

    オーケストレータ異常終了時などに delete_tap が呼ばれず、ルールだけが
    残るケースを回収する。active_vm_ids に含まれる VM のテーブルは残す。
    """
    rc, out = await _run(["nft", "-a", "list", "tables", "bridge"], check=False, want_stdout=True)
    if rc != 0:
        return
    active_tables = {_nft_table_name(v) for v in active_vm_ids}
    for line in out.splitlines():
        # 例: "table bridge hb_1a2b3c4d # handle 5"
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "table" and parts[1] == "bridge":
            name = parts[2]
            if name.startswith("hb_") and name not in active_tables:
                rc2, _ = await _run(["nft", "delete", "table", "bridge", name], check=False)
                if rc2 == 0:
                    logger.info(f"残留した anti-spoof テーブルを削除しました: bridge/{name}")


async def create_tap(vm_id: str, ip: str) -> str:
    name = tap_name(vm_id)
    await _run(["ip", "tuntap", "add", name, "mode", "tap"])
    try:
        await _run(["ip", "link", "set", name, "master", settings.BRIDGE_NAME])
        await _run(["ip", "link", "set", name, "up"])
        # 割り当て IP/MAC 以外の送信元を名乗るフレームをホスト側で遮断する
        await _apply_tap_antispoof(vm_id, name, ip)
    except Exception:
        # master 設定や up に失敗すると TAP が host 上に取り残され、
        # 例外発生時点では呼び出し元の create_tap() が未返却＝tap=None のため
        # _start_firecracker の例外ハンドラからも delete_tap が呼ばれない。
        # ここで確実に削除しておく（nft テーブルも含む）。
        await _remove_tap_antispoof(vm_id)
        await _run(["ip", "link", "del", name], check=False)
        raise
    logger.info(f"TAP created: {name} (master={settings.BRIDGE_NAME})")
    return name


async def delete_tap(vm_id: str) -> None:
    name = tap_name(vm_id)
    # anti-spoof テーブルを先に除去（TAP が消えてもルールは残るため）
    await _remove_tap_antispoof(vm_id)
    rc, _ = await _run(["ip", "link", "del", name], check=False)
    if rc == 0:
        logger.info(f"TAP deleted: {name}")
    else:
        logger.warning(f"TAP delete failed (may already be gone): {name}")
