"""Preserve append-only phase results for frozen decision-case recovery."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_decision_stage_events"
down_revision = "0002_decision_case_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_stage_events",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stage_event_id", sa.String(length=96), nullable=False),
        sa.Column("business_object_id", sa.String(length=96), nullable=False),
        sa.Column("framework_run_id", sa.String(length=96), nullable=False),
        sa.Column("decision_event_id", sa.String(length=96), nullable=True),
        sa.Column("stage_payload", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("stage_event_id"),
    )


def downgrade() -> None:
    stage_count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM decision_stage_events")
    ).scalar_one()
    if stage_count:
        raise RuntimeError("cannot downgrade while append-only stage results exist")
    op.drop_table("decision_stage_events")
