import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from database import get_db, get_db_readonly
from models import Environment, EnvStatus, Image, User, UserRole
from deps import require_username_set
from services.smolvm import start_vm, stop_vm
from services.network import allocate_ip, _lock as _ip_lock, _ip_pool
from services.watchdog import destroy_env, claim_env_for_stop
from services.guacamole import build_guac_iframe_url
from services import vm_assets
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/attack-machine", tags=["attack-machine"])

ATTACK_VNC_PORT = 5901
# Attack Machine の rootfs (services/attack_machine_assets.py の _ATTACK_ROOTFS_BUILD_SCRIPT) で
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
    db: AsyncSession = Depends(get_db_readonly),
):
    result = await db.execute(
        select(Image).where(Image.is_attack_machine == True, Image.is_active == True)
    )
    image = result.scalar_one_or_none()
    if image is None:
        return AttackMachineStatusResponse(
            rootfs_status=vm_assets.attack_rootfs_build_status,
            rootfs_error=vm_assets.attack_rootfs_build_error,
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

    # vm_id だけを先に確定・commitする。これにより、確定前に停止要求
    # （watchdogのstuck-starting処理含む）が先行していても、その後の
    # destroy_env が vm_id を使って実VM/TAP/cgroupを解放できる（孤児防止）。
    env.vm_id = vm_result.vm_id
    await db.commit()

    result = await db.execute(
        select(Environment).where(Environment.id == env.id).with_for_update()
    )
    env = result.scalar_one()
    if env.status != EnvStatus.starting:
        await db.commit()
        await stop_vm(vm_result.vm_id)
        raise HTTPException(status_code=409, detail="起動中にAttack Machineは停止されました")

    env.status = EnvStatus.running
    # start_vm() の起動処理（数十秒かかる場合がある）の間はタイマーを消費させず、
    # running に遷移した時点から timeout 分のカウントを開始する。
    env.expires_at = datetime.utcnow() + timedelta(minutes=timeout)
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
    # starting 中は vm_id が未確定で destroy_env が実VM/TAP/cgroupを解放できず
    # 孤児リソースを生むため、running になってからのみ停止を許可する
    # （starting が長時間続く場合は watchdog の stuck-starting 処理に委ねる）。
    result = await db.execute(
        select(Environment.id, Environment.status).where(
            Environment.user_id == user.id,
            Environment.image_id == image.id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Attack Machine が見つかりません")
    env_id, env_status = row
    if env_status == EnvStatus.starting:
        raise HTTPException(status_code=409, detail="VM起動中は停止できません。起動完了までお待ちください")

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
