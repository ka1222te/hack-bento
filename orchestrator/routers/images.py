import uuid
import os
import re
import asyncio
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from database import get_db
from models import Image, Backend, Difficulty, Visibility, User, UserRole, ImageCollaborator, CollaboratorRole
from deps import get_current_user, require_admin, require_username_set
from services.smolvm import docker_load
from services.firecracker_setup import DEFAULTS_DIR
from config import settings

router = APIRouter(prefix="/api/images", tags=["images"])

UPLOAD_DIR = "/data/images/uploads"
README_DIR = "/data/images/readmes"
VM_DIR = "/data/images/vm"
ALLOWED_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.zst")
ALLOWED_README   = (".md", ".txt")
_ALLOWED_DIRS    = (UPLOAD_DIR, README_DIR, VM_DIR)


def _safe_remove(path: Optional[str]) -> None:
    """許可ディレクトリ配下のファイルのみ削除する。パストラバーサル対策。

    DEFAULTS_DIR 配下（共有のデフォルトカーネル/rootfs）は複数プロジェクトから
    参照されるため削除対象から除外する。
    """
    if not path:
        return
    real = os.path.realpath(path)
    defaults_real = os.path.realpath(DEFAULTS_DIR)
    if real.startswith(defaults_real + os.sep) or real == defaults_real:
        return
    if not any(real.startswith(os.path.realpath(d) + os.sep) or real == os.path.realpath(d)
               for d in _ALLOWED_DIRS):
        import logging
        logging.getLogger(__name__).warning(f"safe_remove: path outside allowed dirs, skipping: {real}")
        return
    try:
        os.remove(real)
    except OSError:
        pass
_VALID_SLUG      = re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
TRUSTED_REGISTRIES = ("docker.io", "ghcr.io", "quay.io", "gcr.io", "registry.hub.docker.com")
ALLOWED_CATEGORIES = frozenset({
    "Web", "Pwn", "Crypto", "Reversing", "Forensics", "OSINT", "Misc",
    "CVE", "Privilege Escalation", "RCE", "LFI/RFI", "SQL Injection", "XSS", "SSRF",
    "Sandbox Test", "Network Test", "Docker Test",
})


class ImageResponse(BaseModel):
    id: int
    owner_username: Optional[str] = None
    name: str
    slug: str
    description: Optional[str]
    readme: Optional[str] = None
    backend: str
    oci_ref: str
    has_kernel: bool = False
    has_rootfs: bool = False
    is_default_kernel: bool = False
    is_default_rootfs: bool = False
    default_kernel_asset_id: Optional[int] = None
    default_rootfs_asset_id: Optional[int] = None
    difficulty: str
    category: Optional[str]
    estimated_minutes: int
    timeout_minutes: int
    is_active: bool
    visibility: str
    created_at: datetime

    model_config = {"from_attributes": True}


def _allowed_image(filename: str) -> bool:
    return any(filename.endswith(s) for s in ALLOWED_SUFFIXES)

def _allowed_readme(filename: str) -> bool:
    return any(filename.lower().endswith(s) for s in ALLOWED_README)

_VALID_IMAGE_REF = re.compile(r'^[a-zA-Z0-9._:/@-]+$')

def _validate_image_ref(ref: str) -> None:
    """Docker image reference のレジストリホストが許可リストに含まれるか検証する。

    Docker image reference の形式:
      [registry-host[:port]/]name[:tag][@digest]

    レジストリ未指定（例: ubuntu:22.04）は Docker Hub として扱い許可する。
    IP アドレスや許可外ホスト、許可外文字を含む場合は ValueError を送出する。
    """
    if not _VALID_IMAGE_REF.match(ref):
        raise ValueError("イメージ参照に使用できない文字が含まれています")

    # name 部分（タグ・ダイジェストを除いたパス）を取り出す
    name_part = ref.split("@")[0].split(":")[0]
    segments = name_part.split("/")

    # 先頭セグメントにドット・コロンが含まれるか localhost の場合はレジストリホストと判定
    # （Docker の規則に準拠: ドットまたはコロンを含む先頭セグメントはホスト名とみなす）
    first = segments[0]
    is_registry = ("." in first or ":" in first or first == "localhost")

    if not is_registry:
        # レジストリ未指定 → Docker Hub (docker.io) として扱い許可
        return

    # IP アドレスを拒否（IPv4・IPv6）
    host = first.split(":")[0]
    ipv4_pattern = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
    if ipv4_pattern.match(host) or host == "localhost" or host.startswith("["):
        raise ValueError(f"レジストリに IP アドレス・localhost は使用できません: {host}")

    # ホストが許可リストのいずれかと一致するか、そのサブドメインか確認
    if not any(host == d or host.endswith(f".{d}") for d in TRUSTED_REGISTRIES):
        raise ValueError(
            f"許可されていないレジストリです: {host}  "
            f"（許可: {', '.join(TRUSTED_REGISTRIES)}）"
        )


