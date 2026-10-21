"""ledger.cache_state: warm / cold / none as the session-aware gate saw each request (M3)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21 21:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cache_state', sa.String(length=8), nullable=True))


def downgrade():
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.drop_column('cache_state')
