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
_STAGE_EVENT_STRING_COLUMN_LENGTHS = {
    "stage_event_id": 96,
    "business_object_id": 96,
    "framework_run_id": 96,
    "decision_event_id": 96,
    "stage_payload": None,
    "recorded_at": 40,
}


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _validate_existing_stage_event_table() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        str(column["name"]): column for column in inspector.get_columns("decision_stage_events")
    }
    missing_columns = _REQUIRED_STAGE_EVENT_COLUMNS - columns.keys()
    incompatible_columns = [
        column_name
        for column_name in _REQUIRED_STAGE_EVENT_COLUMNS - missing_columns
        if not _has_expected_stage_event_column_definition(
            columns[column_name],
            column_name,
        )
    ]
    has_sequence_primary_key = inspector.get_pk_constraint("decision_stage_events").get(
        "constrained_columns"
    ) == ["sequence"]
    sequence_column = columns.get("sequence")
    has_generated_sequence = sequence_column is not None and _has_generated_sequence(
        sequence_column,
        has_sequence_primary_key,
    )
    has_unique_stage_event_id = _has_unique_stage_event_id(inspector)
    has_no_extra_columns = columns.keys() == _REQUIRED_STAGE_EVENT_COLUMNS
    has_no_extra_constraints = not inspector.get_foreign_keys(
        "decision_stage_events"
    ) and not inspector.get_check_constraints("decision_stage_events")
    if (
        missing_columns
        or incompatible_columns
        or not has_no_extra_columns
        or not has_sequence_primary_key
        or not has_generated_sequence
        or not has_unique_stage_event_id
        or not has_no_extra_constraints
    ):
        missing_schema = [
            *(f"column {column}" for column in sorted(missing_columns)),
            *(f"compatible column {column}" for column in sorted(incompatible_columns)),
            *(() if has_sequence_primary_key else ("primary key sequence",)),
            *(() if has_generated_sequence else ("generated sequence",)),
            *(() if has_unique_stage_event_id else ("unique stage_event_id",)),
        ]
        raise RuntimeError(
            "interrupted decision stage event migration has incompatible table: "
            f"missing {', '.join(missing_schema)}"
        )


def _has_expected_stage_event_column_definition(
    column: dict[str, object],
    column_name: str,
) -> bool:
    """Fail closed unless an interrupted column has the canonical storage semantics."""
    if column.get("nullable") is not (column_name == "decision_event_id"):
        return False
    if column.get("default") is not None or column.get("computed") is not None:
        return False
    column_type = column.get("type")
    if column_name == "sequence":
        return isinstance(column_type, sa.Integer)
    expected_length = _STAGE_EVENT_STRING_COLUMN_LENGTHS[column_name]
    return isinstance(column_type, sa.String) and column_type.length == expected_length


def _has_generated_sequence(column: dict[str, object], has_primary_key: bool) -> bool:
    """Recognize the SQLite INTEGER PRIMARY KEY rowid allocation used by this migration."""
    if not has_primary_key or not isinstance(column.get("type"), sa.Integer):
        return False
    if op.get_bind().dialect.name == "sqlite":
        return True
    return column.get("autoincrement") is True


def _has_unique_stage_event_id(inspector: sa.Inspector) -> bool:
    """Accept only the canonical stage-event uniqueness and no secondary indexes."""
    found_stage_event_id = False
    for constraint in inspector.get_unique_constraints("decision_stage_events"):
        if constraint.get("column_names") != ["stage_event_id"]:
            return False
        found_stage_event_id = True
    for index in inspector.get_indexes("decision_stage_events"):
        if not index.get("unique") or index.get("column_names") != ["stage_event_id"]:
            return False
        found_stage_event_id = True
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return found_stage_event_id
    for index in bind.execute(sa.text("PRAGMA index_list('decision_stage_events')")).mappings():
        if not index["unique"]:
            return False
        index_name = str(index["name"]).replace("'", "''")
        index_columns = [
            row["name"]
            for row in bind.execute(
                sa.text(f"PRAGMA index_info('{index_name}')")  # noqa: S608
            ).mappings()
        ]
        if index_columns != ["stage_event_id"]:
            return False
        found_stage_event_id = True
    return found_stage_event_id


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