async def _can_see(image: Image, user: Optional[User], db: AsyncSession) -> bool:
    """閲覧権限チェック。
    - public   : 誰でも閲覧可
    - protected: ログイン済みユーザのみ
    - private  : オーナー・管理者・コラボレーターのみ（他のログインユーザには不可）
    """
    if image.visibility == Visibility.public:
        return True

    # public 以外は未ログインには不可
    if user is None:
        return False

    # 管理者は全て閲覧可
    if user.role == UserRole.admin:
        return True

    # protected はログイン済みなら可
    if image.visibility == Visibility.protected:
        return True

    # private: オーナー本人のみ、またはコラボレーターのみ
    # (visibility == private が確定した場合のみここに到達)
    if image.owner_id == user.id:
        return True

    collab = await db.execute(
        select(ImageCollaborator).where(
            ImageCollaborator.image_id == image.id,
            ImageCollaborator.user_id == user.id,
        )
    )
    return collab.scalar_one_or_none() is not None


def _read_readme(path: Optional[str]) -> Optional[str]:
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            pass
    return None


async def _build_response(image: Image, db: AsyncSession, include_readme: bool = False) -> ImageResponse:
    owner_username = None
    if image.owner_id:
        u = await db.get(User, image.owner_id)
        owner_username = u.username if u else None
    readme = _read_readme(getattr(image, "readme_path", None)) if include_readme else None
    return ImageResponse(
        id=image.id,
        owner_username=owner_username,
        name=image.name,
        slug=image.slug,
        description=image.description,
        readme=readme,
        backend=image.backend.value,
        oci_ref=image.oci_ref,
        has_kernel=bool(image.kernel_path),
        has_rootfs=bool(image.rootfs_path),
        is_default_kernel=(image.default_kernel_asset_id is not None),
        is_default_rootfs=(image.default_rootfs_asset_id is not None),
        default_kernel_asset_id=image.default_kernel_asset_id,
        default_rootfs_asset_id=image.default_rootfs_asset_id,
        difficulty=image.difficulty.value,
        category=image.category,
        estimated_minutes=image.estimated_minutes,
        timeout_minutes=image.timeout_minutes,
        is_active=image.is_active,
        visibility=image.visibility.value,
        created_at=image.created_at,
    )


async def _docker_pull(ref: str) -> tuple[str, str]:
    """docker pull してアーカイブに保存し、(oci_ref, save_path) を返す。"""
    import logging
    _log = logging.getLogger(__name__)
    _validate_image_ref(ref)
    # pull
    pull = await asyncio.create_subprocess_exec(
        "docker", "pull", ref,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(pull.communicate(), timeout=300)
    if pull.returncode != 0:
        _log.error(f"docker pull failed: {stderr.decode().strip()}")
        raise RuntimeError("docker pull に失敗しました")

    # save（ファイルと同じ保存先に tar として保存）
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    save_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}.tar")
    save = await asyncio.create_subprocess_exec(
        "docker", "save", "-o", save_path, ref,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(save.communicate(), timeout=300)
    if save.returncode != 0:
        if os.path.exists(save_path):
            os.remove(save_path)
        _log.error(f"docker save failed: {stderr.decode().strip()}")
        raise RuntimeError("docker save に失敗しました")

    return ref, save_path


def _validate_download_url(url: str) -> None:
    """ゲストカーネル/rootfs のダウンロード元URLを検証する（SSRF対策）。

    http(s) のみ許可し、ホスト名がプライベートIP・ループバック・リンクローカル
    アドレスに解決される場合は拒否する。
    """
    import socket
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL は http または https で始まる必要があります")
    host = parsed.hostname
    if not host:
        raise ValueError("URL からホスト名を取得できません")

    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"ホスト名を解決できません: {host}")

    for family, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"内部ネットワークを指すURLは使用できません: {host} -> {ip}")


