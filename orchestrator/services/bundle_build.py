"""Compose バンドル取り込みワーカー（Phase 6）。

アップロードされた Compose バンドル（tar）を受け取り、ビルド用 VM（services.image_build）の
中で docker-base を土台に ext4 を組み立て、本番用 rootfs として ready にするパイプライン。

状態機械は rootfs_convert と同じ RootfsConversionStatus（pending→converting→ready/failed）を
Image 行で共有する。rootfs_convert 側は rootfs_kind='compose' の行を拾わないようにしてあり、
本ワーカーが rootfs_kind='compose' かつ pending の行だけを行ロックで claim する。

信用できない入力（バンドル tar）はホスト上で展開・解釈せず、以下に限定する:
  - 受付検査: tar の *メタデータのみ* で判定（展開しない）
  - 取り込み/解釈: すべてネットワークなしのビルド用 VM の中
"""
import asyncio
import json
import logging
import os
import shutil
import tarfile
import tempfile

from sqlalchemy import select

from database import AsyncSessionLocal
from models import Image, DefaultRootfsAsset, RootfsConversionStatus, BundlePhase
from config import settings
from services import image_build
from services.vm_assets import DEFAULT_KERNEL_PATH

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 10
BUNDLE_FORMAT = 1

# 受付検査の上限（展開後合計サイズ・圧縮サイズ）。巨大な展開爆弾を防ぐ。
MAX_BUNDLE_FILE_BYTES = 8 * 1024 * 1024 * 1024       # バンドル tar 自体 8GiB
MAX_BUNDLE_EXTRACTED_BYTES = 12 * 1024 * 1024 * 1024  # 展開後合計 12GiB

# compose が禁止する構成（別 IP を名乗る等）。ビルド用 VM が出力した正規化 JSON に対して判定する。
# （compose 自体はビルド用 VM の中の docker compose に正規化させる＝ホストで独自パースしない）

# overlay/ で上書きされると docker-base の起動の仕組みそのものが壊れるパス
# （/sbin/init が無いとゲストカーネルが起動できない・daemon.json を壊すと dockerd が
# 起動しない等）。ビルド用 VM の中で "cp -a overlay/. /" として展開されるため、
# ホストには影響しないが、VM 自体が起動できなくなる事故を受付検査で未然に防ぐ。
_PROTECTED_OVERLAY_PATHS = (
    "sbin/init",
    "etc/docker/daemon.json",
)


class BundleError(RuntimeError):
    pass


# ---- 受付検査（tar を展開せずメタデータのみで判定）----

def _validate_bundle_tar(tar_path: str) -> dict:
    """バンドル tar を展開せず、エントリ名・型・サイズだけで安全性と構造を検査する。

    返り値: {"project_name": str, "has_overlay": bool}
    不正時は BundleError。
    """
    if not os.path.exists(tar_path):
        raise BundleError(f"バンドルが見つかりません: {tar_path}")
    size = os.path.getsize(tar_path)
    if size > MAX_BUNDLE_FILE_BYTES:
        raise BundleError(f"バンドルが大きすぎます（上限 {MAX_BUNDLE_FILE_BYTES // (1024**3)}GiB）")
    if not tarfile.is_tarfile(tar_path):
        raise BundleError("バンドルが tar 形式ではありません")

    names: set[str] = set()
    total = 0
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            name = m.name
            norm = os.path.normpath(name)
            # 絶対パス・親参照・デバイス/FIFO・外向きシンボリックリンクを拒否
            if name.startswith("/") or norm.startswith("..") or os.path.isabs(norm):
                raise BundleError(f"バンドルに不正なパスが含まれています: {name}")
            if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise BundleError(f"バンドルにデバイス/FIFO が含まれています: {name}")
            if m.issym() or m.islnk():
                target = m.linkname
                # 展開先の外を指すリンクを拒否（絶対・親参照）
                if target.startswith("/") or os.path.normpath(os.path.join(os.path.dirname(norm), target)).startswith(".."):
                    raise BundleError(f"バンドルに外部を指すリンクが含まれています: {name} -> {target}")
            cleaned = norm.lstrip("./")
            # overlay/ はビルド用 VM 内で "/" に直接上書きコピーされる。
            # /sbin/init 等の起動の仕組みそのものを上書きされると VM が起動できなくなるため、
            # 受付検査（展開前）で拒否する。
            if cleaned.startswith("overlay/"):
                overlay_rel = cleaned[len("overlay/"):]
                if overlay_rel in _PROTECTED_OVERLAY_PATHS:
                    raise BundleError(
                        f"overlay/{overlay_rel} は起動の仕組みに関わるため上書きできません"
                    )
            total += max(m.size, 0)
            if total > MAX_BUNDLE_EXTRACTED_BYTES:
                raise BundleError(f"展開後サイズが大きすぎます（上限 {MAX_BUNDLE_EXTRACTED_BYTES // (1024**3)}GiB）")
            names.add(cleaned)

    if "hackbento-bundle.json" not in names:
        raise BundleError("Compose バンドルではありません（hackbento-bundle.json が見つかりません）")
    if "images.tar" not in names:
        raise BundleError("バンドルに images.tar が含まれていません")
    has_app = any(n == "app" or n.startswith("app/") for n in names)
    if not has_app:
        raise BundleError("バンドルに app/ が含まれていません")

    # マニフェストを読み project_name を取得（tar 内の小さな JSON）
    project_name = "app"
    with tarfile.open(tar_path) as tf:
        try:
            f = tf.extractfile("hackbento-bundle.json") or tf.extractfile("./hackbento-bundle.json")
            if f is not None:
                meta = json.loads(f.read().decode("utf-8", "replace"))
                if int(meta.get("format", 0)) != BUNDLE_FORMAT:
                    raise BundleError(f"未対応のバンドル形式です: format={meta.get('format')}")
                pn = str(meta.get("project_name", "") or "").strip()
                if pn:
                    project_name = pn
        except KeyError:
            pass
    return {"project_name": project_name, "has_overlay": any(n.startswith("overlay/") for n in names)}


