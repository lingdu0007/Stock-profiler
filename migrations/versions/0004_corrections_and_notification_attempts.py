"""Add correction lineage and append-only notification attempts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_corrections_and_notification_attempts"
down_revision = "0003_decision_stage_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_events_replacement",
        sa.Column("decision_event_id", sa.String(length=96), nullable=False),
        sa.Column("business_object_id", sa.String(length=96), nullable=False),
        sa.Column("framework_run_id", sa.String(length=96), nullable=False),
        sa.Column("corrects_event_id", sa.String(length=96), nullable=True),
        sa.Column("event_payload", sa.String(), nullable=False),
        sa.Column("committed_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("decision_event_id"),
    )
    op.execute(
        """
        INSERT INTO decision_events_replacement (
            decision_event_id,
            business_object_id,
            framework_run_id,
            corrects_event_id,
            event_payload,
            committed_at
        )
        SELECT
            decision_event_id,
            business_object_id,
            framework_run_id,
            NULL,
            event_payload,
            committed_at
        FROM decision_events
        """
    )
    op.drop_table("decision_events")
    op.rename_table("decision_events_replacement", "decision_events")
    op.create_table(
        "decision_notification_attempts",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("notification_attempt_id", sa.String(length=96), nullable=False),
        sa.Column("report_version_id", sa.String(length=96), nullable=False),
        sa.Column("decision_event_id", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reasons_payload", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("notification_attempt_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    correction_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM decision_events WHERE corrects_event_id IS NOT NULL"
        )
    ).scalar_one()
    if correction_count:
        raise RuntimeError("cannot downgrade while append-only correction facts exist")
    notification_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM decision_notification_attempts")
    ).scalar_one()
    if notification_count:
        raise RuntimeError("cannot downgrade while append-only notification attempts exist")
    op.drop_table("decision_notification_attempts")
    op.create_table(
        "decision_events_replacement",
        sa.Column("decision_event_id", sa.String(length=96), nullable=False),
        sa.Column("business_object_id", sa.String(length=96), nullable=False),
        sa.Column("framework_run_id", sa.String(length=96), nullable=False),
        sa.Column("event_payload", sa.String(), nullable=False),
        sa.Column("committed_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("decision_event_id"),
        sa.UniqueConstraint("framework_run_id"),
    )
    op.execute(
        """
        INSERT INTO decision_events_replacement (
            decision_event_id,
            business_object_id,
            framework_run_id,
            event_payload,
            committed_at
        )
        SELECT
            decision_event_id,
            business_object_id,
            framework_run_id,
            event_payload,
            committed_at
        FROM decision_events
        """
    )
    op.drop_table("decision_events")
    op.rename_table("decision_events_replacement", "decision_events")
