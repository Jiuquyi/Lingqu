"""Dynamic budgets, upstream quota observations and cycle spend recovery (M4)."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ledger") as batch:
        batch.add_column(sa.Column("accounted_at", sa.Float()))
    op.create_table("budget_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pool", sa.String(128), nullable=False),
        sa.Column("scope_key", sa.String(160), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("trigger", sa.String(32)),
        sa.Column("remaining_r", sa.Float()),
        sa.Column("weight_now", sa.Float()),
        sa.Column("weight_rem", sa.Float()),
        sa.Column("s_rem", sa.Float()),
        sa.Column("h_value", sa.Float()),
        sa.Column("detail", sa.JSON()))
    op.create_index("ix_budget_snapshots_pool", "budget_snapshots", ["pool"])
    op.create_index("ix_budget_snapshots_ts", "budget_snapshots", ["ts"])
    op.create_table("upstream_quota",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pool", sa.String(128), nullable=False),
        sa.Column("connection", sa.String(160), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("window", sa.String(32), nullable=False),
        sa.Column("used_pct", sa.Float(), nullable=False),
        sa.Column("reset_at", sa.Float()),
        sa.Column("source", sa.String(16)))
    op.create_index("ix_upstream_quota_pool", "upstream_quota", ["pool"])
    op.create_index("ix_upstream_quota_ts", "upstream_quota", ["ts"])
    op.create_table("cycle_spend_snapshots",
        sa.Column("pool", sa.String(128), primary_key=True),
        sa.Column("model", sa.String(256), primary_key=True),
        sa.Column("saved_at", sa.Float(), nullable=False),
        sa.Column("charges", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("cycle_spend_snapshots")
    op.drop_table("upstream_quota")
    op.drop_table("budget_snapshots")
    with op.batch_alter_table("ledger") as batch:
        batch.drop_column("accounted_at")
