import uuid
import os
import asyncio
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from database import get_db
from models import Image, Difficulty, Visibility, User, UserRole, ImageCollaborator, CollaboratorRole
from deps import get_current_user, require_admin
from services.smolvm import docker_load
from config import settings

router = APIRouter(prefix="/api/images", tags=["images"])

UPLOAD_DIR = "/data/images/uploads"
README_DIR = "/data/images/readmes"
ALLOWED_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.zst")
ALLOWED_README   = (".md", ".txt")
TRUSTED_REGISTRIES = ("docker.io", "ghcr.io", "quay.io", "gcr.io", "registry.hub.docker.com")


class ImageResponse(BaseModel):
    id: int
    owner_username: Optional[str] = None
    name: str
    slug: str
    description: Optional[str]
    readme: Optional[str] = None      # README 本文
    oci_ref: str
    archive_path: Optional[str]
    readme_path: Optional[str] = None
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

def _validate_image_ref(ref: str) -> None:
    """Docker image reference のレジストリホストが許可リストに含まれるか検証する。

    Docker image reference の形式:
      [registry-host[:port]/]name[:tag][@digest]

    レジストリ未指定（例: ubuntu:22.04）は Docker Hub として扱い許可する。
    IP アドレスや許可外ホストを含む場合は ValueError を送出する。
    """
    import re
    # name 部分（タグ・ダイジェストを除いたパス）を取り出す
    name_part = ref.split("@")[0].split(":")[0]
    segments = name_part.split("/")

    # 先頭セグメントにドット・コロン・大文字が含まれる場合はレジストリホストと判定
    # （Docker の規則: https://docs.docker.com/engine/reference/commandline/pull/）
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
        oci_ref=image.oci_ref,
        archive_path=image.archive_path,
        readme_path=getattr(image, "readme_path", None),
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
    _validate_image_ref(ref)
    # pull
    pull = await asyncio.create_subprocess_exec(
        "docker", "pull", ref,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(pull.communicate(), timeout=300)
    if pull.returncode != 0:
        raise RuntimeError(f"docker pull failed: {stderr.decode().strip()}")

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
        raise RuntimeError(f"docker save failed: {stderr.decode().strip()}")

    return ref, save_path


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
    result = await db.execute(
        select(Image).where(Image.is_active == True).order_by(Image.name)
    )
    out = []
    for img in result.scalars().all():
        if await _can_see(img, user, db):
            out.append(await _build_response(img, db))
    return out


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
    readme: Optional[UploadFile] = File(None),
    readme_text: Optional[str] = Form(None),
    name: str = Form(...),
    slug: str = Form(...),
    description: Optional[str] = Form(None),
    difficulty: Difficulty = Form(Difficulty.none),
    category: Optional[str] = Form(None),
    timeout_minutes: Optional[int] = Form(None),
    cpu_limit: Optional[int] = Form(None),
    memory_limit_mb: Optional[int] = Form(None),
    visibility: Visibility = Form(Visibility.protected),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # .env のデフォルト値を適用
    if timeout_minutes is None:
        timeout_minutes = settings.DEFAULT_TIMEOUT_MINUTES
    if cpu_limit is None:
        cpu_limit = settings.VM_CPU_LIMIT
    if memory_limit_mb is None:
        memory_limit_mb = settings.VM_MEMORY_LIMIT_MB

    # Docker イメージ: ファイルと URL のどちらかが必須（両方あればファイル優先）
    has_file = bool(file and file.filename and file.size and file.size > 0)
    has_url  = bool(image_url and image_url.strip())
    if not has_file and not has_url:
        raise HTTPException(status_code=400, detail="Docker イメージファイルまたはイメージ URL を指定してください")

    if has_file and not _allowed_image(file.filename or ""):
        raise HTTPException(status_code=400, detail=f"対応拡張子: {', '.join(ALLOWED_SUFFIXES)}")

    image_max_bytes = settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024
    if has_file and file.size and file.size > image_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"ファイルサイズが上限を超えています（上限: {settings.UPLOAD_IMAGE_MAX_MB} MB）",
        )

    readme_max_bytes = settings.UPLOAD_README_MAX_MB * 1024 * 1024
    if readme and readme.filename and not _allowed_readme(readme.filename):
        raise HTTPException(status_code=400, detail="README は .md または .txt のみ対応")
    if readme and readme.size and readme.size > readme_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"README ファイルサイズが上限を超えています（上限: {settings.UPLOAD_README_MAX_MB} MB）",
        )

    # slug の重複チェック（同オーナー内）
    existing = await db.execute(
        select(Image).where(Image.owner_id == current_user.id, Image.slug == slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="このslugは既に使用されています")

    # Docker イメージ取得（ファイル優先、なければ URL から pull）
    save_path = None
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
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ファイル保存に失敗しました: {e}")
        try:
            oci_ref = await docker_load(save_path)
        except Exception as e:
            os.remove(save_path)
            raise HTTPException(status_code=500, detail=f"docker load に失敗しました: {e}")
    else:
        # URL から docker pull → docker save でアーカイブ保存
        ref = image_url.strip()
        try:
            oci_ref, save_path = await _docker_pull(ref)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"docker pull に失敗しました: {e}")

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
        oci_ref=oci_ref,
        archive_path=save_path,
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
    result = await db.execute(select(Image).where(Image.id == image_id))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")
    if not image.archive_path or not os.path.exists(image.archive_path):
        raise HTTPException(status_code=400, detail=f"アーカイブが見つかりません: {image.archive_path}")
    try:
        oci_ref = await docker_load(image.archive_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"docker load に失敗しました: {e}")
    image.oci_ref = oci_ref
    await db.commit()
    await db.refresh(image)
    return await _build_response(image, db)


