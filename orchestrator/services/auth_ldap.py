from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import User, AuthProvider, UserRole
from config import settings
import ldap3


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    if not settings.LDAP_ENABLED:
        return None
    try:
        server = ldap3.Server(settings.LDAP_SERVER, get_info=ldap3.ALL)
        conn = ldap3.Connection(
            server,
            settings.LDAP_BIND_DN,
            settings.LDAP_BIND_PASSWORD,
            auto_bind=True,
        )
        search_filter = settings.LDAP_USER_FILTER.format(username=username)
        conn.search(settings.LDAP_BASE_DN, search_filter, attributes=["mail", "displayName"])
        if not conn.entries:
            return None
        user_dn = conn.entries[0].entry_dn
        # LDAP ユーザのメールは "username@ldap" で区別する
        email = f"{username}@ldap"

        user_conn = ldap3.Connection(server, user_dn, password)
        if not user_conn.bind():
            return None

        result = await db.execute(
            select(User).where(
                User.username == username,
                User.auth_provider == AuthProvider.ldap,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            # 初回ログイン: ユーザ名設定が必要なフラグを立てる
            user = User(
                username=username,
                email=email,
                auth_provider=AuthProvider.ldap,
                role=UserRole.user,
                is_active=True,
                needs_username_setup=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
        return user if user.is_active else None
    except Exception:
        return None
