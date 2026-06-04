import asyncio
import logging
from datetime import datetime
from sqlalchemy import select
from database import AsyncSessionLocal
from models import Environment, EnvStatus
from services.smolvm import stop_vm
from services.network import release_ip

logger = logging.getLogger(__name__)


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
                result = await db.execute(
                    select(Environment).where(
                        Environment.status == EnvStatus.running,
                        Environment.expires_at <= now,
                    )
                )
                expired = result.scalars().all()
                for env in expired:
                    logger.info(f"Timeout: env_id={env.id} vm_id={env.vm_id}")
                    await destroy_env(env)
                if expired:
                    await db.commit()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        await asyncio.sleep(30)
