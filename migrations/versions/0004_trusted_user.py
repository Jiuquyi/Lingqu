"""Trusted user identity for per-user limits and restart recovery.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ledger') as batch:
        batch.add_column(sa.Column('user_ref', sa.String(128), nullable=True))
        batch.create_index('ix_ledger_user_ref', ['user_ref'])


def downgrade():
    with op.batch_alter_table('ledger') as batch:
        batch.drop_index('ix_ledger_user_ref')
        batch.drop_column('user_ref')
