from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

from database import get_db
from models import Environment, EnvStatus, Image, User, UserRole
from deps import get_current_user, require_username_set
from services.smolvm import start_vm, stop_vm
from services.network import allocate_ip, release_ip, _lock as _ip_lock
from services.watchdog import destroy_env
from config import settings

router = APIRouter(prefix="/api/envs", tags=["environments"])


class EnvResponse(BaseModel):
    id: int
    image_id: int
    image_name: str
    image_slug: str
    owner_username: Optional[str]
    ip_address: Optional[str]
    status: str
    started_at: datetime
    expires_at: datetime
    seconds_remaining: int
    timeout_minutes: int
    warning: bool

    model_config = {"from_attributes": True}


def _to_response(env: Environment) -> EnvResponse:
    now = datetime.utcnow()
    remaining = max(0, int((env.expires_at - now).total_seconds()))
    timeout = env.image.timeout_minutes if env.image and env.image.timeout_minutes else settings.DEFAULT_TIMEOUT_MINUTES
    owner = env.image.owner.username if env.image and env.image.owner else None
    return EnvResponse(
        id=env.id,
        image_id=env.image_id,
        image_name=env.image.name,
        image_slug=env.image.slug,
        owner_username=owner,
        ip_address=env.ip_address,
        status=env.status.value,
        started_at=env.started_at,
        expires_at=env.expires_at,
        seconds_remaining=remaining,
        timeout_minutes=timeout,
        warning=remaining < settings.TIMEOUT_WARNING_MINUTES * 60,
    )


@router.get("/", response_model=list[EnvResponse])
async def list_my_envs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.image).selectinload(Image.owner))
        .where(
            Environment.user_id == user.id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
        .order_by(Environment.started_at.desc())
    )
    envs = result.scalars().all()
    return [_to_response(e) for e in envs]


@router.post("/{image_id}/start", response_model=EnvResponse, status_code=status.HTTP_201_CREATED)
async def start_env(
    image_id: int,
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    img_result = await db.execute(
        select(Image).where(Image.id == image_id, Image.is_active == True)
    )
    image = img_result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")

    from routers.images import _can_see
    if not await _can_see(image, user, db):
        raise HTTPException(status_code=404, detail="イメージが見つかりません")

    timeout = image.timeout_minutes or settings.DEFAULT_TIMEOUT_MINUTES
    env: Environment | None = None

    async def _reserve(ip: str) -> None:
        """lock 保持中に上限チェックと Environment の仮記録を行う。"""
        nonlocal env
        # admin は上限チェックを免除
        if user.role != UserRole.admin:
            user_count = await db.execute(
                select(func.count()).where(
                    Environment.user_id == user.id,
                    Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
                )
            )
            if user_count.scalar() >= settings.MAX_ENVS_PER_USER:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"同時起動数の上限（{settings.MAX_ENVS_PER_USER}つ）に達しています",
                )
            total_count = await db.execute(
                select(func.count()).where(
                    Environment.status.in_([EnvStatus.starting, EnvStatus.running])
                )
            )
            if total_count.scalar() >= settings.MAX_ENVS_TOTAL:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="サーバが混雑しています。しばらくお待ちください",
                )
        now = datetime.utcnow()
        env = Environment(
            user_id=user.id,
            image_id=image.id,
            ip_address=ip,
            status=EnvStatus.starting,
            started_at=now,
            expires_at=now + timedelta(minutes=timeout),
        )
        db.add(env)
        await db.flush()

    ip = await allocate_ip(db, _reserve)
    if not ip or env is None:
        raise HTTPException(status_code=503, detail="利用可能なIPアドレスがありません")

    try:
        vm_result = await start_vm(
            oci_ref=image.oci_ref,
            ip=ip,
            cpu=image.cpu_limit,
            memory_mb=image.memory_limit_mb,
        )
        env.vm_id = vm_result.vm_id
        env.status = EnvStatus.running
    except Exception as e:
        env.status = EnvStatus.error
        await db.commit()
        raise HTTPException(status_code=500, detail=f"VM起動に失敗しました: {e}")

    await db.commit()
    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.image).selectinload(Image.owner))
        .where(Environment.id == env.id)
    )
    env = result.scalar_one()
    return _to_response(env)


@router.post("/{env_id}/extend")
async def extend_env(
    env_id: int,
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.image))
        .where(
            Environment.id == env_id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
    )
    env = result.scalar_one_or_none()
    if not env:
        raise HTTPException(status_code=404, detail="環境が見つかりません")
    if env.user_id != user.id and user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="権限がありません")

    # 延長時間 = イメージのタイムアウト設定（上限もこれに揃える）
    extend_minutes = env.image.timeout_minutes if env.image and env.image.timeout_minutes else settings.DEFAULT_TIMEOUT_MINUTES
    env.expires_at = datetime.utcnow() + timedelta(minutes=extend_minutes)
    env.extended_count += 1
    await db.commit()
    return {"message": f"{extend_minutes}分延長しました", "expires_at": env.expires_at.isoformat(), "extend_minutes": extend_minutes}


@router.post("/{env_id}/stop")
async def stop_env(
    env_id: int,
    user: User = Depends(require_username_set),
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
    if env.user_id != user.id and user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="権限がありません")

    await destroy_env(env)
    await db.commit()
    return {"message": "停止しました"}
