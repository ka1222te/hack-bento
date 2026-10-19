import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import settings
from database import init_db, AsyncSessionLocal, AsyncSessionLocalReadonly
from models import User, UserRole, AuthProvider, Image, Visibility, ImageCollaborator
from services.auth_local import hash_password
from services.watchdog import run_watchdog
from services.network import ensure_vm_network
from services.firecracker_setup import ensure_default_vm_assets, ensure_attack_machine_assets
from services.rootfs_convert import run_rootfs_conversion_worker, reset_stuck_conversions
from services.bundle_build import run_bundle_build_worker
from services.docker_base_assets import run_docker_base_build_worker
from services.smolvm import cleanup_orphaned_vms
from routers import auth, envs, images, admin, users, attack_machine
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
    await ensure_vm_network()
    await cleanup_orphaned_vms()
    await reset_stuck_conversions()
    task = asyncio.create_task(run_watchdog())
    vm_assets_task = asyncio.create_task(ensure_default_vm_assets())
    rootfs_convert_task = asyncio.create_task(run_rootfs_conversion_worker())
    bundle_build_task = asyncio.create_task(run_bundle_build_worker())
    attack_machine_task = asyncio.create_task(ensure_attack_machine_assets())
    docker_base_build_task = asyncio.create_task(run_docker_base_build_worker())
    yield
    task.cancel()
    vm_assets_task.cancel()
    rootfs_convert_task.cancel()
    bundle_build_task.cancel()
    attack_machine_task.cancel()
    docker_base_build_task.cancel()
    for t in (task, vm_assets_task, rootfs_convert_task, bundle_build_task, attack_machine_task, docker_base_build_task):
        try:
            await t
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
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Attack Machine コンソール(iframe)でのクリップボード共有を許可する
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), clipboard-read=(self), clipboard-write=(self)"
        )
        # Guacamole は同一オリジンのiframeに埋め込むため、配下のパスにはDENYを付与しない
        guac_prefix = settings.GUACAMOLE_PUBLIC_PATH.rstrip("/") + "/"
        if not request.url.path.startswith(guac_prefix):
            response.headers["X-Frame-Options"] = "DENY"
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

try:
    _STYLE_CSS_VERSION = str(int(os.path.getmtime("static/css/style.css")))
except OSError:
    _STYLE_CSS_VERSION = "1"
templates.env.globals["style_css_version"] = _STYLE_CSS_VERSION


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)

app.include_router(auth.router)
app.include_router(envs.router)
app.include_router(images.router)
app.include_router(admin.router)
app.include_router(users.router)
app.include_router(attack_machine.router)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ---- Guacamole リバースプロキシ ----
# Attack Machine の GUI (VNC) 表示用に、ブラウザから見える GUACAMOLE_PUBLIC_PATH (既定: /guacamole)
# 配下へのリクエストを GUACAMOLE_INTERNAL_URL (Guacamole/Tomcat) へ転送する。
# 認証はGuacamole側のJSON認証拡張が発行する authToken（クエリ文字列）で行われるため、
# このプロキシ自体はHackBentoのユーザ認証チェックを行わない。

_GUAC_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


@app.websocket(f"{settings.GUACAMOLE_PUBLIC_PATH.rstrip('/')}/websocket-tunnel")
async def guacamole_websocket_proxy(websocket: WebSocket):
    target_base = settings.GUACAMOLE_INTERNAL_URL.rstrip("/").replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    query = websocket.url.query
    target_url = f"{target_base}/websocket-tunnel"
    if query:
        target_url += f"?{query}"

    await websocket.accept(subprotocol="guacamole")

    try:
        async with websockets.connect(target_url, subprotocols=["guacamole"]) as upstream:
            async def client_to_upstream():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await upstream.send(data)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass

            async def upstream_to_client():
                try:
                    async for data in upstream:
                        await websocket.send_text(data)
                except websockets.exceptions.ConnectionClosed:
                    pass

            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception:
        logging.getLogger(__name__).exception("Guacamole WebSocketプロキシでエラーが発生しました")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.api_route(
    f"{settings.GUACAMOLE_PUBLIC_PATH.rstrip('/')}/{{path:path}}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def guacamole_http_proxy(path: str, request: Request):
    target_base = settings.GUACAMOLE_INTERNAL_URL.rstrip("/")
    target_url = f"{target_base}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _GUAC_HOP_BY_HOP_HEADERS and k.lower() != "host"
    }
    body = await request.body()

    async with httpx.AsyncClient(timeout=30) as client:
        upstream_resp = await client.request(
            request.method,
            target_url,
            headers=headers,
            content=body,
        )

    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in _GUAC_HOP_BY_HOP_HEADERS
    }
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


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
        async with AsyncSessionLocalReadonly() as db:
            result = await db.execute(
                select(User).where(User.id == int(payload["sub"]), User.is_active == True)
            )
            return result.scalar_one_or_none()
    except Exception:
        return None