async def _download_vm_asset(url: str, dest_path: str, timeout: int = 1800) -> None:
    """指定URLからゲストカーネル/rootfsをダウンロードして dest_path に保存する。"""
    import logging
    _log = logging.getLogger(__name__)
    _validate_download_url(url)

    tmp_path = dest_path + ".tmp"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-fsSL", "--max-filesize", str(settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024),
        url, "-o", tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise RuntimeError("ダウンロードがタイムアウトしました")
    if proc.returncode != 0:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        _log.error(f"asset download failed: {stderr.decode().strip()}")
        raise RuntimeError("ファイルのダウンロードに失敗しました")
    os.replace(tmp_path, dest_path)


_VM_ASSET_KINDS = {
    "kernel": {"suffix": "vmlinux", "model": None, "label": "ゲストカーネル"},
    "rootfs": {"suffix": "rootfs.ext4", "model": None, "label": "rootfs"},
}


async def _resolve_vm_asset(
    *,
    kind: str,
    mode: str,
    upload: Optional[UploadFile],
    url: Optional[str],
    asset_id: Optional[int],
    db: AsyncSession,
) -> tuple[str, Optional[int]]:
    """kernel/rootfs の入力（default/file/link のいずれか）を検証・保存し、
    (file_path, default_asset_id) を返す。

    呼び出し側で保存先ディレクトリ作成・モデルへの代入・旧ファイルの削除を行う。
    """
    from models import DefaultKernelAsset, DefaultRootfsAsset

    info = _VM_ASSET_KINDS[kind]
    label = info["label"]
    image_max_bytes = settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024
    AssetModel = DefaultKernelAsset if kind == "kernel" else DefaultRootfsAsset

    if mode == "default":
        if not asset_id:
            raise HTTPException(status_code=400, detail=f"{label}のデフォルト資産を選択してください")
        asset = await db.get(AssetModel, asset_id)
        if not asset or not asset.is_active:
            raise HTTPException(status_code=400, detail=f"指定された{label}のデフォルト資産が見つかりません")
        if not os.path.exists(asset.file_path):
            raise HTTPException(status_code=503, detail=f"{label}のデフォルト資産ファイルが見つかりません。管理者に連絡してください")
        return asset.file_path, asset.id

    if mode == "link":
        if not url or not url.strip():
            raise HTTPException(status_code=400, detail=f"{label}のダウンロードURLを指定してください")
        os.makedirs(VM_DIR, exist_ok=True)
        dest_path = os.path.join(VM_DIR, f"{uuid.uuid4()}-{info['suffix']}")
        try:
            await _download_vm_asset(url.strip(), dest_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"{kind} download failed: {e}")
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise HTTPException(status_code=500, detail=f"{label}のダウンロードに失敗しました")
        return dest_path, None

    # mode == "file"
    has_file = bool(upload and upload.filename and upload.size and upload.size > 0)
    if not has_file:
        raise HTTPException(status_code=400, detail=f"{label}のファイルをアップロードしてください")
    os.makedirs(VM_DIR, exist_ok=True)
    dest_path = os.path.join(VM_DIR, f"{uuid.uuid4()}-{info['suffix']}")
    try:
        written = 0
        with open(dest_path, "wb") as f:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > image_max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"ファイルサイズが上限を超えています（上限: {settings.UPLOAD_IMAGE_MAX_MB} MB）",
                    )
                f.write(chunk)
    except HTTPException:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise
    except Exception:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise HTTPException(status_code=500, detail=f"{label}の保存に失敗しました")
    return dest_path, None


async def _get_optional_user(request: Request, db: AsyncSession) -> Optional[User]:
    try:
        token = request.cookies.get("access_token")
        if not token:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
        if not token:
            return None
        from services.jwt_utils import decode_token
        payload = decode_token(token)
        result = await db.execute(
            select(User).where(User.id == int(payload["sub"]), User.is_active == True)
        )
        return result.scalar_one_or_none()
    except Exception:
        return None


