"""add document content_hash

Revision ID: c41d8e2f9a07
Revises: 7748769af8cf
Create Date: 2026-10-20 04:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c41d8e2f9a07'
down_revision: Union[str, Sequence[str], None] = '7748769af8cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 檔案內容的指紋，用來擋重複文件（docs/decisions/022）
    # 可以是空的：處理成功時才記下；這次之前處理的舊文件不補，等於不跟它們比對
    op.add_column('documents', sa.Column('content_hash', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_documents_content_hash'), 'documents', ['content_hash'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_documents_content_hash'), table_name='documents')
    op.drop_column('documents', 'content_hash')
