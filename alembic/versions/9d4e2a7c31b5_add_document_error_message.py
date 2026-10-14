"""add document error_message

Revision ID: 9d4e2a7c31b5
Revises: 151825878a8f
Create Date: 2026-10-14 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d4e2a7c31b5'
down_revision: Union[str, Sequence[str], None] = '151825878a8f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 可以是空的：沒失敗的文件不需要原因，舊文件也不用補值
    op.add_column('documents', sa.Column('error_message', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'error_message')
