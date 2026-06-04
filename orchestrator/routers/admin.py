from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from database import get_db
from models import User, UserRole, AuthProvider, Environment, EnvStatus
from reserved import is_reserved
from deps import require_admin
from services.auth_local import hash_password
from services.watchdog import destroy_env

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
    if is_reserved(body.username):
        raise HTTPException(status_code=400, detail=f"'{body.username}' は予約語のため使用できません")
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="このユーザ名は既に使用されています")
    user = User(
        username=body.username,
        email=body.email,
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
        user.hashed_password = hash_password(body.password)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/envs", response_model=list[EnvAdminResponse])
async def list_all_envs(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.user), selectinload(Environment.image))
        .where(Environment.status.in_([EnvStatus.starting, EnvStatus.running]))
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
    result = await db.execute(
        select(Environment).where(
            Environment.id == env_id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
    )
    env = result.scalar_one_or_none()
    if not env:
        raise HTTPException(status_code=404, detail="環境が見つかりません")
    await destroy_env(env)
    await db.commit()
    return {"message": "強制停止しました"}
