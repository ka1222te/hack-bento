from datetime import datetime, timedelta
from jose import JWTError, jwt
from config import settings

ALGORITHM = "HS256"


def create_access_token(user_id: int, username: str, role: str, needs_username_setup: bool = False) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {
            "sub": str(user_id),
            "username": username,
            "role": role,
            "needs_username_setup": needs_username_setup,
            "exp": expire,
        },
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
