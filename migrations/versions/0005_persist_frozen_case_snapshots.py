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


def _has_compatible_case_payload_column(column: dict[str, object]) -> bool:
    """Accept only the nullable, unbounded JSON column created by this migration."""
    return (
        column.get("nullable") is True
        and column.get("default") is None
        and column.get("computed") is None
        and isinstance(column.get("type"), sa.String)
        and column["type"].length is None
    )


def upgrade() -> None:
    existing_column = _case_payload_column()
    if existing_column is not None:
        if _has_compatible_case_payload_column(existing_column):
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
