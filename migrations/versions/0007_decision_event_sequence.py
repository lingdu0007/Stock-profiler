"""Order append-only decision events by their durable commit sequence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_decision_event_sequence"
down_revision = "0006_result_access_audit"
branch_labels = None
depends_on = None

_INDEX_NAME = "ux_decision_events_event_sequence"
_REPLACEMENT_TABLE = "decision_events_without_event_sequence"


def upgrade() -> None:
    connection = op.get_bind()
    columns = {
        str(column["name"]) for column in sa.inspect(connection).get_columns("decision_events")
    }
    if "event_sequence" not in columns:
        op.add_column(
            "decision_events",
            sa.Column("event_sequence", sa.Integer(), nullable=True),
        )
    _backfill_event_sequences(connection)
    index_names = {
        str(index["name"]) for index in sa.inspect(connection).get_indexes("decision_events")
    }
    if _INDEX_NAME not in index_names:
        op.create_index(
            _INDEX_NAME,
            "decision_events",
            ["event_sequence"],
            unique=True,
        )


def downgrade() -> None:
    if _repair_interrupted_downgrade():
        return
    connection = op.get_bind()
    columns = {
        str(column["name"]) for column in sa.inspect(connection).get_columns("decision_events")
    }
    if "event_sequence" not in columns:
        return
    index_names = {
        str(index["name"]) for index in sa.inspect(connection).get_indexes("decision_events")
    }
    if _INDEX_NAME in index_names:
        op.drop_index(_INDEX_NAME, table_name="decision_events")
    op.create_table(
        _REPLACEMENT_TABLE,
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
        INSERT INTO decision_events_without_event_sequence (
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
            corrects_event_id,
            event_payload,
            committed_at
        FROM decision_events
        """
    )
    op.drop_table("decision_events")
    op.rename_table(_REPLACEMENT_TABLE, "decision_events")


def _backfill_event_sequences(connection: sa.Connection) -> None:
    """Use SQLite's durable insertion order to sequence pre-migration facts."""
    next_sequence = int(
        connection.execute(
            sa.text("SELECT COALESCE(MAX(event_sequence), 0) FROM decision_events")
        ).scalar_one()
    )
    event_ids = connection.execute(
        sa.text(
            "SELECT decision_event_id FROM decision_events "
            "WHERE event_sequence IS NULL ORDER BY rowid"
        )
    ).scalars()
    for event_id in event_ids:
        next_sequence += 1
        connection.execute(
            sa.text(
                "UPDATE decision_events SET event_sequence = :event_sequence "
                "WHERE decision_event_id = :decision_event_id"
            ),
            {
                "event_sequence": next_sequence,
                "decision_event_id": event_id,
            },
        )


def _repair_interrupted_downgrade() -> bool:
    """Discard a copied replacement or finish the rename after non-transactional DDL."""
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    has_events = "decision_events" in table_names
    has_replacement = _REPLACEMENT_TABLE in table_names
    if not has_replacement:
        return False
    if has_events:
        op.drop_table(_REPLACEMENT_TABLE)
        return False
    op.rename_table(_REPLACEMENT_TABLE, "decision_events")
    return True
