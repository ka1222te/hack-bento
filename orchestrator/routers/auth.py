from fastapi import APIRouter, Depends, HTTPException, Response, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime
from pydantic import BaseModel

from database import get_db
from models import User
from services import auth_local, auth_ldap, auth_oauth
from services.jwt_utils import create_access_token
from config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    needs_username_setup: bool = False


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    user = await auth_local.authenticate(db, body.username, body.password)
    if not user and settings.LDAP_ENABLED:
        user = await auth_ldap.authenticate(db, body.username, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ユーザ名またはパスワードが正しくありません")

    await db.execute(
        update(User).where(User.id == user.id).values(last_login=datetime.utcnow())
    )
    await db.commit()

    token = create_access_token(user.id, user.username, user.role.value, user.needs_username_setup)
    response.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax", max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
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


@router.get("/oauth/google")
async def oauth_google_login(request: Request):
    if not settings.GOOGLE_OAUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Google OAuth is disabled")
    redirect_uri = str(request.url_for("oauth_google_callback"))
    return await auth_oauth.oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/oauth/google/callback", name="oauth_google_callback")
async def oauth_google_callback(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    if not settings.GOOGLE_OAUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Google OAuth is disabled")
    token_data = await auth_oauth.oauth.google.authorize_access_token(request)
    userinfo = token_data.get("userinfo")
    if not userinfo:
        raise HTTPException(status_code=400, detail="OAuth認証に失敗しました")

    user = await auth_oauth.get_or_create_oauth_user(db, userinfo["email"], userinfo.get("name", ""))
    if not user:
        raise HTTPException(status_code=403, detail="許可されていないドメインです")

    await db.execute(
        update(User).where(User.id == user.id).values(last_login=datetime.utcnow())
    )
    await db.commit()

    token = create_access_token(user.id, user.username, user.role.value, user.needs_username_setup)
    redirect_url = "/setup-username" if user.needs_username_setup else "/"
    resp = RedirectResponse(url=redirect_url)
    resp.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax", max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return resp
