"""ビルド用 VM 基盤（Phase 4）。

信用できない入力（アップロードされた tar / バンドル）を、ホスト上で直接展開せず、
ネットワークなしの使い捨て Firecracker microVM の中で処理するための土台。

本番 VM 起動（smolvm._start_firecracker）とは別系統の軽量ランチャーとして実装する:
  - TAP / network-interface を作らない（完全にネットワークなし）
  - IP プールを消費しない・Environment レコードを作らない（本番と枠を奪い合わない）
  - 2 本目のドライブ（scratch）にバンドル/作業用ディスクを渡す
  - ゲストが自分で電源を落とす（reboot/poweroff）→ Firecracker プロセス終了を検知
  - ハードタイムアウトを必ず課し、ハング時は確実に kill

smolvm の低レベルヘルパ（cgroup・Firecracker API・プロセス終了）を再利用する。
cgroup だけはビルド専用サブツリーに隔離し、本番 VM のリソース会計と干渉させない。
"""
import asyncio
import os
import shutil
import uuid
import logging

from services import smolvm

logger = logging.getLogger(__name__)

# ビルド用 VM の chroot/socket/rootfs を置くベース（本番 VM とは別ディレクトリ）
BUILD_WORK_DIR = "/data/images/vm/build-work"
# 同時に走るビルド VM の上限（本番 VM と資源を奪い合わないよう絞る）
MAX_CONCURRENT_BUILDS = 1
_build_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BUILDS)

# ビルド VM は本番より長く走り得る（docker load 等）。ただし必ず上限を課す。
DEFAULT_BUILD_TIMEOUT_S = 900  # 15 分


class BuildVMError(RuntimeError):
    pass


def _build_socket_path(bid: str) -> str:
    return os.path.join(BUILD_WORK_DIR, bid, "firecracker.socket")


def _build_rootfs_path(bid: str) -> str:
    return os.path.join(BUILD_WORK_DIR, bid, "rootfs.ext4")


def _build_log_path(bid: str) -> str:
    return os.path.join(BUILD_WORK_DIR, bid, "fc.log")


def _prepare_build_rootfs(bid: str, base_rootfs_path: str) -> str:
    """ベース rootfs を書き込み可能なコピーとしてビルド作業ディレクトリへ複製する。"""
    dst = _build_rootfs_path(bid)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    shutil.copy2(base_rootfs_path, tmp)
    os.replace(tmp, dst)
    return dst


