import asyncio
import sys
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import User, AuthProvider, UserRole
from config import settings

sys.path.insert(0, "/app")


def _make_client():
    """ldap.py の LDAPClient を config の設定値から生成する。"""
    from ldap import LDAPClient
    return LDAPClient(
        ldap_uri=settings.LDAP_URI,
        top_domain=settings.LDAP_TOP_DOMAIN,
        mail_domain="ldap",  # メールは username@ldap で固定管理
        ou_user=settings.LDAP_OU_USER,
        ou_group=settings.LDAP_OU_GROUP,
        ssh_attr_name=None,
    )


def _check_reachable_sync() -> bool:
    try:
        from ldap import LDAPClient
        import ldap3
        server = ldap3.Server(settings.LDAP_URI, connect_timeout=3)
        conn = ldap3.Connection(server)
        conn.open()
        result = conn.closed is False
        conn.unbind()
        return result
    except Exception:
        return False


def _authenticate_sync(username: str, password: str) -> bool:
    try:
        client = _make_client()
        return client.authenticate(username, password)
    except Exception:
        return False


async def check_reachable() -> bool:
    """LDAPサーバに到達できるか非同期で確認する。"""
    if not settings.LDAP_ENABLED:
        return False
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _check_reachable_sync)


async def _unique_username(db: AsyncSession, base: str) -> str:
    """base が既存ユーザと衝突する場合は base2, base3 ... で一意にする。"""
    candidate = base
    suffix = 2
    while True:
        existing = await db.execute(select(User).where(User.username == candidate))
        if not existing.scalar_one_or_none():
            return candidate
        candidate = f"{base}{suffix}"
        suffix += 1


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    if not settings.LDAP_ENABLED:
        return None
    try:
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, _authenticate_sync, username, password)
        if not ok:
            return None

        # 既存ユーザを email or username で検索（再ログイン対応）
        email = f"{username}@ldap"
        result = await db.execute(
            select(User).where(
                User.email == email,
                User.auth_provider == AuthProvider.ldap,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            unique_name = await _unique_username(db, username)
            user = User(
                username=unique_name,
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
