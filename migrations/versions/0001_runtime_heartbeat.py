"""Create application-owned runtime heartbeats."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_runtime_heartbeat"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_heartbeats",
        sa.Column("process_name", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("process_name"),
    )


def downgrade() -> None:
    op.drop_table("runtime_heartbeats")
