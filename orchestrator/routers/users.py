import re
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from pydantic import BaseModel, field_validator
from datetime import datetime

from database import get_db
from models import User, Image, ImageCollaborator, CollaboratorRole, Visibility, AuthProvider
from deps import get_current_user
from services.jwt_utils import create_access_token
from services.auth_local import hash_password
from config import settings
from reserved import is_reserved

router = APIRouter(prefix="/api/users", tags=["users"])

_VALID_USERNAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


class UsernameSetupRequest(BaseModel):
    username: str

    @field_validator("username")
    @classmethod
    def validate(cls, v: str) -> str:
        if not _VALID_USERNAME.match(v):
            raise ValueError("英数字・ハイフン・アンダースコアのみ使用可（1〜64文字）")
        if is_reserved(v):
            raise ValueError(f"'{v}' は予約語のため使用できません")
        return v


class UsernameSetupResponse(BaseModel):
    access_token: str
    username: str
    role: str


@router.post("/setup-username", response_model=UsernameSetupResponse)
async def setup_username(
    body: UsernameSetupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """初回ログイン時のユーザ名設定。一意チェックを行い確定する。"""
    if not user.needs_username_setup and user.username == body.username:
        # 既に設定済みで同じ名前なら OK
        token = create_access_token(user.id, user.username, user.role.value, False)
        return UsernameSetupResponse(access_token=token, username=user.username, role=user.role.value)

    # 他ユーザとの重複チェック
    existing = await db.execute(
        select(User).where(User.username == body.username, User.id != user.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="このユーザ名は既に使用されています")

    await db.execute(
        update(User).where(User.id == user.id).values(
            username=body.username,
            needs_username_setup=False,
        )
    )
    await db.commit()

    token = create_access_token(user.id, body.username, user.role.value, False)
    return UsernameSetupResponse(access_token=token, username=body.username, role=user.role.value)


@router.get("/search")
async def search_users(
    q: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """ユーザ名の前方一致検索（候補サジェスト用）。自分自身は除外。"""
    if len(q) < 1:
        return []
    from sqlalchemy import func
    result = await db.execute(
        select(User.username).where(
            User.username.ilike(f"{q}%"),
            User.id != current_user.id,
            User.is_active == True,
        ).limit(8)
    )
    return [row[0] for row in result.fetchall()]


@router.get("/check-username")
async def check_username(username: str, db: AsyncSession = Depends(get_db)):
    """ユーザ名の使用可否を確認する（認証不要）。自分自身は除外。"""
    if not _VALID_USERNAME.match(username):
        return {"available": False, "reason": "英数字・ハイフン・アンダースコアのみ使用可"}
    if is_reserved(username):
        return {"available": False, "reason": f"'{username}' は予約語のため使用できません"}
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        return {"available": False, "reason": "このユーザ名は既に使用されています"}
    return {"available": True}


@router.get("/check-username-for-setup")
async def check_username_for_setup(
    username: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """ユーザ名設定画面専用: 自分自身のユーザ名は使用可として扱う。"""
    if not _VALID_USERNAME.match(username):
        return {"available": False, "reason": "英数字・ハイフン・アンダースコアのみ使用可"}
    if is_reserved(username):
        return {"available": False, "reason": f"'{username}' は予約語のため使用できません"}
    existing = await db.execute(
        select(User).where(User.username == username, User.id != current_user.id)
    )
    if existing.scalar_one_or_none():
        return {"available": False, "reason": "このユーザ名は既に使用されています"}
    return {"available": True}


# ---- パスワード変更 ----

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.auth_provider != AuthProvider.local:
        raise HTTPException(
            status_code=403,
            detail="パスワード変更はローカルユーザのみ使用可能です",
        )
    from services.auth_local import verify_password
    if not current_user.hashed_password or not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="現在のパスワードが正しくありません")
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="新しいパスワードが一致しません")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="パスワードは8文字以上にしてください")
    await db.execute(
        update(User).where(User.id == current_user.id).values(
            hashed_password=hash_password(body.new_password)
        )
    )
    await db.commit()


# ---- コラボレーター管理 ----

class CollaboratorAdd(BaseModel):
    username: str
    role: str = "read"


class CollaboratorResponse(BaseModel):
    id: int
    username: str
    role: str
    added_at: datetime
    model_config = {"from_attributes": True}


@router.get("/{owner}/{slug}/collaborators", response_model=list[CollaboratorResponse])
async def list_collaborators(
    owner: str,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_image_as_owner_or_admin(owner, slug, current_user, db)
    result = await db.execute(
        select(ImageCollaborator, User.username)
        .join(User, ImageCollaborator.user_id == User.id)
        .where(ImageCollaborator.image_id == image.id)
    )
    rows = result.all()
    return [
        CollaboratorResponse(id=c.id, username=uname, role=c.role.value, added_at=c.added_at)
        for c, uname in rows
    ]


@router.post("/{owner}/{slug}/collaborators", status_code=status.HTTP_201_CREATED)
async def add_collaborator(
    owner: str,
    slug: str,
    body: CollaboratorAdd,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_image_as_owner_or_admin(owner, slug, current_user, db)

    target = await db.execute(select(User).where(User.username == body.username))
    target_user = target.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="ユーザが見つかりません")
    if target_user.id == image.owner_id:
        raise HTTPException(status_code=400, detail="オーナー自身はコラボレーターにできません")

    exists = await db.execute(
        select(ImageCollaborator).where(
            ImageCollaborator.image_id == image.id,
            ImageCollaborator.user_id == target_user.id,
        )
    )
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="既にコラボレーターです")

    role_map = {r.value: r for r in CollaboratorRole}
    role = role_map.get(body.role)
    if role is None:
        raise HTTPException(status_code=422, detail=f"無効なロール: {body.role}")
    collab = ImageCollaborator(image_id=image.id, user_id=target_user.id, role=role)
    db.add(collab)
    await db.commit()
    return {"message": f"{body.username} をコラボレーターに追加しました"}


@router.delete("/{owner}/{slug}/collaborators/{username}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_collaborator(
    owner: str,
    slug: str,
    username: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_image_as_owner_or_admin(owner, slug, current_user, db)

    target = await db.execute(select(User).where(User.username == username))
    target_user = target.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="ユーザが見つかりません")

    collab = await db.execute(
        select(ImageCollaborator).where(
            ImageCollaborator.image_id == image.id,
            ImageCollaborator.user_id == target_user.id,
        )
    )
    c = collab.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="コラボレーターではありません")
    await db.delete(c)
    await db.commit()


# ---- visibility 変更 ----

class VisibilityUpdate(BaseModel):
    visibility: Visibility


@router.patch("/{owner}/{slug}/visibility")
async def update_visibility(
    owner: str,
    slug: str,
    body: VisibilityUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_image_as_owner_or_admin(owner, slug, current_user, db)
    image.visibility = body.visibility
    await db.commit()
    return {"visibility": body.visibility.value}


# ---- ヘルパー ----

async def _get_image_as_owner_or_admin(
    owner: str, slug: str, current_user: User, db: AsyncSession
) -> Image:
    from models import UserRole
    owner_user = await db.execute(select(User).where(User.username == owner))
    owner_obj = owner_user.scalar_one_or_none()
    if not owner_obj:
        raise HTTPException(status_code=404, detail="ユーザが見つかりません")

    image = await db.execute(
        select(Image).where(Image.owner_id == owner_obj.id, Image.slug == slug, Image.is_active == True)
    )
    img = image.scalar_one_or_none()
    if not img:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")

    if current_user.role != UserRole.admin and img.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="権限がありません")
    return img
