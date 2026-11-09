from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError
from database import get_db_readonly
from models import User, UserRole
from services.jwt_utils import decode_token


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db_readonly),
) -> User:
    """認証はCookie（access_token、httponly）のみで行う。トークンをJSから読める
    場所（sessionStorage等）に置かないことでXSS時のトークン窃取リスクを無くすため、
    Bearerヘッダ方式はサポートしない。"""
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    # FastAPIのDepends(...)はリクエストハンドラの処理が完全に終わるまでこのセッション
    # （＝プールからのコネクション）を保持し続ける。認証チェック自体はここで完了している
    # ため、トランザクションを即座に終了してコネクションを早期に解放する。
    # これを怠ると、VM起動（start_vm、数十秒かかる）のようにハンドラが長時間実行される
    # エンドポイントで、認証用セッションが「idle in transaction」のまま居座り続け、
    # 複数のVM起動が重なるとDB接続プールを食い尽くして全リクエストがハングする事故が
    # 起きた（実際に発生: pg_stat_activityのidle in transactionが蓄積）。
    # expire_on_commit=False のため、commit後もuserの属性には問題なくアクセスできる。
    await db.commit()
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


async def require_username_set(user: User = Depends(get_current_user)) -> User:
    """ユーザ名設定が完了していないユーザを弾く依存関数。"""
    if user.needs_username_setup:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ユーザ名を設定してください",
        )
    return user
