"""add document attempts and queued_at

Revision ID: 151825878a8f
Revises: 7bf34d6848e9
Create Date: 2026-10-14 16:09:43.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '151825878a8f'
down_revision: Union[str, Sequence[str], None] = '7bf34d6848e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default="0"：表裡已經有的舊文件，這個欄位會自動填 0，不會因為 NOT NULL 而加不上去
    op.add_column('documents', sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))
    op.add_column('documents', sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'queued_at')
    op.drop_column('documents', 'attempts')
