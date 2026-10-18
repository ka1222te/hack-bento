"""rootfs tar と ext4 の相互変換、および資産ビルド共通の小さな実行ヘルパー。

services/firecracker_setup.py から分離した責務のひとつ。他の資産ビルド処理
（デフォルト rootfs・docker-base・Attack Machine・rootfs 変換専用ベース）が
共通して使う _run/_run_to_file/_docker_available もここに置く。

アーカイブ種別判定（_detect_archive_kind）・tar→ext4 変換（convert_tar_to_ext4）は
他モジュール（routers/images.py, routers/admin.py, services/rootfs_convert.py,
services/bundle_build.py）から直接 import されるため、関数名はそのまま維持する。
"""
import asyncio
import os
import shutil
import subprocess
import tarfile
import tempfile

# rootfs tar → ext4 変換の既定サイズ（ホスト上展開版・VM経由版の双方で使う既定値）。
ROOTFS_SIZE_MB = 512

_DOCKER_SOCK = "/var/run/docker.sock"


def docker_available() -> bool:
    return os.path.exists(_DOCKER_SOCK)


async def run(cmd: list[str], timeout: int = 600) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {stderr.decode().strip()}")


async def run_to_file(cmd: list[str], dest_path: str, timeout: int = 600) -> None:
    """cmd の標準出力をメモリに溜め込まず dest_path へストリーミングで書き出す。

    大きな tar の `docker export` などをメモリに載せないために使用する。
    """
    with open(dest_path, "wb") as f:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=f,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {stderr.decode().strip()}")


def run_sync(cmd: list[str]) -> None:
    """同期的にコマンドを実行する（run_in_executor 経由で呼ぶための小さなラッパー）。"""
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {result.stderr.decode(errors='replace').strip()}")


