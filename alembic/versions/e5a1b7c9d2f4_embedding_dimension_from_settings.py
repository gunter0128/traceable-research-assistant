"""embedding dimension from settings

Revision ID: e5a1b7c9d2f4
Revises: c41d8e2f9a07
Create Date: 2026-10-25 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.config import settings


# revision identifiers, used by Alembic.
revision: str = 'e5a1b7c9d2f4'
down_revision: Union[str, Sequence[str], None] = 'c41d8e2f9a07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 向量欄位的維度改成跟設定一樣（docs/decisions/037）
    # 公開展示版用 OpenAI（1536），跟原本一樣，什麼都不做
    # 企業內部版用 Qwen3-Embedding（1024），是全新的資料庫，chunks 表還是空的，直接改欄位
    conn = op.get_bind()
    # pgvector 把維度記在欄位型別的 atttypmod 裡
    current = conn.execute(sa.text(
        "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    )).scalar()
    target = settings.embedding_dimension
    if current == target:
        return
    # 已經有向量的話不能直接改：舊的向量是別的模型轉的，維度不一樣，要全部重新轉
    if conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM chunks)")).scalar():
        raise RuntimeError(
            f"chunks 表已經有 {current} 維的向量，不能直接改成 {target} 維：換轉向量模型要先清掉 chunks、文件重新處理"
        )
    op.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({target})")


def downgrade() -> None:
    """Downgrade schema."""
    # 不改回去：維度跟著設定走，退回上一版 migration 不代表要換轉向量模型
    pass