@router.get("/", response_model=list[ImageResponse])
async def list_images(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_optional_user(request, db)
    # ホストの VM_BACKEND に対応する backend のプロジェクトのみを提示する
    # （macvlan ホストでは bridge プロジェクトを起動できないため、AND 条件で絞り込む）
    host_backend = Backend.bridge if settings.VM_BACKEND == "bridge" else Backend.macvlan
    result = await db.execute(
        select(Image)
        .where(Image.is_active == True, Image.backend == host_backend)
        .order_by(Image.name)
    )
    out = []
    for img in result.scalars().all():
        if await _can_see(img, user, db):
            out.append(await _build_response(img, db))
    return out


@router.get("/default-assets")
async def list_default_assets(
    kind: str,
    current_user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    """bridge プロジェクト作成・編集時に選択可能なデフォルトカーネル/rootfs資産の一覧を返す。"""
    from models import DefaultKernelAsset, DefaultRootfsAsset

    if kind not in ("kernel", "rootfs"):
        raise HTTPException(status_code=400, detail="kind は kernel または rootfs を指定してください")
    AssetModel = DefaultKernelAsset if kind == "kernel" else DefaultRootfsAsset
    result = await db.execute(
        select(AssetModel).where(AssetModel.is_active == True).order_by(AssetModel.label)
    )
    return [{"id": a.id, "label": a.label} for a in result.scalars().all()]


@router.get("/{image_id}/readme")
async def get_readme(image_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_optional_user(request, db)
    result = await db.execute(select(Image).where(Image.id == image_id, Image.is_active == True))
    image = result.scalar_one_or_none()
    if not image or not await _can_see(image, user, db):
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
    return {"readme": _read_readme(getattr(image, "readme_path", None))}


@router.post("/upload", response_model=ImageResponse, status_code=status.HTTP_201_CREATED)
async def upload_image(
    file: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
    kernel: Optional[UploadFile] = File(None),
    rootfs: Optional[UploadFile] = File(None),
    kernel_mode: str = Form("file"),
    rootfs_mode: str = Form("file"),
    default_kernel_asset_id: Optional[int] = Form(None),
    default_rootfs_asset_id: Optional[int] = Form(None),
    kernel_url: Optional[str] = Form(None),
    rootfs_url: Optional[str] = Form(None),
    readme: Optional[UploadFile] = File(None),
    readme_text: Optional[str] = Form(None),
    name: str = Form(...),
    slug: str = Form(...),
    description: Optional[str] = Form(None),
    backend: Optional[Backend] = Form(None),
    difficulty: Difficulty = Form(Difficulty.none),
    category: Optional[str] = Form(None),
    timeout_minutes: Optional[int] = Form(None),
    cpu_limit: Optional[int] = Form(None),
    memory_limit_mb: Optional[int] = Form(None),
    visibility: Visibility = Form(Visibility.protected),
    current_user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    # .env のデフォルト値を適用し、システム上限を超えないようクランプ
    if timeout_minutes is None:
        timeout_minutes = settings.DEFAULT_TIMEOUT_MINUTES
    if cpu_limit is None:
        cpu_limit = settings.VM_CPU_LIMIT
    if memory_limit_mb is None:
        memory_limit_mb = settings.VM_MEMORY_LIMIT_MB

    if cpu_limit < 1 or cpu_limit > settings.VM_CPU_LIMIT:
        raise HTTPException(status_code=400, detail=f"cpu_limit は 1〜{settings.VM_CPU_LIMIT} の範囲で指定してください")
    if memory_limit_mb < 128 or memory_limit_mb > settings.VM_MEMORY_LIMIT_MB:
        raise HTTPException(status_code=400, detail=f"memory_limit_mb は 128〜{settings.VM_MEMORY_LIMIT_MB} の範囲で指定してください")
    if timeout_minutes < 1 or timeout_minutes > 1440:
        raise HTTPException(status_code=400, detail="timeout_minutes は 1〜1440 の範囲で指定してください")
    if len(name) > 128:
        raise HTTPException(status_code=400, detail="name は 128 文字以内で指定してください")
    if description and len(description) > 512:
        raise HTTPException(status_code=400, detail="description は 512 文字以内で指定してください")
    if category and category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"無効なカテゴリです: {category}")

    # ホストの VM_BACKEND によって扱うバックエンドが固定される
    # （macvlan/bridge は明確に分離されており、同一ホストで両方を起動することはない）
    host_backend = Backend.bridge if settings.VM_BACKEND == "bridge" else Backend.macvlan
    if backend is not None and backend != host_backend:
        raise HTTPException(status_code=400, detail=f"このホストでは backend={host_backend.value} のプロジェクトのみ作成できます")
    backend = host_backend

    image_max_bytes = settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024
    readme_max_bytes = settings.UPLOAD_README_MAX_MB * 1024 * 1024

    if readme and readme.filename and not _allowed_readme(readme.filename):
        raise HTTPException(status_code=400, detail="README は .md または .txt のみ対応")

    # slug バリデーション
    if not _VALID_SLUG.match(slug):
        raise HTTPException(status_code=400, detail="slug は英数字・ハイフン・アンダースコアのみ使用可（1〜128文字）")

    # slug の重複チェック（同オーナー内・アクティブなイメージのみ）
    existing = await db.execute(
        select(Image).where(Image.owner_id == current_user.id, Image.slug == slug, Image.is_active == True)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="このslugは既に使用されています")

    oci_ref = ""
    save_path = None
    kernel_path = None
    rootfs_path = None
    kernel_asset_id: Optional[int] = None
    rootfs_asset_id: Optional[int] = None

    if backend == Backend.macvlan:
        # Docker イメージ: ファイルと URL のどちらかが必須（両方あればファイル優先）
        has_file = bool(file and file.filename and file.size and file.size > 0)
        has_url  = bool(image_url and image_url.strip())
        if not has_file and not has_url:
            raise HTTPException(status_code=400, detail="Docker イメージファイルまたはイメージ URL を指定してください")

        if has_file and not _allowed_image(file.filename or ""):
            raise HTTPException(status_code=400, detail=f"対応拡張子: {', '.join(ALLOWED_SUFFIXES)}")

        # Docker イメージ取得（ファイル優先、なければ URL から pull）
        if has_file:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            ext = next(s for s in ALLOWED_SUFFIXES if (file.filename or "").endswith(s))
            save_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
            try:
                written = 0
                with open(save_path, "wb") as f:
                    while chunk := await file.read(1024 * 1024):
                        written += len(chunk)
                        if written > image_max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"ファイルサイズが上限を超えています（上限: {settings.UPLOAD_IMAGE_MAX_MB} MB）",
                            )
                        f.write(chunk)
            except HTTPException:
                if os.path.exists(save_path):
                    os.remove(save_path)
                raise
            except Exception:
                if os.path.exists(save_path):
                    os.remove(save_path)
                raise HTTPException(status_code=500, detail="ファイル保存に失敗しました")
            try:
                oci_ref = await docker_load(save_path)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"docker load failed: {e}")
                os.remove(save_path)
                raise HTTPException(status_code=500, detail="イメージ処理に失敗しました")
        else:
            # URL から docker pull → docker save でアーカイブ保存
            ref = image_url.strip()
            try:
                oci_ref, save_path = await _docker_pull(ref)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"docker pull failed: {e}")
                raise HTTPException(status_code=500, detail="イメージ取得に失敗しました")
    else:
        # bridge バックエンド: ゲストカーネル・rootfs は default/file/link のいずれかで指定
        if kernel_mode not in ("default", "file", "link"):
            raise HTTPException(status_code=400, detail="kernel_mode は default・file・link のいずれかを指定してください")
        if rootfs_mode not in ("default", "file", "link"):
            raise HTTPException(status_code=400, detail="rootfs_mode は default・file・link のいずれかを指定してください")

        new_kernel_path: Optional[str] = None
        new_rootfs_path: Optional[str] = None
        try:
            new_kernel_path, kernel_asset_id = await _resolve_vm_asset(
                kind="kernel", mode=kernel_mode, upload=kernel, url=kernel_url,
                asset_id=default_kernel_asset_id, db=db,
            )
            new_rootfs_path, rootfs_asset_id = await _resolve_vm_asset(
                kind="rootfs", mode=rootfs_mode, upload=rootfs, url=rootfs_url,
                asset_id=default_rootfs_asset_id, db=db,
            )
        except HTTPException:
            for p, mode in ((new_kernel_path, kernel_mode), (new_rootfs_path, rootfs_mode)):
                if p and mode != "default" and os.path.exists(p):
                    os.remove(p)
            raise
        kernel_path = new_kernel_path
        rootfs_path = new_rootfs_path

    # README 保存（ファイル優先、なければテキスト入力）
    readme_path = None
    if readme and readme.filename:
        os.makedirs(README_DIR, exist_ok=True)
        readme_ext = ".md" if readme.filename.lower().endswith(".md") else ".txt"
        readme_path = os.path.join(README_DIR, f"{uuid.uuid4()}{readme_ext}")
        try:
            written = 0
            with open(readme_path, "wb") as f:
                while chunk := await readme.read(1024 * 1024):
                    written += len(chunk)
                    if written > readme_max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"README ファイルサイズが上限を超えています（上限: {settings.UPLOAD_README_MAX_MB} MB）",
                        )
                    f.write(chunk)
        except HTTPException:
            if os.path.exists(readme_path):
                os.remove(readme_path)
            raise
        except Exception:
            readme_path = None
    elif readme_text and readme_text.strip():
        if len(readme_text.encode()) > readme_max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"README テキストが上限を超えています（上限: {settings.UPLOAD_README_MAX_MB} MB）",
            )
        os.makedirs(README_DIR, exist_ok=True)
        readme_path = os.path.join(README_DIR, f"{uuid.uuid4()}.md")
        try:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme_text)
        except Exception:
            readme_path = None

    image = Image(
        owner_id=current_user.id,
        name=name,
        slug=slug,
        description=description,
        backend=backend,
        oci_ref=oci_ref,
        archive_path=save_path,
        kernel_path=kernel_path,
        rootfs_path=rootfs_path,
        default_kernel_asset_id=kernel_asset_id,
        default_rootfs_asset_id=rootfs_asset_id,
        readme_path=readme_path,
        difficulty=difficulty,
        category=category,
        timeout_minutes=timeout_minutes,
        cpu_limit=cpu_limit,
        memory_limit_mb=memory_limit_mb,
        visibility=visibility,
        created_by=current_user.id,
    )
    db.add(image)
    await db.commit()
    await db.refresh(image)
    return await _build_response(image, db)


