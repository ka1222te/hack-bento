import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

from database import get_db, get_db_readonly
from models import Environment, EnvStatus, Image, User, UserRole, RootfsConversionStatus
from deps import get_current_user, require_username_set
from services.smolvm import start_vm, stop_vm, get_copy_progress, is_copy_waiting
from services.network import allocate_ip, release_ip, _lock as _ip_lock
from services.watchdog import destroy_env, claim_env_for_stop
from services.global_settings import get_global_settings
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/envs", tags=["environments"])


# VM起動失敗の原因分類: smolvm.py/network.py が投げる例外メッセージ（キーワード）
# → 一般ユーザー向けの分かりやすい文言。文字列全文は一致させずキーワード部分一致で
# 判定する（内部のメッセージ文言が多少変わっても分類が壊れないようにするため）。
_START_VM_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("/dev/kvm", "ホストでKVMが有効になっていません。管理者に連絡してください。"),
    ("ディスク空き容量が不足", "ホストのディスク容量が不足しています。しばらく待ってから再試行するか、管理者に連絡してください。"),
    ("ゲストカーネルが見つかりません", "プロジェクトのゲストカーネルファイルが見つかりません。プロジェクト編集画面でカーネルを再登録してください。"),
    ("rootfs が見つかりません", "プロジェクトのrootfsファイルが見つかりません。プロジェクト編集画面でrootfsを再登録してください。"),
    ("がタイムアウトしました", "VMの起動がタイムアウトしました。しばらく待ってから再試行してください。"),
    ("以内に応答しませんでした", "VMの起動がタイムアウトしました。しばらく待ってから再試行してください。"),
    ("ブリッジ", "ホストのネットワーク設定に問題があります。管理者に連絡してください。"),
    ("IP_POOL", "ホストのネットワーク設定に問題があります。管理者に連絡してください。"),
]


def _classify_start_vm_error(exc: Exception) -> str:
    """start_vm() が投げた例外を一般ユーザー向けの分かりやすい文言に分類する。
    どのパターンにも当たらない場合は原因不明として扱う。実際の例外内容は
    呼び出し元が必ずログに出す（ここでは出さない）。"""
    msg = str(exc)
    for keyword, user_message in _START_VM_ERROR_PATTERNS:
        if keyword in msg:
            return user_message
    return "VM起動に失敗しました（原因不明）。しばらく待ってから再試行するか、管理者に連絡してください。"


class EnvResponse(BaseModel):
    id: int
    image_id: int
    image_name: str
    image_slug: str
    owner_username: Optional[str]
    ip_address: Optional[str]
    status: str
    started_at: datetime
    expires_at: datetime
    seconds_remaining: int
    timeout_minutes: int
    is_unlimited: bool
    warning: bool
    is_attack_machine: bool
    last_error: Optional[str] = None
    # rootfsのwritableコピー中のみ設定される（コピー中でない場合はNone）。
    copy_progress_percent: Optional[int] = None
    # VM_ROOTFS_COPY_CONCURRENCYの空きが無く、他のVMのコピー完了を順番待ちしている間はTrue。
    # UIが「VM起動中」（コピー実行中・Firecracker起動処理中）と「VM起動待機中」
    # （順番待ち）を区別して表示するために使う。
    is_copy_waiting: bool = False

    model_config = {"from_attributes": True}


def _to_response(env: Environment) -> EnvResponse:
    now = datetime.utcnow()
    remaining = max(0, int((env.expires_at - now).total_seconds()))
    img_timeout = env.image.timeout_minutes if env.image else None
    timeout = img_timeout if img_timeout is not None else settings.DEFAULT_TIMEOUT_MINUTES
    owner = env.image.owner.username if env.image and env.image.owner else None
    progress = get_copy_progress(env.id)
    copy_progress_percent = int(progress[0] * 100 / progress[1]) if progress and progress[1] > 0 else None
    return EnvResponse(
        id=env.id,
        image_id=env.image_id,
        image_name=env.image.name,
        image_slug=env.image.slug,
        owner_username=owner,
        ip_address=env.ip_address,
        status=env.status.value,
        started_at=env.started_at,
        expires_at=env.expires_at,
        seconds_remaining=remaining,
        timeout_minutes=timeout,
        is_unlimited=env.is_unlimited,
        warning=not env.is_unlimited and remaining < settings.TIMEOUT_WARNING_MINUTES * 60,
        is_attack_machine=env.image.is_attack_machine if env.image else False,
        last_error=env.last_error,
        copy_progress_percent=copy_progress_percent,
        is_copy_waiting=is_copy_waiting(env.id),
    )


