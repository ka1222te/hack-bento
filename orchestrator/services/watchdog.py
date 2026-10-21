import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import AsyncSessionLocal
from models import Environment, EnvStatus
from services.smolvm import stop_vm, vm_is_alive
from services.network import release_ip

logger = logging.getLogger(__name__)

_STARTING_TIMEOUT_MINUTES = 5
# running 遷移直後の一時的な未登録・計測誤差で誤検知しないための猶予
_CRASH_GRACE_SECONDS = 20


async def claim_env_for_stop(db: AsyncSession, env_id: int) -> Environment | None:
    """env_id の Environment を行ロック付きで取得し、停止可能な状態であれば
    EnvStatus.stopping へ遷移させて返す。

    SELECT ... FOR UPDATE で行をロックした上で状態を確認・遷移するため、
    同一環境に対するユーザの停止操作・管理者の強制停止・watchdog のタイムアウト処理が
    同時に発生しても、destroy_env が二重に実行されることはない
    （後続の呼び出しは status が既に stopping/stopped になっているため None を返す）。
    """
    result = await db.execute(
        select(Environment)
        .where(
            Environment.id == env_id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
        .with_for_update()
    )
    env = result.scalar_one_or_none()
    if not env:
        return None
    env.status = EnvStatus.stopping
    await db.flush()
    return env


async def destroy_env(env: Environment) -> None:
    if env.vm_id:
        await stop_vm(env.vm_id)
    if env.ip_address:
        await release_ip(env.ip_address)
    env.status = EnvStatus.stopped


async def run_watchdog() -> None:
    logger.info("Watchdog started")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                now = datetime.utcnow()

                # 期限切れ running 環境を停止（is_unlimited の環境はタイムアウト判定の対象外）
                result = await db.execute(
                    select(Environment.id).where(
                        Environment.status == EnvStatus.running,
                        Environment.is_unlimited == False,
                        Environment.expires_at <= now,
                    )
                )
                expired_ids = result.scalars().all()
                for env_id in expired_ids:
                    try:
                        env = await claim_env_for_stop(db, env_id)
                        if env is None:
                            continue  # 既に他の経路で停止処理が開始されている
                        logger.info(f"Timeout: env_id={env.id} vm_id={env.vm_id}")
                        await destroy_env(env)
                        await db.commit()
                    except Exception as e:
                        logger.error(f"Failed to destroy env_id={env_id}: {e}")
                        await db.rollback()

                # starting のまま一定時間経過した環境を停止（起動失敗の取りこぼし対策）
                stuck_threshold = now - timedelta(minutes=_STARTING_TIMEOUT_MINUTES)
                stuck_result = await db.execute(
                    select(Environment.id).where(
                        Environment.status == EnvStatus.starting,
                        Environment.started_at <= stuck_threshold,
                    )
                )
                stuck_ids = stuck_result.scalars().all()
                for env_id in stuck_ids:
                    try:
                        env = await claim_env_for_stop(db, env_id)
                        if env is None:
                            continue
                        logger.warning(f"Stuck starting env cleaned up: env_id={env.id} vm_id={env.vm_id}")
                        await destroy_env(env)
                        await db.commit()
                    except Exception as e:
                        logger.error(f"Failed to cleanup stuck env_id={env_id}: {e}")
                        await db.rollback()

                # クラッシュ検知: DB 上 running だが VM プロセスが死んでいる環境を回収する。
                # カーネルパニック（exploit 失敗）やゲストの自発 poweroff で Firecracker が
                # 終了した場合、放置すると IP と同時起動枠を占有し続けるため停止・解放する。
                # running 遷移直後の誤検知を避けるため started_at に猶予を設ける。
                crash_threshold = now - timedelta(seconds=_CRASH_GRACE_SECONDS)
                running_result = await db.execute(
                    select(Environment.id, Environment.vm_id).where(
                        Environment.status == EnvStatus.running,
                        Environment.started_at <= crash_threshold,
                    )
                )
                for env_id, vm_id in running_result.all():
                    if not vm_id or vm_is_alive(vm_id):
                        continue
                    try:
                        env = await claim_env_for_stop(db, env_id)
                        if env is None:
                            continue
                        logger.warning(f"Crashed VM detected (process gone): env_id={env.id} vm_id={vm_id[:8]}")
                        await destroy_env(env)
                        await db.commit()
                    except Exception as e:
                        logger.error(f"Failed to reap crashed env_id={env_id}: {e}")
                        await db.rollback()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        await asyncio.sleep(30)
