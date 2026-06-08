import os
import re
import uuid
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from config import settings
from database import get_db
from models import (
    User, UserRole, AuthProvider, Environment, EnvStatus, Image, ImageCollaborator,
    DefaultKernelAsset, DefaultRootfsAsset, RootfsConversionStatus,
)
from reserved import is_reserved
from deps import require_admin
from services.firecracker_setup import DEFAULTS_DIR, _detect_archive_kind

_VALID_USERNAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
from services.auth_local import hash_password
from services.watchdog import destroy_env, claim_env_for_stop

router = APIRouter(prefix="/api/admin", tags=["admin"])

_DEFAULT_ASSET_KINDS = {
    "kernels": {"model": DefaultKernelAsset, "suffix": "vmlinux", "label": "ゲストカーネル"},
    "rootfs": {"model": DefaultRootfsAsset, "suffix": "rootfs.ext4", "label": "rootfs"},
}


class DefaultAssetResponse(BaseModel):
    id: int
    label: str
    file_path: str
    is_active: bool
    created_at: datetime
    conversion_status: str = RootfsConversionStatus.ready.value
    conversion_error: Optional[str] = None

    model_config = {"from_attributes": True}

    @classmethod
    def model_validate(cls, obj, **kwargs):
        # DefaultKernelAsset には conversion_status 系カラムが存在しないため、
        # 無ければ「変換不要」として扱う（rootfs専用フィールドをモデル間で共有しているため）
        if not hasattr(obj, "conversion_status"):
            return cls(
                id=obj.id,
                label=obj.label,
                file_path=obj.file_path,
                is_active=obj.is_active,
                created_at=obj.created_at,
            )
        return super().model_validate(obj, **kwargs)


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str]
    role: str
    auth_provider: str
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime]

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    password: str
    role: UserRole = UserRole.user


class UserUpdate(BaseModel):
    email: Optional[str] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class EnvAdminResponse(BaseModel):
    id: int
    user_id: int
    username: str
    image_name: str
    ip_address: Optional[str]
    status: str
    started_at: datetime
    expires_at: datetime

    model_config = {"from_attributes": True}


@router.get("/users", response_model=list[UserResponse])
async def list_users(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.id))
    return result.scalars().all()


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not _VALID_USERNAME.match(body.username):
        raise HTTPException(status_code=400, detail="ユーザ名は英数字・ハイフン・アンダースコアのみ使用可（1〜64文字）")
    if is_reserved(body.username):
        raise HTTPException(status_code=400, detail=f"'{body.username}' は予約語のため使用できません")
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="このユーザ名は既に使用されています")
    user = User(
        username=body.username,
        email=body.email if body.email else f"{body.username}@local",
        hashed_password=hash_password(body.password),
        role=body.role,
        auth_provider=AuthProvider.local,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    body: UserUpdate,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="ユーザが見つかりません")
    if user_id == current_admin.id:
        if body.is_active is False:
            raise HTTPException(status_code=400, detail="自分自身を無効化することはできません")
        if body.role is not None and body.role != UserRole.admin:
            raise HTTPException(status_code=400, detail="自分自身のロールを降格することはできません")
    if body.email is not None:
        user.email = body.email
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password:
        if user.auth_provider != AuthProvider.local:
            raise HTTPException(status_code=400, detail="パスワード変更はローカルユーザのみ可能です")
        user.hashed_password = hash_password(body.password)
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="自分自身を削除することはできません")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="ユーザが見つかりません")
    if user.role == UserRole.admin:
        raise HTTPException(status_code=400, detail="管理者ユーザは削除できません。先に一般ユーザに降格してください")

    # 起動中の環境を強制停止（行ロックで watchdog 等との二重停止を防ぐ）
    env_result = await db.execute(
        select(Environment.id).where(
            Environment.user_id == user_id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
    )
    for env_id in env_result.scalars().all():
        env = await claim_env_for_stop(db, env_id)
        if env is not None:
            await destroy_env(env)

    # ユーザが所有するイメージのコラボレーター・環境・イメージ本体を削除
    img_result = await db.execute(select(Image).where(Image.owner_id == user_id))
    for img in img_result.scalars().all():
        await db.execute(delete(ImageCollaborator).where(ImageCollaborator.image_id == img.id))
        # 他ユーザが起動中のコンテナも含めて停止してからレコードを削除
        running_result = await db.execute(
            select(Environment.id).where(
                Environment.image_id == img.id,
                Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
            )
        )
        for env_id in running_result.scalars().all():
            try:
                env = await claim_env_for_stop(db, env_id)
                if env is not None:
                    await destroy_env(env)
            except Exception:
                pass
        await db.execute(delete(Environment).where(Environment.image_id == img.id))
        # アーカイブ・README・カーネル/rootfs の実ファイルを削除（パストラバーサル対策付き）
        from routers.images import _safe_remove
        _safe_remove(img.archive_path)
        _safe_remove(getattr(img, "readme_path", None))
        _safe_remove(getattr(img, "kernel_path", None))
        _safe_remove(getattr(img, "rootfs_path", None))
        await db.delete(img)

    # コラボレーター参加分を削除
    await db.execute(delete(ImageCollaborator).where(ImageCollaborator.user_id == user_id))
    # 残存環境を削除
    await db.execute(delete(Environment).where(Environment.user_id == user_id))

    await db.delete(user)
    await db.commit()


@router.get("/envs", response_model=list[EnvAdminResponse])
async def list_all_envs(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.user), selectinload(Environment.image))
        .where(Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]))
        .order_by(Environment.started_at.desc())
    )
    envs = result.scalars().all()
    out = []
    for env in envs:
        out.append(EnvAdminResponse(
            id=env.id,
            user_id=env.user_id,
            username=env.user.username,
            image_name=env.image.name,
            ip_address=env.ip_address,
            status=env.status.value,
            started_at=env.started_at,
            expires_at=env.expires_at,
        ))
    return out


