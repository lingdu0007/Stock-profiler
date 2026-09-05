"""Persist the frozen case snapshot alongside its stable business-to-Run mapping."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_persist_frozen_case_snapshots"
down_revision = "0004_corrections_and_notification_attempts"
branch_labels = None
depends_on = None


def _case_payload_column() -> dict[str, object] | None:
    return next(
        (
            column
            for column in sa.inspect(op.get_bind()).get_columns("decision_case_business_objects")
            if column["name"] == "case_payload"
        ),
        None,
    )


def upgrade() -> None:
    existing_column = _case_payload_column()
    if existing_column is not None:
        if existing_column["nullable"] is True:
            return
        raise RuntimeError("interrupted frozen case snapshot migration has incompatible column")
    op.add_column(
        "decision_case_business_objects",
        sa.Column("case_payload", sa.String(), nullable=True),
    )


def downgrade() -> None:
    if _case_payload_column() is None:
        return
    snapshot_count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM decision_case_business_objects WHERE case_payload IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if snapshot_count:
        raise RuntimeError("cannot downgrade while frozen case snapshots exist")
    op.drop_column("decision_case_business_objects", "case_payload")
