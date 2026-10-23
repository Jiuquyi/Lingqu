"""Idempotent metering: request ids on ledger rows, pending charges and meter events.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ledger') as batch:
        batch.add_column(sa.Column('request_id', sa.String(32), nullable=True))
        batch.add_column(sa.Column('eusd_pending', sa.Float(), nullable=True))
        batch.create_index('ix_ledger_request_id', ['request_id'], unique=True)
    op.create_table('meter_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('request_id', sa.String(32), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('ts', sa.Float(), nullable=False),
        sa.Column('pool', sa.String(128), nullable=False),
        sa.Column('model', sa.String(128)),
        sa.Column('user_ref', sa.String(128)),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.UniqueConstraint('request_id', 'seq', name='uq_meter_events_request_seq'))
    op.create_index('ix_meter_events_ts', 'meter_events', ['ts'])


def downgrade():
    op.drop_table('meter_events')
    with op.batch_alter_table('ledger') as batch:
        batch.drop_index('ix_ledger_request_id')
        batch.drop_column('eusd_pending')
        batch.drop_column('request_id')