@router.post("/envs/{env_id}/force-stop")
async def force_stop_env(
    env_id: int,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    env = await claim_env_for_stop(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="環境が見つかりません（既に停止処理中の可能性があります）")
    await destroy_env(env)
    await db.commit()
    return {"message": "強制停止しました"}


@router.get("/default-assets/{kind}", response_model=list[DefaultAssetResponse])
async def list_default_assets(
    kind: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    info = _DEFAULT_ASSET_KINDS.get(kind)
    if not info:
        raise HTTPException(status_code=400, detail="kind は kernels または rootfs を指定してください")
    result = await db.execute(select(info["model"]).order_by(info["model"].created_at.desc()))
    return result.scalars().all()


@router.post("/default-assets/{kind}", response_model=DefaultAssetResponse, status_code=status.HTTP_201_CREATED)
async def create_default_asset(
    kind: str,
    label: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    info = _DEFAULT_ASSET_KINDS.get(kind)
    if not info:
        raise HTTPException(status_code=400, detail="kind は kernels または rootfs を指定してください")

    label = label.strip()
    if not label or len(label) > 128:
        raise HTTPException(status_code=400, detail="label は 1〜128 文字で指定してください")
    if not (file and file.filename and file.size and file.size > 0):
        raise HTTPException(status_code=400, detail="ファイルを指定してください")

    os.makedirs(DEFAULTS_DIR, exist_ok=True)
    dest_path = os.path.join(DEFAULTS_DIR, f"{uuid.uuid4()}-{info['suffix']}")
    image_max_bytes = settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024
    try:
        written = 0
        with open(dest_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
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
        raise HTTPException(status_code=500, detail="ファイルの保存に失敗しました")

    if kind == "rootfs":
        kind_detected = _detect_archive_kind(dest_path)
        if kind_detected == "docker-save":
            os.remove(dest_path)
            raise HTTPException(
                status_code=400,
                detail="docker save 形式のアーカイブ（複数レイヤー構成のOCIイメージ）は対応していません。"
                       "`docker export` 等でファイルシステム単体の tar を作成してアップロードしてください。",
            )
        if kind_detected == "tar":
            tar_path = dest_path
            ext4_path = os.path.join(DEFAULTS_DIR, f"{uuid.uuid4()}-{info['suffix']}")
            asset = info["model"](
                label=label,
                file_path=ext4_path,
                created_by=current_user.id,
                conversion_status=RootfsConversionStatus.pending.value,
                source_archive_path=tar_path,
            )
            db.add(asset)
            await db.commit()
            await db.refresh(asset)
            return asset

    asset = info["model"](label=label, file_path=dest_path, created_by=current_user.id)
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


@router.delete("/default-assets/{kind}/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_default_asset(
    kind: str,
    asset_id: int,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    info = _DEFAULT_ASSET_KINDS.get(kind)
    if not info:
        raise HTTPException(status_code=400, detail="kind は kernels または rootfs を指定してください")
    AssetModel = info["model"]
    asset = await db.get(AssetModel, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="資産が見つかりません")

    ref_column = Image.default_kernel_asset_id if kind == "kernels" else Image.default_rootfs_asset_id
    ref_result = await db.execute(
        select(Image.id).where(ref_column == asset_id, Image.is_active == True)
    )
    if ref_result.first():
        raise HTTPException(status_code=400, detail="このデフォルト資産を使用しているプロジェクトが存在するため削除できません")

    paths_to_remove = [asset.file_path]
    source_archive_path = getattr(asset, "source_archive_path", None)
    if source_archive_path:
        paths_to_remove.append(source_archive_path)

    defaults_dir_real = os.path.realpath(DEFAULTS_DIR) + os.sep
    for path in paths_to_remove:
        if path and os.path.realpath(path).startswith(defaults_dir_real):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    await db.delete(asset)
    await db.commit()