@router.get("/", response_model=list[EnvResponse])
async def list_my_envs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_readonly),
):
    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.image).selectinload(Image.owner))
        .where(
            Environment.user_id == user.id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]),
        )
        .order_by(Environment.started_at.desc())
    )
    envs = result.scalars().all()
    return [_to_response(e) for e in envs]


@router.get("/by-id/{env_id}", response_model=EnvResponse)
async def get_env(
    env_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_readonly),
):
    """starting/running/stopping に限らず、stopped になった直後の環境も取得できる。

    list_my_envs（一覧）は starting/running/stopping のみを返すため、watchdog の
    タイムアウト処理で stopped になった瞬間、ポーリング中のUIからは「対象が消えた」
    ようにしか見えず、last_error（タイムアウトで打ち切られた等の理由）を伝えられない。
    UIは一覧から対象が消えたタイミングでこのエンドポイントを呼び、last_error の
    有無で「起動に失敗した」のか「タイムアウトで打ち切られた」のかを案内する。
    """
    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.image).selectinload(Image.owner))
        .where(Environment.id == env_id, Environment.user_id == user.id)
    )
    env = result.scalar_one_or_none()
    if env is None:
        raise HTTPException(status_code=404, detail="環境が見つかりません")
    return _to_response(env)


@router.post("/{image_id}/start", response_model=EnvResponse, status_code=status.HTTP_201_CREATED)
async def start_env(
    image_id: int,
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    img_result = await db.execute(
        select(Image).where(Image.id == image_id, Image.is_active == True)
    )
    image = img_result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="イメージが見つかりません")

    from routers.images import _can_see
    if not await _can_see(image, user, db):
        raise HTTPException(status_code=404, detail="イメージが見つかりません")

    if image.rootfs_conversion_status in (RootfsConversionStatus.pending.value, RootfsConversionStatus.converting.value):
        raise HTTPException(
            status_code=409,
            detail="rootfs を ext4 形式へ変換中です。完了までしばらくお待ちください",
        )
    if image.rootfs_conversion_status == RootfsConversionStatus.failed.value:
        raise HTTPException(
            status_code=409,
            detail="rootfs の変換に失敗しています。プロジェクト編集画面から rootfs を再アップロードしてください",
        )

    timeout = image.timeout_minutes if image.timeout_minutes is not None else settings.DEFAULT_TIMEOUT_MINUTES
    is_unlimited = timeout == 0
    # is_unlimited=True の環境では expires_at は watchdog の判定対象外になるため
    # 実質無意味な値だが、カラムが NOT NULL のため placeholder を入れておく。
    placeholder_minutes = timeout if not is_unlimited else settings.DEFAULT_TIMEOUT_MINUTES
    env: Environment | None = None

    async def _reserve(ip: str) -> None:
        """lock 保持中に上限チェックと Environment の仮記録を行う。"""
        nonlocal env
        # admin は上限チェックを免除
        if user.role != UserRole.admin:
            user_count = await db.execute(
                select(func.count()).where(
                    Environment.user_id == user.id,
                    Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping]),
                )
            )
            if user_count.scalar() >= settings.MAX_ENVS_PER_USER:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"同時起動数の上限（{settings.MAX_ENVS_PER_USER}つ）に達しています",
                )
            total_count = await db.execute(
                select(func.count()).where(
                    Environment.status.in_([EnvStatus.starting, EnvStatus.running, EnvStatus.stopping])
                )
            )
            if total_count.scalar() >= settings.MAX_ENVS_TOTAL:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="サーバが混雑しています。しばらくお待ちください",
                )
        now = datetime.utcnow()
        env = Environment(
            user_id=user.id,
            image_id=image.id,
            ip_address=ip,
            status=EnvStatus.starting,
            started_at=now,
            expires_at=now + timedelta(minutes=placeholder_minutes),
            is_unlimited=is_unlimited,
        )
        db.add(env)
        await db.flush()

    ip = await allocate_ip(db, _reserve)
    if not ip or env is None:
        raise HTTPException(status_code=503, detail="利用可能なIPアドレスがありません")

    try:
        pids_limit = image.pids_limit or (await get_global_settings(db)).vm_pids_max
        # watchdog の起動タイムアウト判定はこの時刻（実際にstart_vm()の処理が
        # 始まる直前）を基準にする。started_at（IP確保・同時起動数チェック等の
        # キュー待ち時間を含む）を基準にすると、複数VM同時起動時に後発のVMが
        # _lock待ちやディスクI/O輻輳での待ち時間を含めて不公平にタイムアウトして
        # しまうため（実際に発生）。
        env.vm_processing_started_at = datetime.utcnow()
        # get_global_settings の SELECT でトランザクションが開いたまま、この先の
        # start_vm()（Firecracker起動、数十秒かかる）を実行すると、その間ずっと
        # このDBセッション（＝プールからのコネクション）が「idle in transaction」
        # のまま居座り続ける。複数のVM起動が重なるとDB接続プールを食い尽くして
        # 全リクエストがハングする事故が起きたため、ここで明示的にコミットして
        # トランザクションを終了し、コネクションを早期に解放する。
        await db.commit()
        vm_result = await start_vm(
            image=image,
            ip=ip,
            cpu=image.cpu_limit,
            memory_mb=image.memory_limit_mb,
            pids_limit=pids_limit,
            env_id=env.id,
        )
    except Exception as e:
        logger.error(f"start_vm failed env_id={env.id}: {e}", exc_info=True)
        await destroy_env(env)
        await db.commit()
        user_message = _classify_start_vm_error(e)
        if user.role == UserRole.admin:
            user_message = f"{user_message}\n詳細: {e}"
        raise HTTPException(status_code=500, detail=user_message)

    try:
        # vm_id は取得後すぐに確定させる（status=starting の行自体は allocate_ip 内で
        # 既に commit 済み）。ここで vm_id を確定・commit しないまま後続処理中に
        # オーケストレータが落ちると、VM/コンテナは実在するのに DB 上は vm_id が NULL の
        # まま残り、cleanup_orphaned_vms が vm_id で突き合わせできず孤児を検出できなくなる。
        # また、vm_id が確定する前に停止要求（watchdogのstuck-starting処理含む）が
        # 先行した場合、destroy_env が vm_id=None で実VM/TAP/cgroupを解放できず
        # 孤児が残ってしまうため、vm_id だけを先に確定・commitする。
        env.vm_id = vm_result.vm_id
        await db.commit()

        # ここまでの間に他経路（停止要求・watchdog）が先に env を stopping/stopped へ
        # 遷移させていた場合は、running への遷移を行わず、いま確定した vm_id を使って
        # 実VMを後始末する（孤児防止）。
        result = await db.execute(
            select(Environment).where(Environment.id == env.id).with_for_update()
        )
        env = result.scalar_one()
        if env.status != EnvStatus.starting:
            await db.commit()
            await stop_vm(vm_result.vm_id)
            raise HTTPException(status_code=409, detail="起動中にこの環境は停止されました")

        env.status = EnvStatus.running
        # start_vm() の起動処理（数十秒かかる場合がある）の間はタイマーを消費させず、
        # running に遷移した時点から timeout 分のカウントを開始する
        # （is_unlimited=True の場合 expires_at は watchdog の判定対象外になる placeholder）。
        env.expires_at = datetime.utcnow() + timedelta(minutes=placeholder_minutes)
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        # start_vm 自体は成功している（実VM/TAP/cgroupが実在する）ため、ここで例外が
        # 出た場合（DBロック等）に何もせず上に投げると、HTTPレスポンスは失敗を返しつつ
        # 実VMだけが起動済みのまま孤立する（ユーザーが再試行すると二重起動・IP枯渇の
        # 原因になる）。vm_id は確定済みなので stop_vm でここで必ず後始末する。
        logger.error(f"start_env commit failed after start_vm succeeded env_id={env.id} vm_id={vm_result.vm_id}: {e}")
        try:
            await stop_vm(vm_result.vm_id)
        except Exception as cleanup_e:
            logger.error(f"start_env cleanup also failed vm_id={vm_result.vm_id}: {cleanup_e}")
        raise HTTPException(status_code=500, detail="環境の起動処理中にエラーが発生しました。もう一度お試しください")

    result = await db.execute(
        select(Environment)
        .options(selectinload(Environment.image).selectinload(Image.owner))
        .where(Environment.id == env.id)
    )
    env = result.scalar_one()
    return _to_response(env)


