import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import User, AuthProvider, UserRole
from config import settings
from reserved import is_reserved

_VALID_USERNAME = re.compile(r'^[a-zA-Z0-9_-]+$')


def _sanitize_username(raw: str) -> str:
    """メールのローカルパートをユーザ名として使える形に変換する。"""
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '-', raw)
    return cleaned[:64].strip('-') or "user"


async def get_or_create_oauth_user(db: AsyncSession, email: str, name: str) -> User | None:
    if settings.OAUTH_ALLOWED_DOMAINS:
        domain = email.split("@")[-1]
        if domain not in settings.OAUTH_ALLOWED_DOMAINS:
            return None

    result = await db.execute(
        select(User).where(
            User.email == email,
            User.auth_provider == AuthProvider.oauth,
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        # 初回: メールのローカルパートを候補ユーザ名にして一意チェック
        base = _sanitize_username(email.split("@")[0])
        # 予約語だった場合は "-u" を付けてベース名を変える
        if is_reserved(base):
            base = f"{base}-u"
        username = base
        suffix = 1
        while True:
            existing = await db.execute(select(User).where(User.username == username))
            if not existing.scalar_one_or_none():
                break
            username = f"{base}-{suffix}"
            suffix += 1

        user = User(
            username=username,
            email=email,
            display_name=name or None,
            auth_provider=AuthProvider.oauth,
            role=UserRole.user,
            is_active=True,
            needs_username_setup=True,  # 初回ログイン時にユーザ名設定画面へ
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user if user.is_active else None
