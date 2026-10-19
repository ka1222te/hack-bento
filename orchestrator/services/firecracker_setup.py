"""VM （デフォルトOS・docker-base・Attack Machine）の起動時オーケストレーション。

各の init/ビルドスクリプト・構築処理は責務ごとに分離済み:
  - services/vm_assets.py            パス・ラベル等の定数
  - services/rootfs_tar.py           tar⇄ext4 変換・共通実行ヘルパー
  - services/default_assets.py       デフォルトカーネル/rootfs・rootfs変換専用ベース
  - services/docker_base_assets.py   docker-base（Compose バンドル用土台）
  - services/attack_machine_assets.py Attack Machine（Kali）

このモジュールは main.py の lifespan から呼ばれる、起動時の準備処理の
入り口（ensure_default_vm_assets/ensure_attack_machine_assets）だけを残す。
"""
import logging
import os

from services import default_assets, docker_base_assets
from services.vm_assets import DEFAULTS_DIR

logger = logging.getLogger(__name__)


async def ensure_default_vm_assets() -> None:
    """デフォルトゲストカーネル・rootfs を準備する（無ければ生成）。

    プロジェクト登録時に「デフォルトのカーネル/rootfsを使う」を選択した場合に
    参照される共通ファイル。失敗してもアプリ起動はブロックしない。
    """
    if not os.path.exists("/dev/kvm"):
        logger.warning("/dev/kvm が見つからないため、デフォルトVMアセットの準備をスキップします。")
        return

    os.makedirs(DEFAULTS_DIR, exist_ok=True)

    # rootfs 変換専用ベース（アップロードされた rootfs tar をビルド用VM内で安全に
    # 展開するための土台）を最初に準備する。convert_tar_to_ext4（デフォルト rootfs・
    # docker-base・Attack Machine の構築、および rootfs_convert からのユーザアップロード
    # 変換すべてが内部で使う）がこのベースに依存するため、他のどの構築よりも先に
    # 用意できていないと以降の変換が全て失敗する。
    try:
        await default_assets.build_rootfs_converter_base()
    except Exception as e:
        logger.error(f"rootfs 変換専用ベースの準備に失敗しました（rootfs の tar→ext4 変換が全て失敗します）: {e}")

    try:
        await default_assets.download_default_kernel()
        await default_assets.build_default_rootfs()
        await default_assets.register_builtin_asset_records()
    except Exception as e:
        logger.error(f"デフォルトVMアセットの準備に失敗しました（プロジェクト作成時に手動アップロードで代替できます）: {e}")

    # docker-base（Compose バンドル用土台）は独立して準備する。
    # 失敗しても通常の OS イメージ運用には影響しないため、別 try で握りつぶす。
    try:
        await docker_base_assets.build_docker_base_rootfs()
        await docker_base_assets.register_docker_base_asset()
    except Exception as e:
        logger.error(f"docker-base rootfs の準備に失敗しました（Compose バンドル機能のみ無効・手動登録で代替可）: {e}")


async def ensure_attack_machine_assets() -> None:
    """Attack Machine 用 rootfs を準備し、Image テンプレートを DB に登録する。"""
    from services.attack_machine_assets import ensure_attack_machine_assets as _ensure
    await _ensure()