def cleanup_stale_build_work() -> None:
    """起動時に、前回のビルド VM が残した作業ディレクトリをすべて削除する。

    起動直後は稼働中のビルド VM は存在しないため、BUILD_WORK_DIR 配下の
    build-* ディレクトリは全て残骸とみなして削除する。
    """
    try:
        entries = os.listdir(BUILD_WORK_DIR)
    except OSError:
        return
    for name in entries:
        path = os.path.join(BUILD_WORK_DIR, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            logger.info(f"残留したビルド作業ディレクトリを削除しました: {name}")


def _cleanup_build(bid: str) -> None:
    """ビルド作業ディレクトリ（rootfs コピー・ソケット・ログ）をまるごと削除する。

    scratch ディスクは呼び出し側が所有・回収するため、ここでは触らない。
    """
    work = os.path.join(BUILD_WORK_DIR, bid)
    try:
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
    except Exception:
        pass


async def run_build_vm(
    kernel_path: str,
    base_rootfs_path: str,
    scratch_path: str,
    cpu: int = 1,
    memory_mb: int = 1024,
    timeout_s: int = DEFAULT_BUILD_TIMEOUT_S,
    boot_marker: str = "hb_build=1",
    result_rootfs_path: str | None = None,
    input_disk_path: str | None = None,
    result_from_scratch: bool = False,
    status_disk_path: str | None = None,
) -> None:
    """ネットワークなしのビルド VM を起動し、ゲストが電源を落とすまで待つ。

    - kernel_path      : ゲストカーネル（本番と同じものを共有可）
    - base_rootfs_path : ベース rootfs（docker-base 等）。書き込み可能コピーを作って使う
    - scratch_path     : 2 本目のドライブ（ext4 / 生ディスク）。バンドル投入・結果回収に使う。
                         VM からは /dev/vdb として見える。呼び出し側が事前に用意し、後で読む。
    - timeout_s        : ハードタイムアウト。超過したら VM を強制終了して BuildVMError を送出。
    - boot_marker      : ゲスト init に「ビルドモード」を伝えるカーネル引数。
    - result_rootfs_path: 指定すると、正常終了後にビルド済み rootfs コピー（1本目のディスク。
                         docker-base にバンドルを焼き込んだもの等）をこのパスへ移動して残す。
                         result_from_scratch=True の場合は代わりに scratch（2本目）を回収する。
                         未指定なら何も回収せず破棄する。
    - input_disk_path  : 指定すると 3 本目のドライブ（読み取り専用）として渡す。VM からは
                         /dev/vdc として見える。信用できない入力（rootfs tar 等）を、書き込み
                         先の scratch とは別のディスクとして渡したい場合に使う。
    - result_from_scratch: True の場合、成果物の回収元を 1 本目（base_rootfs のコピー）ではなく
                         2 本目（scratch）にする。rootfs 変換（scratch 自体を最終 rootfs として
                         VM 内で直接書き込む用途）向け。
    - status_disk_path : 指定すると 4 本目のドライブ（書き込み可能）として渡す。VM からは
                         /dev/vdd として見える。scratch を最終成果物としてそのまま回収する
                         場合、ステータス/ログ/展開後サイズ等の「おまけ情報」を scratch に
                         書くと成果物に混入してしまうため、別ディスクに分離するために使う。
                         呼び出し側が事前に空の ext4 を用意し、終了後に自分で読む。

    正常終了（ゲストが自分で poweroff/reboot）した場合は None を返す。
    異常（タイムアウト・起動失敗）時は BuildVMError。
    """
    if not kernel_path or not os.path.exists(kernel_path):
        raise BuildVMError(f"ゲストカーネルが見つかりません: {kernel_path}")
    if not base_rootfs_path or not os.path.exists(base_rootfs_path):
        raise BuildVMError(f"ベース rootfs が見つかりません: {base_rootfs_path}")
    if not scratch_path or not os.path.exists(scratch_path):
        raise BuildVMError(f"scratch ディスクが見つかりません: {scratch_path}")
    if input_disk_path is not None and not os.path.exists(input_disk_path):
        raise BuildVMError(f"入力ディスクが見つかりません: {input_disk_path}")
    if status_disk_path is not None and not os.path.exists(status_disk_path):
        raise BuildVMError(f"ステータスディスクが見つかりません: {status_disk_path}")
    if not os.path.exists("/dev/kvm"):
        raise BuildVMError("/dev/kvm が存在しません。KVM が有効か確認してください。")

    async with _build_semaphore:
        bid = "build-" + uuid.uuid4().hex[:12]
        # cgroup は本番 VM と同じ委譲ツリーを使うが、キー（bid）で区別する。
        # bid は "build-" 前缀付きなので本番 vm_id（UUID）と衝突しない。
        socket_path = _build_socket_path(bid)
        loop = asyncio.get_event_loop()
        rootfs_copy = await loop.run_in_executor(
            None, _prepare_build_rootfs, bid, base_rootfs_path
        )

        fc_proc = None
        try:
            smolvm._create_vm_cgroup(bid, cpu, memory_mb)

            fc_proc = await asyncio.create_subprocess_exec(
                "firecracker",
                "--api-sock", socket_path,
                "--log-path", _build_log_path(bid),
                "--level", "Warning",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            smolvm._fc_pids[bid] = fc_proc.pid
            smolvm._attach_pid_to_cgroup(bid, fc_proc.pid)
            await smolvm._fc_wait_socket(socket_path, timeout=10.0)

            # ネットワークなし。boot_marker でゲスト init にビルドモードを伝える。
            await smolvm._fc_api(socket_path, "PUT", "/boot-source", {
                "kernel_image_path": kernel_path,
                "boot_args": (
                    f"console=ttyS0 reboot=k panic=1 pci=off {boot_marker} rw"
                ),
            })
            # 1 本目: 書き込み可能なベース rootfs コピー（/dev/vda）
            await smolvm._fc_api(socket_path, "PUT", "/drives/rootfs", {
                "drive_id": "rootfs",
                "path_on_host": rootfs_copy,
                "is_root_device": True,
                "is_read_only": False,
            })
            # 2 本目: scratch ディスク（/dev/vdb）。バンドル投入・結果回収用。
            await smolvm._fc_api(socket_path, "PUT", "/drives/scratch", {
                "drive_id": "scratch",
                "path_on_host": scratch_path,
                "is_root_device": False,
                "is_read_only": False,
            })
            if input_disk_path is not None:
                # 3 本目: 読み取り専用の入力ディスク（/dev/vdc）。信用できない入力
                # （rootfs tar 等）を、書き込み先の scratch とは分離して渡す。
                await smolvm._fc_api(socket_path, "PUT", "/drives/input", {
                    "drive_id": "input",
                    "path_on_host": input_disk_path,
                    "is_root_device": False,
                    "is_read_only": True,
                })
            if status_disk_path is not None:
                # 4 本目: 書き込み可能なステータス専用ディスク（/dev/vdd）。ステータス・
                # ログ・展開後サイズ等を、成果物として回収する scratch と分離して書く。
                await smolvm._fc_api(socket_path, "PUT", "/drives/status", {
                    "drive_id": "status",
                    "path_on_host": status_disk_path,
                    "is_root_device": False,
                    "is_read_only": False,
                })
            await smolvm._fc_api(socket_path, "PUT", "/machine-config", {
                "vcpu_count": cpu,
                "mem_size_mib": memory_mb,
                "smt": False,
            })
            await smolvm._fc_api(socket_path, "PUT", "/actions", {"action_type": "InstanceStart"})
            logger.info(f"ビルド VM 起動: bid={bid} rootfs={base_rootfs_path} scratch={scratch_path}")

            # ゲストが自分で電源を落とす（poweroff/reboot）→ fc_proc が終了する。
            # ハードタイムアウトを必ず課す（ゲストがハングしても無期限待ちしない）。
            try:
                await asyncio.wait_for(fc_proc.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                raise BuildVMError(
                    f"ビルド VM が {timeout_s} 秒以内に終了しませんでした（タイムアウト）: bid={bid}"
                )
            if fc_proc.returncode not in (0, None):
                logger.warning(f"ビルド VM の終了コードが非ゼロ: bid={bid} rc={fc_proc.returncode}")
            logger.info(f"ビルド VM 正常終了: bid={bid}")

            # 成果物を残す場合はここで回収する（VM は既に終了しファイルは安定している）。
            # result_from_scratch=True なら 2 本目（scratch）を、そうでなければ
            # 1 本目（base_rootfs の書き込み可能コピー）を回収元にする。
            if result_rootfs_path is not None:
                source = scratch_path if result_from_scratch else rootfs_copy
                os.makedirs(os.path.dirname(result_rootfs_path), exist_ok=True)
                tmp = result_rootfs_path + ".tmp"
                if os.path.exists(tmp):
                    os.remove(tmp)
                if result_from_scratch:
                    # scratch は呼び出し側が所有するファイルなのでコピーする（移動しない）。
                    await loop.run_in_executor(None, shutil.copy2, source, tmp)
                else:
                    await loop.run_in_executor(None, shutil.move, source, tmp)
                os.replace(tmp, result_rootfs_path)

        finally:
            pid = smolvm._fc_pids.pop(bid, None)
            if fc_proc is not None and fc_proc.returncode is None:
                try:
                    fc_proc.kill()
                    await fc_proc.wait()
                except Exception:
                    pass
            # 取りこぼし PID の保険
            if pid is not None and smolvm._pid_alive(pid):
                await smolvm._terminate_pid(pid, bid)
            smolvm._remove_vm_cgroup(bid)
            _cleanup_build(bid)