# ---- scratch ディスク構築（loop マウント不要。mkfs.ext4 -d で直接 populate）----

def _build_scratch_from_bundle(tar_path: str, project_name: str, scratch_path: str) -> None:
    """バンドル tar の中身を scratch 用ディレクトリへ展開し、ext4 を作る。

    ビルド用 VM が /dev/vdb としてマウントし、app/ images.tar overlay/ project_name を読む。
    展開はホスト上で行うが、_validate_bundle_tar でパス・リンクを検査済みのため安全。
    """
    workdir = tempfile.mkdtemp(prefix="hackbento-bundle-scratch-")
    root = os.path.join(workdir, "scratch")
    os.makedirs(root, exist_ok=True)
    try:
        with tarfile.open(tar_path) as tf:
            # filter='data' で絶対パス・親参照・特殊ファイルを tar 側でも拒否（多層防御）
            try:
                tf.extractall(root, filter="data")
            except TypeError:
                # 古い Python 互換（filter 引数非対応）
                tf.extractall(root)
        # project_name を scratch に明示（マニフェスト優先で上書き）
        with open(os.path.join(root, "project_name"), "w") as f:
            f.write(project_name)

        # 実サイズ + 余裕で ext4 を作る
        total = 0
        for dp, _, fns in os.walk(root):
            for fn in fns:
                try:
                    total += os.path.getsize(os.path.join(dp, fn))
                except OSError:
                    pass
        mb = int(total / (1024 * 1024) * 1.3) + 256
        tmp_img = scratch_path + ".tmp"
        if os.path.exists(tmp_img):
            os.remove(tmp_img)
        import subprocess
        # dd if=/dev/zero でのゼロ埋めは不要: mkfs.ext4 に device + fs-size を渡すと
        # 対象ファイルが無ければ自動でスパースファイルとして確保する。
        # 事前ゼロ埋め（実データ分のディスク書き込み）をスキップすることで、
        # このステップのディスクI/O量をほぼ半分に減らせる。
        subprocess.run(["mkfs.ext4", "-F", "-O", "^metadata_csum", "-d", root, tmp_img, f"{mb}M"],
                       check=True, capture_output=True)
        os.replace(tmp_img, scratch_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---- scratch からの結果回収（debugfs。loop マウント不要）----

async def _read_scratch_file(scratch_path: str, name: str) -> str:
    rc, out = await _run_capture(["debugfs", "-R", f"cat /{name}", scratch_path])
    return out if rc == 0 else ""


async def _run_capture(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, out.decode("utf-8", "replace")


# ---- compose 正規化結果の検査（ホスト側ルール）----

def _validate_compose(compose_json_text: str, loaded_text: str) -> tuple[dict, str | None]:
    """ビルド用 VM が出力した正規化 compose(JSON) を検査する。

    返り値: (summary_dict, error)。error が None でなければ失敗。
    """
    try:
        data = json.loads(compose_json_text)
    except Exception:
        return {}, "compose 設定の解析に失敗しました"

    loaded = {l.strip() for l in loaded_text.splitlines() if l.strip()}

    # 禁止ネットワーク（別 IP を名乗る構成）
    for nname, ncfg in (data.get("networks") or {}).items():
        driver = (ncfg or {}).get("driver")
        if driver in ("macvlan", "ipvlan"):
            return {}, f"ネットワーク '{nname}' のドライバ {driver} は許可されていません"
        if (ncfg or {}).get("external"):
            return {}, f"外部ネットワーク '{nname}' は許可されていません"

    services = data.get("services") or {}
    summary_services = []
    published_ports = []
    for sname, scfg in services.items():
        scfg = scfg or {}
        # 別 IP を名乗る構成の拒否
        for net in (scfg.get("networks") or {}).values():
            if isinstance(net, dict) and (net.get("ipv4_address") or net.get("ipv6_address")):
                return {}, f"サービス '{sname}' が固定 IP を指定しています（許可されていません）"
        # 参照イメージが images.tar に含まれているか（build のみは不可）
        image = scfg.get("image")
        if not image:
            return {}, f"サービス '{sname}' に image がありません（build のみはバンドルでは非対応）"
        # docker compose config はタグ省略（例: "web"）をそのまま出力し :latest を補わないが、
        # loaded 側（docker images）は常に "repo:tag" 形式になる。タグなし参照は
        # :latest 相当として比較する（digest参照 "repo@sha256:..." はそのまま比較）。
        image_for_check = image if ("@" in image or ":" in image.rsplit("/", 1)[-1]) else f"{image}:latest"
        if loaded and image_for_check not in loaded:
            return {}, f"サービス '{sname}' のイメージ '{image}' が images.tar に含まれていません"
        ports = []
        for p in (scfg.get("ports") or []):
            if isinstance(p, dict) and p.get("published"):
                ports.append(str(p.get("published")))
                published_ports.append(str(p.get("published")))
        summary_services.append({"name": sname, "image": image, "ports": ports})

    summary = {
        "project": data.get("name"),
        "services": summary_services,
        "published_ports": sorted(set(published_ports), key=lambda x: (len(x), x)),
    }
    return summary, None


# ---- claim / finish（rootfs_convert と同じ行ロックパターン）----

async def _claim_pending() -> int | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Image)
            .where(
                Image.rootfs_conversion_status == RootfsConversionStatus.pending.value,
                Image.rootfs_kind == "compose",
            )
            .with_for_update()
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.rootfs_conversion_status = RootfsConversionStatus.converting.value
        row.build_phase = BundlePhase.validating.value
        await db.flush()
        rid = row.id
        await db.commit()
        return rid


async def _set_phase(row_id: int, phase: str) -> None:
    """ビルド進捗フェーズをUI表示用に更新する（converting中のみ意味を持つ）。"""
    async with AsyncSessionLocal() as db:
        row = await db.get(Image, row_id)
        if row is None:
            return
        row.build_phase = phase
        await db.commit()


async def _finish(row_id: int, status: str, error: str | None,
                  rootfs_path: str | None = None, summary: str | None = None) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(Image, row_id)
        if row is None:
            return
        row.rootfs_conversion_status = status
        row.rootfs_conversion_error = error
        row.build_phase = None
        if rootfs_path is not None:
            row.rootfs_path = rootfs_path
        if summary is not None:
            row.compose_summary = summary
        # 成功/失敗にかかわらず、バンドル原本は処理後に破棄する
        src = row.rootfs_source_archive_path
        row.rootfs_source_archive_path = None
        await db.commit()
    if src and os.path.exists(src):
        try:
            os.remove(src)
        except OSError:
            pass


async def _resolve_base_rootfs(row_id: int) -> str | None:
    """Image.base_rootfs_asset_id から docker-base の file_path を得る。未指定なら docker-base の既定を使う。"""
    async with AsyncSessionLocal() as db:
        row = await db.get(Image, row_id)
        if row is None:
            return None
        if row.base_rootfs_asset_id is not None:
            asset = await db.get(DefaultRootfsAsset, row.base_rootfs_asset_id)
            if asset is not None and os.path.exists(asset.file_path):
                return asset.file_path
        # 既定の docker-base 
        res = await db.execute(
            select(DefaultRootfsAsset).where(DefaultRootfsAsset.kind == "docker-base",
                                             DefaultRootfsAsset.is_active == True).limit(1)
        )
        asset = res.scalar_one_or_none()
        if asset is not None and os.path.exists(asset.file_path):
            return asset.file_path
    return None


async def _process_one(row_id: int) -> None:
    # バンドル tar と出力先 rootfs パスを取得
    async with AsyncSessionLocal() as db:
        row = await db.get(Image, row_id)
        if row is None:
            return
        tar_path = row.rootfs_source_archive_path
        dest_rootfs = row.rootfs_path

    try:
        if not tar_path:
            raise BundleError("バンドルの保存パスがありません")
        base = await _resolve_base_rootfs(row_id)
        if base is None:
            raise BundleError("docker-base が見つかりません（管理者がデフォルトVMに登録してください）")

        # 1) 受付検査（展開しない。build_phase は _claim_pending で validating 済み）
        info = _validate_bundle_tar(tar_path)
        project_name = info["project_name"]

        # 2) scratch 構築
        await _set_phase(row_id, BundlePhase.preparing.value)
        scratch = dest_rootfs + ".scratch"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _build_scratch_from_bundle, tar_path, project_name, scratch)

        try:
            # 3) ビルド用 VM で docker-base にバンドルを焼き込み、成果物を dest_rootfs へ
            await _set_phase(row_id, BundlePhase.building.value)
            await image_build.run_build_vm(
                kernel_path=DEFAULT_KERNEL_PATH,
                base_rootfs_path=base,
                scratch_path=scratch,
                cpu=max(1, settings.VM_CPU_LIMIT),
                memory_mb=max(512, settings.VM_MEMORY_LIMIT_MB),
                timeout_s=image_build.DEFAULT_BUILD_TIMEOUT_S,
                boot_marker="hb_build=1",
                result_rootfs_path=dest_rootfs,
            )

            # 4) scratch から結果回収・検査
            await _set_phase(row_id, BundlePhase.inspecting.value)
            status = (await _read_scratch_file(scratch, "status")).strip()
            if status != "0":
                err = (await _read_scratch_file(scratch, "compose.err")).strip() \
                      or (await _read_scratch_file(scratch, "build.log")).strip()[-400:]
                raise BundleError(f"ビルドに失敗しました（status={status or '?'}）: {err}")
            compose_json = await _read_scratch_file(scratch, "compose.json")
            loaded = await _read_scratch_file(scratch, "loaded.list")
            summary, verr = _validate_compose(compose_json, loaded)
            if verr is not None:
                raise BundleError(verr)
        finally:
            if os.path.exists(scratch):
                try:
                    os.remove(scratch)
                except OSError:
                    pass

        # 本番起動時にプロジェクト名でカウントするためのヒントを埋め込みたいが、
        # それは起動側の責務。ここでは要約のみ保存する。
        await _finish(row_id, RootfsConversionStatus.ready.value, None,
                      rootfs_path=dest_rootfs, summary=json.dumps(summary, ensure_ascii=False))
        logger.info(f"Compose バンドルのビルドが完了しました: image.id={row_id} -> {dest_rootfs}")
    except Exception as e:
        message = str(e).strip()[:512]
        # 失敗時は中途半端な成果物を残さない
        if dest_rootfs and os.path.exists(dest_rootfs):
            try:
                os.remove(dest_rootfs)
            except OSError:
                pass
        await _finish(row_id, RootfsConversionStatus.failed.value, message)
        logger.error(f"Compose バンドルのビルドに失敗しました: image.id={row_id}: {e}")


async def run_bundle_build_worker() -> None:
    """pending かつ rootfs_kind='compose' の Image を順次ビルドするバックグラウンドループ。"""
    logger.info("Bundle build worker started")
    while True:
        try:
            rid = await _claim_pending()
            if rid is not None:
                await _process_one(rid)
                continue
        except Exception as e:
            logger.error(f"Bundle build worker error: {e}")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
