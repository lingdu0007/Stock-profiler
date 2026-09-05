"""Persist the frozen case snapshot alongside its stable business-to-Run mapping."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_persist_frozen_case_snapshots"
down_revision = "0004_corrections_and_notification_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "decision_case_business_objects",
        sa.Column("case_payload", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("decision_case_business_objects", "case_payload")