async def debugfs_cat(ext4_path: str, filename: str) -> str:
    """debugfs 経由で ext4 イメージ内のファイルを読む（loop マウント不要）。"""
    proc = await asyncio.create_subprocess_exec(
        "debugfs", "-R", f"cat /{filename}", ext4_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


def _resolve_within(dest_real: str, norm_path: str) -> str:
    """dest_real（展開先の実パス）を基準に norm_path を1階層ずつ解決し、実パスを返す。

    途中の階層が既存のシンボリックリンクであれば os.path.realpath と同様に解決するが、
    「解決結果が dest_real の外に出た時点で例外を上げる」点が os.path.realpath と異なる
    （realpath は外に出ても黙って解決してしまい、呼び出し後に文字列比較するしかない。
    ここでは1階層ごとに検証するため、最終コンポーネント自身がシンボリックリンクで
    外を指す場合も確実に捕まえる）。
    """
    current = dest_real
    parts = [p for p in norm_path.split(os.sep) if p not in ("", ".")]
    for part in parts:
        candidate = os.path.join(current, part)
        if os.path.islink(candidate):
            link_target = os.readlink(candidate)
            if os.path.isabs(link_target):
                resolved = os.path.normpath(link_target)
            else:
                resolved = os.path.normpath(os.path.join(current, link_target))
            # リンク先自体がさらにリンクの場合に備え realpath で完全解決する。
            # ただし起点は必ず current（展開先内の既知の位置）に固定しているため、
            # ホスト上の無関係な絶対パスへは、リンク先が展開先の中を指す限り出ない。
            resolved = os.path.realpath(resolved) if os.path.exists(resolved) else resolved
            if not (resolved == dest_real or resolved.startswith(dest_real + os.sep)):
                raise tarfile.TarError(f"展開先の外を指すシンボリックリンクです: {part} -> {link_target}")
            current = resolved
        else:
            current = candidate
    return current


def safe_extract_tar(tar_path: str, dest_dir: str) -> None:
    """信用できない tar を安全に展開する（ブロッキングI/O・executor経由で呼ぶこと）。

    方針: シンボリックリンクの *作成* 自体は許可する（リンク先がどこを指す文字列であれ、
    作成時点では何も読み書きしないため無害）。Alpine/Debian 等の実在の rootfs は
    `/bin/ls -> /bin/busybox` のような絶対パスのシンボリックリンクを標準的に含むため、
    tarfile の filter="data" のように絶対パスリンクを一律拒否すると正当な rootfs の
    変換そのものが壊れる。
    その代わり、通常ファイル/ディレクトリを書き込む *直前* に、その書き込み先の親ディレクトリを
    1階層ずつ解決し、既存のシンボリックリンクを経由して展開先ディレクトリの外に出ていないかを
    検証する（symlink traversal 対策）。エントリ名自体の絶対パス・親参照（../）は常に拒否する。
    """
    dest_real = os.path.realpath(dest_dir)
    os.makedirs(dest_real, exist_ok=True)

    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            norm = os.path.normpath(m.name)
            if m.name.startswith("/") or os.path.isabs(norm) or norm.startswith(".."):
                raise tarfile.TarError(f"不正なパスを含むエントリです: {m.name}")
            if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise tarfile.TarError(f"デバイス/FIFOファイルを含むエントリです: {m.name}")

            parent_norm = os.path.dirname(norm)
            # 親ディレクトリを1階層ずつ解決し、途中の symlink が外を指していないか検証する。
            # （エントリ自身の名前がまだ存在しない通常のケースなので、親までを検証すれば十分）
            parent_real = _resolve_within(dest_real, parent_norm) if parent_norm else dest_real
            target_path = os.path.join(parent_real, os.path.basename(norm))

            if m.issym():
                # シンボリックリンクの作成自体は許可する（中身はリンク先の文字列のみで、
                # 作成時点では何も読み書きしない）。既存の同名パスがあれば削除してから作る。
                os.makedirs(parent_real, exist_ok=True)
                if os.path.lexists(target_path):
                    os.remove(target_path)
                os.symlink(m.linkname, target_path)
                continue
            if m.islnk():
                # ハードリンクは tar 内の別エントリを指す。リンク先が展開先の内側にあるかのみ検証。
                link_norm = os.path.normpath(m.linkname)
                if os.path.isabs(m.linkname) or link_norm.startswith(".."):
                    raise tarfile.TarError(f"展開先の外を指すハードリンクです: {m.name} -> {m.linkname}")
                link_target_real = _resolve_within(dest_real, link_norm)
                if not os.path.exists(link_target_real):
                    raise tarfile.TarError(f"ハードリンクの参照先が見つかりません: {m.name} -> {m.linkname}")
                os.makedirs(parent_real, exist_ok=True)
                if os.path.lexists(target_path):
                    os.remove(target_path)
                os.link(link_target_real, target_path)
                continue

            if m.isdir():
                os.makedirs(target_path, exist_ok=True)
                continue

            # 通常ファイル等。tarfile に実体の書き込みを任せるが、展開先は検証済みの実パスにする。
            os.makedirs(parent_real, exist_ok=True)
            if os.path.lexists(target_path) and not os.path.isdir(target_path):
                os.remove(target_path)
            if m.isfile():
                with tf.extractfile(m) as src, open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(target_path, m.mode & 0o7777)
            # isfile 以外（ソケット等）は無視する（ゲスト rootfs に不要かつ扱いが煩雑なため）


# rootfs tar → ext4 変換で使う scratch（展開先）のサイズを、入力 tar の圧縮サイズから
# 事前計算するための倍率・余白。tar は展開すると（圧縮効率次第で）数倍に増えることがあるため、
# 十分に大きい倍率を取った上で固定の余白も足す。実サイズに応じた動的な再計算は行わない
# （ビルド用VMの中では事前にサイズを確定したディスクが必要なため）。
_ROOTFS_CONVERT_SIZE_MULTIPLIER = 4
_ROOTFS_CONVERT_SIZE_MARGIN_MB = 256


async def convert_tar_to_ext4(tar_path: str, dest_path: str, size_mb: int = ROOTFS_SIZE_MB) -> None:
    """信用できない rootfs tar（ユーザアップロード）を、ビルド用VMの中で安全に展開し
    ext4 イメージへ変換する。dest_path へ os.replace する。

    tar のパース・展開処理そのものをネットワークなしの使い捨てVMの中で実行するため、
    tar の内容（symlink traversal に限らず、tar パーサー自体の未知の脆弱性を含む）が
    ホストに影響することはない。

    手順:
      1. 入力 tar の圧縮サイズから、十分な余白を持つ scratch（展開先）サイズを事前計算
      2. scratch（空の ext4）・input（tar の生バイト列を書き込んだブロックデバイス）・
         status（結果報告用の小さな ext4）をホスト上に用意
      3. rootfs 変換専用ベース（vm_assets.ROOTFS_CONVERTER_BASE_PATH）をベースに
         image_build.run_build_vm を実行。ゲストの中で input を展開して scratch に
         書き込み、status に結果を報告する
      4. status を読んで成否判定。成功なら scratch を dest_path として回収する
    """
    from services import image_build
    from services.vm_assets import DEFAULT_KERNEL_PATH, ROOTFS_CONVERTER_BASE_PATH

    if not os.path.exists(ROOTFS_CONVERTER_BASE_PATH):
        raise RuntimeError(
            "rootfs 変換専用ベースが見つかりません（サーバの初期化が完了していない可能性があります）: "
            f"{ROOTFS_CONVERTER_BASE_PATH}"
        )

    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-convert-")
    try:
        input_disk = os.path.join(tmpdir, "input.raw")
        scratch_disk = os.path.join(tmpdir, "scratch.ext4")
        status_disk = os.path.join(tmpdir, "status.ext4")

        # 1. scratch サイズを tar の圧縮サイズから事前計算する
        try:
            tar_size = os.path.getsize(tar_path)
        except OSError as e:
            raise RuntimeError(f"rootfs tar が読み取れません: {e}")
        auto_mb = int(tar_size * _ROOTFS_CONVERT_SIZE_MULTIPLIER / (1024 * 1024)) + _ROOTFS_CONVERT_SIZE_MARGIN_MB
        image_mb = max(size_mb, auto_mb)

        # 2. 各ディスクを用意する（いずれもブロッキングI/O。executor 経由で実行）
        loop = asyncio.get_event_loop()

        def _prepare_disks() -> None:
            # input: tar の生バイト列をそのままコピーしたファイル（ブロックデバイスとして渡す）
            shutil.copy2(tar_path, input_disk)
            # scratch: 空の ext4（ここに tar を展開した結果がそのまま最終 rootfs になる）
            run_sync(["dd", "if=/dev/zero", f"of={scratch_disk}", "bs=1M", f"count={image_mb}"])
            run_sync(["mkfs.ext4", "-F", "-O", "^metadata_csum", scratch_disk])
            # status: 結果報告用の小さな ext4
            run_sync(["dd", "if=/dev/zero", f"of={status_disk}", "bs=1M", "count=4"])
            run_sync(["mkfs.ext4", "-F", "-O", "^metadata_csum", status_disk])

        await loop.run_in_executor(None, _prepare_disks)

        # 3. ビルド用VMで変換を実行する
        try:
            await image_build.run_build_vm(
                kernel_path=DEFAULT_KERNEL_PATH,
                base_rootfs_path=ROOTFS_CONVERTER_BASE_PATH,
                scratch_path=scratch_disk,
                input_disk_path=input_disk,
                status_disk_path=status_disk,
                boot_marker="hb_rootfs_convert=1",
                result_rootfs_path=dest_path,
                result_from_scratch=True,
            )
        except image_build.BuildVMError as e:
            raise RuntimeError(f"rootfs 変換用VMの実行に失敗しました: {e}")

        # 4. status を読んで成否判定する（debugfs 経由。loop マウント不要）
        status_text = await debugfs_cat(status_disk, "status")
        if status_text.strip() != "0":
            error_text = await debugfs_cat(status_disk, "error")
            log_text = await debugfs_cat(status_disk, "convert.log")
            detail = error_text.strip() or log_text.strip()[-400:] or f"status={status_text.strip()!r}"
            # 失敗時は dest_path に書き込まれてしまった中途半端な成果物を削除する
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
            raise RuntimeError(f"rootfs tar の変換に失敗しました: {detail}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def convert_tar_to_ext4_host_unsafe(tar_path: str, dest_path: str, size_mb: int = ROOTFS_SIZE_MB) -> None:
    """単純なファイルシステム tar を、ホスト上で直接展開して ext4 イメージへ変換する。

    「_host_unsafe」の名の通り、信用できない（ユーザがアップロードした）tar には
    絶対に使わないこと。tar のパース・展開処理自体をホストのプロセスとして実行するため、
    symlink traversal 対策（safe_extract_tar）でカバーしていない未知の攻撃面
    （tar パーサー自体の脆弱性等）にはこの関数単体では対処できない。
    用途は HackBento 自身が管理者操作（docker.sock 経由）で生成する、内容を把握できる
    tar の変換に限定する（現時点では rootfs 変換専用ベース自身の初回ビルドのみ）。
    ユーザがアップロードする rootfs tar の変換は convert_tar_to_ext4（ビルド用VM経由）を使うこと。

    mount/loop デバイスを使わず mkfs.ext4 -d で直接構築する。
    イメージサイズは展開後のディレクトリ実サイズを元に自動計算し、固定値より大きい場合はそちらを使う。
    """
    tmpdir = tempfile.mkdtemp(prefix="hackbento-rootfs-convert-")
    try:
        rootfs_root = os.path.join(tmpdir, "rootfs")
        os.makedirs(rootfs_root, exist_ok=True)
        # tar 展開はブロッキングI/O（数百MB規模になり得る）のため event loop を専有しないよう
        # executor で実行する。かつ symlink traversal 対策を適用する。
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, safe_extract_tar, tar_path, rootfs_root
            )
        except tarfile.TarError as e:
            raise RuntimeError(f"rootfs tar の展開に失敗しました（不正な構造の可能性があります): {e}")
        for d in ("proc", "sys", "dev", "tmp"):
            os.makedirs(os.path.join(rootfs_root, d), exist_ok=True)

        # 展開後の実サイズを計測し、20% + 64MB のオーバーヘッドを加えたサイズを使う
        du_result = await asyncio.create_subprocess_exec(
            "du", "-sb", rootfs_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        du_stdout, _ = await du_result.communicate()
        actual_bytes = int(du_stdout.split()[0]) if du_stdout.strip() else 0
        auto_mb = int(actual_bytes * 1.2 / (1024 * 1024)) + 64
        image_mb = max(size_mb, auto_mb)

        tmp_image = dest_path + ".tmp"
        if os.path.exists(tmp_image):
            os.remove(tmp_image)
        await run(["dd", "if=/dev/zero", f"of={tmp_image}", "bs=1M", f"count={image_mb}"])
        # metadata_csum を無効化してから -d で populate する。
        # libext2fs の populate 経路は大きい/深いツリー（docker export された Debian 等）で
        # "Directory block checksum does not match" を起こすことがあり、
        # ディレクトリブロックのチェックサム機能を切ることで回避する。
        # （loop マウントでの展開は CAP_SYS_ADMIN が必要で、このコンテナでは使えない）
        await run([
            "mkfs.ext4", "-F",
            "-O", "^metadata_csum",
            "-d", rootfs_root,
            tmp_image,
        ])
        os.replace(tmp_image, dest_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def detect_archive_kind(path: str) -> str:
    """アップロードされたファイルの種類を判定する。

    戻り値: "ext4"（変換不要・そのまま利用可能） / "tar"（単純なファイルシステムtar・変換対象）
            / "docker-save"（複数レイヤーのOCIアーカイブ・非対応）
            / "compose-bundle"（hackbento-pack の Compose バンドル・ビルド用VMで組み立て）
    """
    import struct

    try:
        with open(path, "rb") as f:
            f.seek(1024 + 56)
            magic = f.read(2)
        if struct.unpack("<H", magic)[0] == 0xEF53:
            return "ext4"
    except (OSError, struct.error):
        pass

    if tarfile.is_tarfile(path):
        try:
            with tarfile.open(path) as tf:
                names = {os.path.normpath(n).lstrip("./") for n in tf.getnames()}
        except tarfile.TarError:
            return "tar"
        # Compose バンドル（目印: hackbento-bundle.json + images.tar + app/）
        if "hackbento-bundle.json" in names and "images.tar" in names \
           and any(n == "app" or n.startswith("app/") for n in names):
            return "compose-bundle"
        if "manifest.json" in names and "repositories" in names:
            return "docker-save"
        return "tar"

    return "ext4"