@router.post("/{image_id}/reload", response_model=ImageResponse)
async def reload_image(
    image_id: int,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Image).where(Image.id == image_id, Image.is_active == True))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")
    if image.backend != Backend.macvlan:
        raise HTTPException(status_code=400, detail="reload は macvlan バックエンドのプロジェクトのみ対応しています")
    if not image.archive_path or not os.path.exists(image.archive_path):
        raise HTTPException(status_code=400, detail="アーカイブが見つかりません")
    try:
        oci_ref = await docker_load(image.archive_path)
    except Exception:
        raise HTTPException(status_code=500, detail="docker load に失敗しました")
    image.oci_ref = oci_ref
    await db.commit()
    await db.refresh(image)
    return await _build_response(image, db)


@router.patch("/{image_id}/visibility")
async def update_visibility(
    image_id: int,
    visibility: Visibility,
    current_user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Image).where(Image.id == image_id, Image.is_active == True))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")
    if current_user.role != UserRole.admin and image.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="権限がありません")
    image.visibility = visibility
    await db.commit()
    return {"visibility": visibility.value}


@router.patch("/{image_id}", response_model=ImageResponse)
async def update_image(
    image_id: int,
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    difficulty: Optional[Difficulty] = Form(None),
    category: Optional[str] = Form(None),
    timeout_minutes: Optional[int] = Form(None),
    visibility: Optional[Visibility] = Form(None),
    file: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
    kernel: Optional[UploadFile] = File(None),
    rootfs: Optional[UploadFile] = File(None),
    kernel_mode: Optional[str] = Form(None),
    rootfs_mode: Optional[str] = Form(None),
    default_kernel_asset_id: Optional[int] = Form(None),
    default_rootfs_asset_id: Optional[int] = Form(None),
    kernel_url: Optional[str] = Form(None),
    rootfs_url: Optional[str] = Form(None),
    readme: Optional[UploadFile] = File(None),
    readme_text: Optional[str] = Form(None),
    current_user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    """プロジェクトの設定を更新する。イメージを変更する場合は起動中VMを先に停止する。"""
    result = await db.execute(select(Image).where(Image.id == image_id, Image.is_active == True))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")

    # 権限チェック: オーナー・管理者・read_write コラボレーターのみ編集可
    is_owner = (current_user.role == UserRole.admin or image.owner_id == current_user.id)
    if not is_owner:
        collab = await db.execute(
            select(ImageCollaborator).where(
                ImageCollaborator.image_id == image_id,
                ImageCollaborator.user_id == current_user.id,
                ImageCollaborator.role == CollaboratorRole.read_write,
            )
        )
        if not collab.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="権限がありません")

    # イメージ/カーネル・rootfs 変更が伴う場合: 起動中VMを先に停止
    has_new_file = bool(file and file.filename and file.size and file.size > 0)
    has_new_url  = bool(image_url and image_url.strip())
    has_new_kernel = bool(kernel and kernel.filename and kernel.size and kernel.size > 0)
    has_new_rootfs = bool(rootfs and rootfs.filename and rootfs.size and rootfs.size > 0)
    image_max_bytes = settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024

    # bridge バックエンド: モード未指定なら現状維持（変更なし）として扱う
    kernel_changing = False
    rootfs_changing = False
    if image.backend == Backend.bridge:
        if kernel_mode is not None:
            if kernel_mode not in ("default", "file", "link"):
                raise HTTPException(status_code=400, detail="kernel_mode は default・file・link のいずれかを指定してください")
            if kernel_mode == "default":
                kernel_changing = default_kernel_asset_id != image.default_kernel_asset_id
            elif kernel_mode == "file":
                kernel_changing = has_new_kernel
            else:  # link
                kernel_changing = bool(kernel_url and kernel_url.strip())
        if rootfs_mode is not None:
            if rootfs_mode not in ("default", "file", "link"):
                raise HTTPException(status_code=400, detail="rootfs_mode は default・file・link のいずれかを指定してください")
            if rootfs_mode == "default":
                rootfs_changing = default_rootfs_asset_id != image.default_rootfs_asset_id
            elif rootfs_mode == "file":
                rootfs_changing = has_new_rootfs
            else:  # link
                rootfs_changing = bool(rootfs_url and rootfs_url.strip())

    needs_vm_stop = (
        (image.backend == Backend.macvlan and (has_new_file or has_new_url))
        or (image.backend == Backend.bridge and (kernel_changing or rootfs_changing))
    )
    if needs_vm_stop:
        from models import Environment, EnvStatus
        from services.watchdog import destroy_env, claim_env_for_stop
        env_result = await db.execute(
            select(Environment.id).where(
                Environment.image_id == image_id,
                Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
            )
        )
        env_ids = env_result.scalars().all()
        for env_id in env_ids:
            try:
                env = await claim_env_for_stop(db, env_id)
                if env is not None:
                    await destroy_env(env)
            except Exception:
                pass
        if env_ids:
            await db.flush()

    if image.backend == Backend.macvlan:
        # 新しい Docker イメージを取得・保存
        if has_new_file:
            if not _allowed_image(file.filename or ""):
                raise HTTPException(status_code=400, detail=f"対応拡張子: {', '.join(ALLOWED_SUFFIXES)}")
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            ext = next(s for s in ALLOWED_SUFFIXES if (file.filename or "").endswith(s))
            save_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}{ext}")
            try:
                written = 0
                with open(save_path, "wb") as f:
                    while chunk := await file.read(1024 * 1024):
                        written += len(chunk)
                        if written > image_max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"ファイルサイズが上限を超えています（上限: {settings.UPLOAD_IMAGE_MAX_MB} MB）",
                            )
                        f.write(chunk)
                new_oci_ref = await docker_load(save_path)
                old_path = image.archive_path
                image.archive_path = save_path
                image.oci_ref = new_oci_ref
                if old_path and old_path != save_path:
                    _safe_remove(old_path)
            except HTTPException:
                if os.path.exists(save_path):
                    os.remove(save_path)
                raise
            except Exception:
                if os.path.exists(save_path):
                    os.remove(save_path)
                raise HTTPException(status_code=500, detail="イメージ更新に失敗しました")
        elif has_new_url:
            ref = image_url.strip()
            try:
                new_oci_ref, save_path = await _docker_pull(ref)
                old_path = image.archive_path
                image.oci_ref = new_oci_ref
                image.archive_path = save_path
                if old_path and old_path != save_path:
                    _safe_remove(old_path)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"docker pull failed (update): {e}")
                raise HTTPException(status_code=500, detail="イメージ取得に失敗しました")
    else:
        # bridge バックエンド: ゲストカーネル・rootfs の差し替え（変更がある場合のみ）
        if kernel_changing:
            new_kernel_path, new_kernel_asset_id = await _resolve_vm_asset(
                kind="kernel", mode=kernel_mode, upload=kernel, url=kernel_url,
                asset_id=default_kernel_asset_id, db=db,
            )
            try:
                old_kernel = image.kernel_path
                image.kernel_path = new_kernel_path
                image.default_kernel_asset_id = new_kernel_asset_id
                if old_kernel and old_kernel != new_kernel_path:
                    _safe_remove(old_kernel)
            except Exception:
                if kernel_mode != "default" and os.path.exists(new_kernel_path):
                    os.remove(new_kernel_path)
                raise

        if rootfs_changing:
            new_rootfs_path, new_rootfs_asset_id = await _resolve_vm_asset(
                kind="rootfs", mode=rootfs_mode, upload=rootfs, url=rootfs_url,
                asset_id=default_rootfs_asset_id, db=db,
            )
            try:
                old_rootfs = image.rootfs_path
                image.rootfs_path = new_rootfs_path
                image.default_rootfs_asset_id = new_rootfs_asset_id
                if old_rootfs and old_rootfs != new_rootfs_path:
                    _safe_remove(old_rootfs)
            except Exception:
                if rootfs_mode != "default" and os.path.exists(new_rootfs_path):
                    os.remove(new_rootfs_path)
                raise

    # メタデータ更新
    if name is not None:
        if len(name) > 128:
            raise HTTPException(status_code=400, detail="name は 128 文字以内で指定してください")
        image.name = name
    if description is not None:
        if len(description) > 512:
            raise HTTPException(status_code=400, detail="description は 512 文字以内で指定してください")
        image.description = description
    if difficulty is not None:
        image.difficulty = difficulty
    if category is not None:
        if category and category not in ALLOWED_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"無効なカテゴリです: {category}")
        image.category = category or None
    if timeout_minutes is not None:
        if timeout_minutes < 1 or timeout_minutes > 1440:
            raise HTTPException(status_code=400, detail="timeout_minutes は 1〜1440 の範囲で指定してください")
        image.timeout_minutes = timeout_minutes
    if visibility is not None and is_owner:
        image.visibility = visibility

    # README 更新
    has_new_readme_file = bool(readme and readme.filename)
    has_new_readme_text = bool(readme_text and readme_text.strip())
    if has_new_readme_file:
        if not _allowed_readme(readme.filename or ""):
            raise HTTPException(status_code=400, detail="README は .md または .txt のみ対応")
        readme_max_bytes = settings.UPLOAD_README_MAX_MB * 1024 * 1024
        os.makedirs(README_DIR, exist_ok=True)
        readme_ext = ".md" if (readme.filename or "").lower().endswith(".md") else ".txt"
        readme_path = os.path.join(README_DIR, f"{uuid.uuid4()}{readme_ext}")
        try:
            written = 0
            with open(readme_path, "wb") as f:
                while chunk := await readme.read(1024 * 1024):
                    written += len(chunk)
                    if written > readme_max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"README ファイルサイズが上限を超えています（上限: {settings.UPLOAD_README_MAX_MB} MB）",
                        )
                    f.write(chunk)
            image.readme_path = readme_path
        except HTTPException:
            if os.path.exists(readme_path):
                os.remove(readme_path)
            raise
        except Exception:
            pass
    elif has_new_readme_text:
        _readme_max = settings.UPLOAD_README_MAX_MB * 1024 * 1024
        if len(readme_text.encode()) > _readme_max:
            raise HTTPException(
                status_code=413,
                detail=f"README テキストが上限を超えています（上限: {settings.UPLOAD_README_MAX_MB} MB）",
            )
        os.makedirs(README_DIR, exist_ok=True)
        readme_path = os.path.join(README_DIR, f"{uuid.uuid4()}.md")
        try:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme_text)
            image.readme_path = readme_path
        except Exception:
            pass

    await db.commit()
    await db.refresh(image)
    return await _build_response(image, db)


