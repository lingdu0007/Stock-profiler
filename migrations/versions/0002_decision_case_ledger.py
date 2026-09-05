"""Create the host-owned decision-case ledger and formal report projection."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_decision_case_ledger"
down_revision = "0001_runtime_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_case_business_objects",
        sa.Column("business_object_id", sa.String(length=96), nullable=False),
        sa.Column("case_id", sa.String(length=96), nullable=False),
        sa.Column("frozen_input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("framework_run_id", sa.String(length=96), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("business_object_id"),
        sa.UniqueConstraint("framework_run_id"),
    )
    op.create_table(
        "decision_events",
        sa.Column("decision_event_id", sa.String(length=96), nullable=False),
        sa.Column("business_object_id", sa.String(length=96), nullable=False),
        sa.Column("framework_run_id", sa.String(length=96), nullable=False),
        sa.Column("event_payload", sa.String(), nullable=False),
        sa.Column("committed_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("decision_event_id"),
        sa.UniqueConstraint("framework_run_id"),
    )
    op.create_table(
        "formal_reports",
        sa.Column("report_version_id", sa.String(length=96), nullable=False),
        sa.Column("decision_event_id", sa.String(length=96), nullable=False),
        sa.Column("generated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("report_version_id"),
        sa.UniqueConstraint("decision_event_id"),
    )
    op.create_table(
        "auth_credentials",
        sa.Column("credential_id", sa.String(length=1024), nullable=False),
        sa.Column("credential_public_key", sa.String(), nullable=False),
        sa.Column("sign_count", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("credential_id"),
    )
    op.create_table(
        "auth_challenges",
        sa.Column("challenge_id", sa.String(length=96), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("challenge", sa.String(length=256), nullable=False),
        sa.Column("grant_id", sa.String(length=96), nullable=True),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("challenge_id"),
    )
    op.create_table(
        "auth_host_grants",
        sa.Column("grant_id", sa.String(length=96), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("grant_id"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_id", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("last_seen_at", sa.String(length=40), nullable=False),
        sa.Column("recent_reauth_at", sa.String(length=40), nullable=False),
        sa.Column("absolute_expires_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("session_hash"),
    )


def downgrade() -> None:
    op.drop_table("auth_sessions")
    op.drop_table("auth_host_grants")
    op.drop_table("auth_challenges")
    op.drop_table("auth_credentials")
    op.drop_table("formal_reports")
    op.drop_table("decision_events")
    op.drop_table("decision_case_business_objects")
