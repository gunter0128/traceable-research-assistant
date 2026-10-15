"""add document lease and progress

Revision ID: 86b16ab13264
Revises: 9d4e2a7c31b5
Create Date: 2026-10-15 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '86b16ab13264'
down_revision: Union[str, Sequence[str], None] = '9d4e2a7c31b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 全部可以是空的：只有正在處理的文件才有租約、心跳和進度，舊文件不用補值
    op.add_column('documents', sa.Column('claim_token', sa.String(), nullable=True))
    op.add_column('documents', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('documents', sa.Column('progress_done', sa.Integer(), nullable=True))
    op.add_column('documents', sa.Column('progress_total', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'progress_total')
    op.drop_column('documents', 'progress_done')
    op.drop_column('documents', 'heartbeat_at')
    op.drop_column('documents', 'claim_token')
