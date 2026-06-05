import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select
from database import AsyncSessionLocal
from models import Environment, EnvStatus
from services.smolvm import stop_vm
from services.network import release_ip

logger = logging.getLogger(__name__)

_STARTING_TIMEOUT_MINUTES = 5


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

                # 期限切れ running 環境を停止
                result = await db.execute(
                    select(Environment).where(
                        Environment.status == EnvStatus.running,
                        Environment.expires_at <= now,
                    )
                )
                expired = result.scalars().all()
                for env in expired:
                    logger.info(f"Timeout: env_id={env.id} vm_id={env.vm_id}")
                    try:
                        await destroy_env(env)
                        await db.commit()
                    except Exception as e:
                        logger.error(f"Failed to destroy env_id={env.id}: {e}")
                        await db.rollback()

                # starting のまま一定時間経過した環境を停止（起動失敗の取りこぼし対策）
                stuck_threshold = now - timedelta(minutes=_STARTING_TIMEOUT_MINUTES)
                stuck_result = await db.execute(
                    select(Environment).where(
                        Environment.status == EnvStatus.starting,
                        Environment.started_at <= stuck_threshold,
                    )
                )
                stuck = stuck_result.scalars().all()
                for env in stuck:
                    logger.warning(f"Stuck starting env cleaned up: env_id={env.id} vm_id={env.vm_id}")
                    try:
                        await destroy_env(env)
                        await db.commit()
                    except Exception as e:
                        logger.error(f"Failed to cleanup stuck env_id={env.id}: {e}")
                        await db.rollback()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        await asyncio.sleep(30)