@router.post("/{env_id}/extend")
async def extend_env(
    env_id: int,
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    from services.network import _ip_pool
    async with _ip_lock:
        # WITH_FOR_UPDATE で行ロックを取得し、同時リクエストによる上限超過を防ぐ
        result = await db.execute(
            select(Environment)
            .options(selectinload(Environment.image))
            .where(
                Environment.id == env_id,
                Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
            )
            .with_for_update()
        )
        env = result.scalar_one_or_none()
        if not env:
            raise HTTPException(status_code=404, detail="環境が見つかりません")
        if env.user_id != user.id and user.role != UserRole.admin:
            raise HTTPException(status_code=403, detail="権限がありません")

        max_extend = len(_ip_pool())
        if user.role != UserRole.admin and env.extended_count >= max_extend:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"延長回数の上限（{max_extend}回）に達しています",
            )

        if env.is_unlimited:
            raise HTTPException(status_code=400, detail="この環境は無制限のため延長の必要はありません")

        # 延長時間 = イメージのタイムアウト設定（上限もこれに揃える）。
        # プロジェクト編集で後から無制限(0)に変更されていた場合は DEFAULT にフォールバックする
        # （この env 自体は起動時点では有限なので is_unlimited=False のまま）。
        img_timeout = env.image.timeout_minutes if env.image else None
        extend_minutes = img_timeout if img_timeout else settings.DEFAULT_TIMEOUT_MINUTES
        env.expires_at = datetime.utcnow() + timedelta(minutes=extend_minutes)
        env.extended_count += 1
        await db.commit()
    return {"message": f"{extend_minutes}分延長しました", "expires_at": env.expires_at.isoformat(), "extend_minutes": extend_minutes}


@router.post("/{env_id}/stop")
async def stop_env(
    env_id: int,
    user: User = Depends(require_username_set),
    db: AsyncSession = Depends(get_db),
):
    # 権限確認は行ロック取得前に行う（不正なリクエストで状態遷移させないため）。
    # starting 中は vm_id が未確定で destroy_env が実VM/TAP/cgroupを解放できず
    # 孤児リソースを生むため、running になってからのみ停止を許可する
    # （starting が長時間続く場合は watchdog の stuck-starting 処理に委ねる）。
    result = await db.execute(
        select(Environment.user_id, Environment.status).where(
            Environment.id == env_id,
            Environment.status.in_([EnvStatus.starting, EnvStatus.running]),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="環境が見つかりません")
    owner_id, env_status = row
    if owner_id != user.id and user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="権限がありません")
    if env_status == EnvStatus.starting:
        raise HTTPException(status_code=409, detail="VM起動中は停止できません。起動完了までお待ちください")

    # 行ロックを取得して stopping へ遷移。watchdog や他リクエストとの二重停止を防ぐ。
    env = await claim_env_for_stop(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="環境が見つかりません（既に停止処理中です）")

    await destroy_env(env)
    await db.commit()
    return {"message": "停止しました"}