@router.patch("/{image_id}/visibility")
async def update_visibility(
    image_id: int,
    visibility: Visibility,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Image).where(Image.id == image_id))
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
    readme: Optional[UploadFile] = File(None),
    readme_text: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """プロジェクトの設定を更新する。イメージを変更する場合は起動中VMを先に停止する。"""
    result = await db.execute(select(Image).where(Image.id == image_id, Image.is_active == True))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")

    # 権限チェック: オーナー・管理者・コラボレーター(write)
    is_owner = (current_user.role == UserRole.admin or image.owner_id == current_user.id)
    if not is_owner:
        collab = await db.execute(
            select(ImageCollaborator).where(
                ImageCollaborator.image_id == image_id,
                ImageCollaborator.user_id == current_user.id,
            )
        )
        if not collab.scalar_one_or_none():
            raise HTTPException(status_code=403, detail="権限がありません")

    # Dockerイメージ変更が伴う場合: 起動中VMを停止
    has_new_file = bool(file and file.filename and file.size and file.size > 0)
    has_new_url  = bool(image_url and image_url.strip())
    if has_new_file or has_new_url:
        from models import Environment, EnvStatus
        from services.smolvm import stop_vm
        env_result = await db.execute(
            select(Environment).where(
                Environment.image_id == image_id,
                Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
            )
        )
        running_envs = env_result.scalars().all()
        for env in running_envs:
            if env.vm_id:
                try:
                    await stop_vm(env.vm_id)
                except Exception:
                    pass
            env.status = EnvStatus.stopped
        if running_envs:
            await db.flush()

        # 新しいイメージを取得・保存
        if has_new_file:
            if not _allowed_image(file.filename or ""):
                raise HTTPException(status_code=400, detail=f"対応拡張子: {', '.join(ALLOWED_SUFFIXES)}")
            image_max_bytes = settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024
            if file.size and file.size > image_max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"ファイルサイズが上限を超えています（上限: {settings.UPLOAD_IMAGE_MAX_MB} MB）",
                )
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
                image.archive_path = save_path
                image.oci_ref = new_oci_ref
            except HTTPException:
                if os.path.exists(save_path):
                    os.remove(save_path)
                raise
            except Exception as e:
                if os.path.exists(save_path):
                    os.remove(save_path)
                raise HTTPException(status_code=500, detail=f"イメージ更新に失敗しました: {e}")
        else:
            ref = image_url.strip()
            try:
                new_oci_ref, save_path = await _docker_pull(ref)
                image.oci_ref = new_oci_ref
                image.archive_path = save_path
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"docker pull に失敗しました: {e}")

    # メタデータ更新
    if name is not None:
        image.name = name
    if description is not None:
        image.description = description
    if difficulty is not None:
        image.difficulty = difficulty
    if category is not None:
        image.category = category or None
    if timeout_minutes is not None:
        image.timeout_minutes = timeout_minutes
    if visibility is not None:
        image.visibility = visibility

    # README 更新
    has_new_readme_file = bool(readme and readme.filename)
    has_new_readme_text = bool(readme_text and readme_text.strip())
    if has_new_readme_file:
        if not _allowed_readme(readme.filename or ""):
            raise HTTPException(status_code=400, detail="README は .md または .txt のみ対応")
        readme_max_bytes = settings.UPLOAD_README_MAX_MB * 1024 * 1024
        if readme.size and readme.size > readme_max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"README ファイルサイズが上限を超えています（上限: {settings.UPLOAD_README_MAX_MB} MB）",
            )
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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Image).where(Image.id == image_id))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")
    if current_user.role != UserRole.admin and image.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="権限がありません")
    image.is_active = False
    await db.commit()
