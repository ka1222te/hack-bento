import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import settings
from database import init_db, AsyncSessionLocal
from models import User, UserRole, AuthProvider, Image, Visibility, ImageCollaborator
from services.auth_local import hash_password
from services.watchdog import run_watchdog
from services.network import ensure_macvlan_network
from routers import auth, envs, images, admin, users
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from fastapi import Depends
from fastapi.responses import Response as FastAPIResponse

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _seed_admin()
    await ensure_macvlan_network()
    task = asyncio.create_task(run_watchdog())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _seed_admin():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == "admin"))
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                username="admin",
                email="admin@hackbento.local",
                hashed_password=hash_password("admin"),
                role=UserRole.admin,
                auth_provider=AuthProvider.local,
                is_active=True,
                needs_username_setup=False,
            )
            db.add(user)
            await db.commit()
            logging.getLogger(__name__).warning("Default admin user created. Please change the password immediately after first login.")


app = FastAPI(title=settings.APP_TITLE, lifespan=lifespan)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    https_only=(settings.SCHEME == "https"),
    same_site="lax",
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

app.include_router(auth.router)
app.include_router(envs.router)
app.include_router(images.router)
app.include_router(admin.router)
app.include_router(users.router)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


async def _get_request_user(request: Request) -> Optional[User]:
    """リクエストのトークン（Cookie or Bearer）からユーザを取得する。"""
    from services.jwt_utils import decode_token
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None
    try:
        payload = decode_token(token)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.id == int(payload["sub"]), User.is_active == True)
            )
            return result.scalar_one_or_none()
    except Exception:
        return None


async def _can_view_project(owner: str, slug: str, user: Optional[User]) -> bool:
    """owner/slug のプロジェクトを user が閲覧できるか確認する。"""
    async with AsyncSessionLocal() as db:
        owner_result = await db.execute(select(User).where(User.username == owner))
        owner_obj = owner_result.scalar_one_or_none()
        if not owner_obj:
            return False
        img_result = await db.execute(
            select(Image).where(
                Image.owner_id == owner_obj.id,
                Image.slug == slug,
                Image.is_active == True,
            )
        )
        image = img_result.scalar_one_or_none()
        if not image:
            return False
        if image.visibility == Visibility.public:
            return True
        if user is None:
            return False
        if user.role == UserRole.admin:
            return True
        if image.visibility == Visibility.protected:
            return True
        if image.owner_id == user.id:
            return True
        collab = await db.execute(
            select(ImageCollaborator).where(
                ImageCollaborator.image_id == image.id,
                ImageCollaborator.user_id == user.id,
            )
        )
        return collab.scalar_one_or_none() is not None


async def _can_edit_project(owner: str, slug: str, user: Optional[User]) -> bool:
    """owner/slug のプロジェクトを user が編集できるか確認する。"""
    if user is None:
        return False
    async with AsyncSessionLocal() as db:
        owner_result = await db.execute(select(User).where(User.username == owner))
        owner_obj = owner_result.scalar_one_or_none()
        if not owner_obj:
            return False
        img_result = await db.execute(
            select(Image).where(
                Image.owner_id == owner_obj.id,
                Image.slug == slug,
                Image.is_active == True,
            )
        )
        image = img_result.scalar_one_or_none()
        if not image:
            return False
        if user.role == UserRole.admin or image.owner_id == user.id:
            return True
        collab = await db.execute(
            select(ImageCollaborator).where(
                ImageCollaborator.image_id == image.id,
                ImageCollaborator.user_id == user.id,
                ImageCollaborator.role == "read_write",
            )
        )
        return collab.scalar_one_or_none() is not None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = await _get_request_user(request)
    if not user:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("index.html", {"request": request, "settings": settings})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "settings": settings})


@app.get("/setup-username", response_class=HTMLResponse)
async def setup_username_page(request: Request):
    from services.jwt_utils import decode_token
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return RedirectResponse(url="/login", status_code=303)
    try:
        payload = decode_token(token)
    except Exception:
        return RedirectResponse(url="/login", status_code=303)
    # needs_username_setup が false のユーザはアクセス不可
    if not payload.get("needs_username_setup", False):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("setup_username.html", {"request": request, "settings": settings})


@app.get("/explore", response_class=HTMLResponse)
async def explore_page(request: Request):
    user = await _get_request_user(request)
    if not user:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("explore.html", {"request": request, "settings": settings})


@app.get("/new", response_class=HTMLResponse)
async def new_project_page(request: Request):
    user = await _get_request_user(request)
    if not user or user.needs_username_setup:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("new_project.html", {"request": request, "settings": settings})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = await _get_request_user(request)
    if not user:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("settings.html", {"request": request, "settings": settings})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await _get_request_user(request)
    if not user or user.role != UserRole.admin:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("admin.html", {"request": request, "settings": settings})


@app.get("/user/{username}", response_class=HTMLResponse)
async def user_profile_page(username: str, request: Request):
    user = await _get_request_user(request)
    if not user:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == username, User.is_active == True))
        target = result.scalar_one_or_none()
    if not target:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("user_profile.html", {"request": request, "username": username, "settings": settings})


@app.get("/{owner}/{slug}/edit", response_class=HTMLResponse)
async def edit_project_page(owner: str, slug: str, request: Request):
    user = await _get_request_user(request)
    if not await _can_edit_project(owner, slug, user):
        return templates.TemplateResponse(
            "404.html", {"request": request}, status_code=404
        )
    return templates.TemplateResponse("edit_project.html", {"request": request, "owner": owner, "slug": slug, "settings": settings})


@app.get("/{owner}/{slug}", response_class=HTMLResponse)
async def project_page(owner: str, slug: str, request: Request):
    user = await _get_request_user(request)
    if not await _can_view_project(owner, slug, user):
        return templates.TemplateResponse(
            "404.html", {"request": request}, status_code=404
        )
    return templates.TemplateResponse("project.html", {"request": request, "owner": owner, "slug": slug, "settings": settings})
