from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text
from config import settings
from models import Base
import logging

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _migrate(conn):
    """既存 DB へのカラム追加など create_all で対応できない差分を適用する。"""
    # (table, column, DDL)
    migrations = [
        ("images",  "archive_path",         "ALTER TABLE images  ADD COLUMN archive_path         VARCHAR(512)"),
        ("images",  "owner_id",             "ALTER TABLE images  ADD COLUMN owner_id             INTEGER REFERENCES users(id)"),
        ("images",  "visibility",           "ALTER TABLE images  ADD COLUMN visibility           VARCHAR(16) NOT NULL DEFAULT 'protected'"),
        ("users",   "display_name",         "ALTER TABLE users   ADD COLUMN display_name         VARCHAR(128)"),
        ("users",   "needs_username_setup", "ALTER TABLE users   ADD COLUMN needs_username_setup BOOLEAN NOT NULL DEFAULT 0"),
        ("images",  "readme_path",          "ALTER TABLE images  ADD COLUMN readme_path          VARCHAR(512)"),
    ]
    for table, column, ddl in migrations:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        columns = [row[1] for row in result.fetchall()]
        if column not in columns:
            await conn.execute(text(ddl))
            logger.info(f"Migration applied: {table}.{column}")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
