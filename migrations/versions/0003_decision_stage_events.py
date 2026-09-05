"""Preserve append-only phase results for frozen decision-case recovery."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_decision_stage_events"
down_revision = "0002_decision_case_ledger"
branch_labels = None
depends_on = None

_REQUIRED_STAGE_EVENT_COLUMNS = frozenset(
    {
        "sequence",
        "stage_event_id",
        "business_object_id",
        "framework_run_id",
        "decision_event_id",
        "stage_payload",
        "recorded_at",
    }
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _validate_existing_stage_event_table() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("decision_stage_events")}
    missing_columns = _REQUIRED_STAGE_EVENT_COLUMNS - columns
    has_sequence_primary_key = inspector.get_pk_constraint("decision_stage_events").get(
        "constrained_columns"
    ) == ["sequence"]
    has_unique_stage_event_id = _has_unique_stage_event_id(inspector)
    if missing_columns or not has_sequence_primary_key or not has_unique_stage_event_id:
        missing_schema = [
            *(f"column {column}" for column in sorted(missing_columns)),
            *(() if has_sequence_primary_key else ("primary key sequence",)),
            *(() if has_unique_stage_event_id else ("unique stage_event_id",)),
        ]
        raise RuntimeError(
            "interrupted decision stage event migration has incompatible table: "
            f"missing {', '.join(missing_schema)}"
        )


def _has_unique_stage_event_id(inspector: sa.Inspector) -> bool:
    """Recognize SQLite's automatic UNIQUE index after an interrupted table create."""
    if any(
        constraint.get("column_names") == ["stage_event_id"]
        for constraint in inspector.get_unique_constraints("decision_stage_events")
    ) or any(
        index.get("unique") and index.get("column_names") == ["stage_event_id"]
        for index in inspector.get_indexes("decision_stage_events")
    ):
        return True
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return False
    for index in bind.execute(sa.text("PRAGMA index_list('decision_stage_events')")).mappings():
        if not index["unique"]:
            continue
        index_name = str(index["name"]).replace("'", "''")
        index_columns = [
            row["name"]
            for row in bind.execute(
                sa.text(f"PRAGMA index_info('{index_name}')")  # noqa: S608
            ).mappings()
        ]
        if index_columns == ["stage_event_id"]:
            return True
    return False


def upgrade() -> None:
    if _has_table("decision_stage_events"):
        _validate_existing_stage_event_table()
        return
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
    if not _has_table("decision_stage_events"):
        return
    stage_count = (
        op.get_bind().execute(sa.text("SELECT COUNT(*) FROM decision_stage_events")).scalar_one()
    )
    if stage_count:
        raise RuntimeError("cannot downgrade while append-only stage results exist")
    op.drop_table("decision_stage_events")
