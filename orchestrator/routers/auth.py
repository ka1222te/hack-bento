import secrets
import httpx
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from datetime import datetime
from pydantic import BaseModel

from database import get_db
from models import User
from services import auth_local, auth_ldap, auth_oauth
from services.jwt_utils import create_access_token
from deps import get_current_user
from config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    needs_username_setup: bool = False


def _callback_url() -> str:
    port = settings.PORT
    host = settings.DOMAIN
    scheme = settings.SCHEME
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}/api/auth/oauth/google/callback"
    return f"{scheme}://{host}:{port}/api/auth/oauth/google/callback"


def _issue_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax",
        secure=(settings.SCHEME == "https"),
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    user = await auth_local.authenticate(db, body.username, body.password)
    if not user and settings.LDAP_ENABLED:
        user = await auth_ldap.authenticate(db, body.username, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ユーザ名またはパスワードが正しくありません")

    await db.execute(update(User).where(User.id == user.id).values(last_login=datetime.utcnow()))
    await db.commit()

    token = create_access_token(user.id, user.username, user.role.value, user.needs_username_setup)
    _issue_cookie(response, token)
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role.value,
        needs_username_setup=user.needs_username_setup,
    )


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "ログアウトしました"}


@router.get("/ldap-status")
async def ldap_status():
    if not settings.LDAP_ENABLED:
        return {"enabled": False, "reachable": False}
    reachable = await auth_ldap.check_reachable()
    return {"enabled": True, "reachable": reachable}


@router.get("/me")
async def me(request: Request, current_user: User = Depends(get_current_user)):
    result: dict = {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role.value,
        "auth_provider": current_user.auth_provider.value,
        "needs_username_setup": current_user.needs_username_setup,
    }
    # BearerヘッダなしはCookie認証（OAuthログイン直後）→ localStorage同期用にトークンを返す
    if not request.headers.get("authorization", "").lower().startswith("bearer "):
        result["access_token"] = create_access_token(
            current_user.id, current_user.username,
            current_user.role.value, current_user.needs_username_setup,
        )
    return result


# ---- Google OAuth2 (authlib不使用・httpx直接実装) ----

@router.get("/oauth/google")
async def oauth_google_login():
    if not settings.GOOGLE_OAUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Google OAuth is disabled")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _callback_url(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    }
    url = GOOGLE_AUTH_URL + "?" + urlencode(params)
    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie("oauth_state", state, httponly=True, samesite="lax", max_age=300, secure=(settings.SCHEME == "https"))
    return resp


@router.get("/oauth/google/callback", name="oauth_google_callback")
async def oauth_google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    if not settings.GOOGLE_OAUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Google OAuth is disabled")

    # state検証
    state_cookie = request.cookies.get("oauth_state")
    state_param  = request.query_params.get("state")
    if not state_cookie or state_cookie != state_param:
        raise HTTPException(status_code=400, detail="OAuth stateが不正です")

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="codeがありません")

    # authorization code → access token
    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _callback_url(),
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="トークン取得に失敗しました")
        token_data = token_resp.json()

        # userinfo取得
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="ユーザ情報の取得に失敗しました")
        userinfo = userinfo_resp.json()

    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="メールアドレスを取得できませんでした")
    if not userinfo.get("email_verified", False):
        raise HTTPException(status_code=400, detail="メールアドレスが確認されていません")

    user = await auth_oauth.get_or_create_oauth_user(db, email, userinfo.get("name", ""))
    if not user:
        raise HTTPException(status_code=403, detail="許可されていないドメインです")

    await db.execute(update(User).where(User.id == user.id).values(last_login=datetime.utcnow()))
    await db.commit()

    token = create_access_token(user.id, user.username, user.role.value, user.needs_username_setup)
    redirect_url = "/setup-username" if user.needs_username_setup else "/"
    resp = RedirectResponse(url=redirect_url, status_code=302)
    resp.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax",
        secure=(settings.SCHEME == "https"),
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    resp.delete_cookie("oauth_state")
    return resp