@router.get("/{image_id}/can-edit")
async def can_edit(
    image_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """現在のユーザがこのイメージを編集できるか確認する。"""
    result = await db.execute(select(Image).where(Image.id == image_id, Image.is_active == True))
    image = result.scalar_one_or_none()
    if not image:
        return {"can_edit": False}
    if current_user.role == UserRole.admin or image.owner_id == current_user.id:
        return {"can_edit": True}
    collab = await db.execute(
        select(ImageCollaborator).where(
            ImageCollaborator.image_id == image_id,
            ImageCollaborator.user_id == current_user.id,
            ImageCollaborator.role == CollaboratorRole.read_write,
        )
    )
    return {"can_edit": collab.scalar_one_or_none() is not None}


@router.delete("/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(
    image_id: int,
    current_user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Image).where(Image.id == image_id, Image.is_active == True))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")
    if current_user.role != UserRole.admin and image.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="権限がありません")

    # 起動中の環境を停止してからイメージを論理削除する
    # （行ロックで watchdog や他経路との二重停止を防ぐ）
    from models import Environment, EnvStatus
    from services.watchdog import destroy_env, claim_env_for_stop
    env_result = await db.execute(
        select(Environment.id).where(
            Environment.image_id == image_id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
    )
    for env_id in env_result.scalars().all():
        try:
            env = await claim_env_for_stop(db, env_id)
            if env is not None:
                await destroy_env(env)
        except Exception:
            pass

    image.is_active = False
    await db.commit()
