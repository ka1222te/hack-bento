from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import User, AuthProvider

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    result = await db.execute(
        select(User).where(
            User.username == username,
            User.auth_provider == AuthProvider.local,
            User.is_active == True,
        )
    )
    user = result.scalar_one_or_none()
    if user and user.hashed_password and verify_password(password, user.hashed_password):
        return user
    return None
