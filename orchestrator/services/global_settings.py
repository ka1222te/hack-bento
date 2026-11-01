"""管理者が /admin から動的に変更するグローバル設定（models.GlobalSetting）へのアクセス。

GlobalSetting は id=1 固定の単一行テーブル。database.py の init_db が起動時に
.env の値を初期値として一度だけ投入するため、この行は常に存在する前提で読める。
"""
from sqlalchemy.ext.asyncio import AsyncSession

from models import GlobalSetting


async def get_global_settings(db: AsyncSession) -> GlobalSetting:
    row = await db.get(GlobalSetting, 1)
    if row is None:
        # init_db が投入しているはずだが、念のためのフォールバック。
        row = GlobalSetting(id=1)
        db.add(row)
        await db.flush()
    return row