async def _can_view_project(owner: str, slug: str, user: Optional[User]) -> bool:
    """owner/slug のプロジェクトを user が閲覧できるか確認する。"""
    async with AsyncSessionLocalReadonly() as db:
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
    async with AsyncSessionLocalReadonly() as db:
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
        return RedirectResponse(url="/login", status_code=303)
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


@app.get("/attack-machine", response_class=HTMLResponse)
async def attack_machine_console_page(request: Request):
    user = await _get_request_user(request)
    if not user or not settings.ATTACK_MACHINE_ENABLED:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("attack_console.html", {"request": request, "settings": settings})


@app.get("/explore", response_class=HTMLResponse)
async def explore_page(request: Request):
    user = await _get_request_user(request)
    if not user:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("explore.html", {"request": request, "settings": settings})


@app.get("/new", response_class=HTMLResponse)
async def new_project_page(request: Request):
    user = await _get_request_user(request)
    if not user or user.needs_username_setup:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("new_project.html", {"request": request, "settings": settings})


@app.get("/docs/rootfs/compose", response_class=HTMLResponse)
async def rootfs_help_compose_page(request: Request):
    """Compose バンドルの作り方の詳細ページ。"""
    user = await _get_request_user(request)
    if not user or user.needs_username_setup:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("rootfs_help_compose.html", {"request": request, "settings": settings})


@app.get("/docs/rootfs/os", response_class=HTMLResponse)
async def rootfs_help_os_page(request: Request):
    """OS イメージの作り方の詳細ページ。"""
    user = await _get_request_user(request)
    if not user or user.needs_username_setup:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("rootfs_help_os.html", {"request": request, "settings": settings})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = await _get_request_user(request)
    if not user:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("settings.html", {"request": request, "settings": settings})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await _get_request_user(request)
    if not user or user.role != UserRole.admin:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("admin.html", {"request": request, "settings": settings})


@app.get("/user/{username}", response_class=HTMLResponse)
async def user_profile_page(username: str, request: Request):
    user = await _get_request_user(request)
    if not user:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    async with AsyncSessionLocalReadonly() as db:
        result = await db.execute(select(User).where(User.username == username, User.is_active == True))
        target = result.scalar_one_or_none()
    if not target:
        return templates.TemplateResponse("404.html", {"request": request, "settings": settings}, status_code=404)
    return templates.TemplateResponse("user_profile.html", {"request": request, "username": username, "settings": settings})


@app.get("/{owner}/{slug}/edit", response_class=HTMLResponse)
async def edit_project_page(owner: str, slug: str, request: Request):
    user = await _get_request_user(request)
    if not await _can_edit_project(owner, slug, user):
        return templates.TemplateResponse(
            "404.html", {"request": request, "settings": settings}, status_code=404
        )
    return templates.TemplateResponse("edit_project.html", {"request": request, "owner": owner, "slug": slug, "settings": settings})


@app.get("/{owner}/{slug}", response_class=HTMLResponse)
async def project_page(owner: str, slug: str, request: Request):
    user = await _get_request_user(request)
    if not await _can_view_project(owner, slug, user):
        return templates.TemplateResponse(
            "404.html", {"request": request, "settings": settings}, status_code=404
        )
    return templates.TemplateResponse("project.html", {"request": request, "owner": owner, "slug": slug, "settings": settings})
