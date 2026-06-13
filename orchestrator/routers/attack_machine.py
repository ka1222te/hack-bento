import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from database import get_db
from models import Environment, EnvStatus, Image, User, UserRole
from deps import require_username_set
from services.smolvm import start_vm
from services.network import allocate_ip, _lock as _ip_lock, _ip_pool
from services.watchdog import destroy_env, claim_env_for_stop
from services.guacamole import build_guac_iframe_url
from services import firecracker_setup
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attack-machine", tags=["attack-machine"])

ATTACK_VNC_PORT = 5901
# Attack Machine の rootfs (firecracker_setup._ATTACK_ROOTFS_BUILD_SCRIPT) で
# hackbento ユーザの VNC パスワードとして固定設定している値
ATTACK_VNC_PASSWORD = "hackbento"


class AttackMachineResponse(BaseModel):
    id: int
    ip_address: Optional[str]
    status: str
    started_at: datetime
    expires_at: datetime
    seconds_remaining: int
    timeout_minutes: int

    model_config = {"from_attributes": True}


class AttackMachineStatusResponse(BaseModel):
    # rootfs (Kali) のバックグラウンド構築状況: idle/building/ready/failed
    rootfs_status: str
    rootfs_error: Optional[str] = None
    # rootfs_status == "ready" の場合のみ起動・操作可能
    machine: Optional[AttackMachineResponse] = None


def _to_response(env: Environment, timeout_minutes: int) -> AttackMachineResponse:
    now = datetime.utcnow()
    remaining = max(0, int((env.expires_at - now).total_seconds()))
    return AttackMachineResponse(
        id=env.id,
        ip_address=env.ip_address,
        status=env.status.value,
        started_at=env.started_at,
        expires_at=env.expires_at,
        seconds_remaining=remaining,
        timeout_minutes=timeout_minutes,
    )


async def _get_attack_image(db: AsyncSession) -> Image:
    result = await db.execute(
        select(Image).where(Image.is_attack_machine == True, Image.is_active == True)
    )
    image = result.scalar_one_or_none()
    if image is None:
        raise HTTPException(status_code=503, detail="Attack Machine は利用できません")
    return image


@router.get("/", response_model=AttackMachineStatusResponse)
async def get_my_attack_machine(
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Image).where(Image.is_attack_machine == True, Image.is_active == True)
    )
    image = result.scalar_one_or_none()
    if image is None:
        return AttackMachineStatusResponse(
            rootfs_status=firecracker_setup.attack_rootfs_build_status,
            rootfs_error=firecracker_setup.attack_rootfs_build_error,
            machine=None,
        )

    env_result = await db.execute(
        select(Environment).where(
            Environment.user_id == user.id,
            Environment.image_id == image.id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]),
        )
    )
    env = env_result.scalar_one_or_none()
    machine = None
    if env is not None:
        machine = _to_response(env, image.timeout_minutes or settings.ATTACK_MACHINE_TIMEOUT_MINUTES)
    return AttackMachineStatusResponse(rootfs_status="ready", rootfs_error=None, machine=machine)


@router.post("/start", response_model=AttackMachineResponse, status_code=status.HTTP_201_CREATED)
async def start_attack_machine(
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_attack_image(db)

    existing = await db.execute(
        select(Environment).where(
            Environment.user_id == user.id,
            Environment.image_id == image.id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Attack Machine は既に起動しています")

    timeout = image.timeout_minutes or settings.ATTACK_MACHINE_TIMEOUT_MINUTES
    env: Environment | None = None

    async def _reserve(ip: str) -> None:
        nonlocal env
        if user.role != UserRole.admin:
            user_count = await db.execute(
                select(func.count()).where(
                    Environment.user_id == user.id,
                    Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]),
                )
            )
            if user_count.scalar() >= settings.MAX_ENVS_PER_USER:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"同時起動数の上限（{settings.MAX_ENVS_PER_USER}つ）に達しています",
                )
            total_count = await db.execute(
                select(func.count()).where(
                    Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping])
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

    # start_vm() はVM起動完了まで数十秒かかる。ここでcommitして"starting"状態を
    # 確定させないと、他のリクエスト（GET /api/attack-machine/ のポーリングや
    # ページ再読み込み）からこのレコードが見えず、起動中にもかかわらず
    # 「VM起動」ボタンが再度押せる状態に見えてしまう。
    await db.commit()

    try:
        vm_result = await start_vm(
            image=image,
            ip=ip,
            cpu=image.cpu_limit,
            memory_mb=image.memory_limit_mb,
        )
    except Exception as e:
        logger.error(f"Attack Machine の起動に失敗しました env_id={env.id}: {e}")
        await destroy_env(env)
        await db.commit()
        raise HTTPException(status_code=500, detail="Attack Machine の起動に失敗しました")

    env.vm_id = vm_result.vm_id
    env.status = EnvStatus.running
    await db.commit()
    return _to_response(env, timeout)


@router.post("/extend")
async def extend_attack_machine(
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_attack_image(db)
    async with _ip_lock:
        result = await db.execute(
            select(Environment)
            .where(
                Environment.user_id == user.id,
                Environment.image_id == image.id,
                Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
            )
            .with_for_update()
        )
        env = result.scalar_one_or_none()
        if not env:
            raise HTTPException(status_code=404, detail="Attack Machine が見つかりません")

        max_extend = len(_ip_pool())
        if user.role != UserRole.admin and env.extended_count >= max_extend:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"延長回数の上限（{max_extend}回）に達しています",
            )

        extend_minutes = image.timeout_minutes or settings.ATTACK_MACHINE_TIMEOUT_MINUTES
        env.expires_at = datetime.utcnow() + timedelta(minutes=extend_minutes)
        env.extended_count += 1
        await db.commit()
    return {"message": f"{extend_minutes}分延長しました", "expires_at": env.expires_at.isoformat(), "extend_minutes": extend_minutes}


@router.post("/stop")
async def stop_attack_machine(
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_attack_image(db)
    result = await db.execute(
        select(Environment.id).where(
            Environment.user_id == user.id,
            Environment.image_id == image.id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
    )
    env_id = result.scalar_one_or_none()
    if env_id is None:
        raise HTTPException(status_code=404, detail="Attack Machine が見つかりません")

    env = await claim_env_for_stop(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="Attack Machine が見つかりません（既に停止処理中です）")

    await destroy_env(env)
    await db.commit()
    return {"message": "停止しました"}


@router.post("/connect")
async def connect_attack_machine(
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    image = await _get_attack_image(db)
    result = await db.execute(
        select(Environment).where(
            Environment.user_id == user.id,
            Environment.image_id == image.id,
            Environment.status == EnvStatus.running,
        )
    )
    env = result.scalar_one_or_none()
    if env is None or not env.ip_address:
        raise HTTPException(status_code=404, detail="Attack Machine が起動していません")

    connection_name = f"attack-machine-{user.id}"
    try:
        iframe_url = await build_guac_iframe_url(
            username=user.username,
            connection_name=connection_name,
            protocol="vnc",
            hostname=env.ip_address,
            port=ATTACK_VNC_PORT,
            guac_password=ATTACK_VNC_PASSWORD,
        )
    except Exception as e:
        logger.error(f"Guacamole トークンの取得に失敗しました user_id={user.id}: {e}")
        raise HTTPException(status_code=502, detail="Guacamole への接続に失敗しました")

    return {"iframe_url": iframe_url}
