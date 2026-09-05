"""Add correction lineage and append-only notification attempts."""

from __future__ import annotations

import json
from hashlib import sha256

import sqlalchemy as sa
from alembic import op
from pydantic import ValidationError

from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    FormalReport,
    StageResult,
)

revision = "0004_corrections_and_notification_attempts"
down_revision = "0003_decision_stage_events"
branch_labels = None
depends_on = None

_REQUIRED_NOTIFICATION_ATTEMPT_COLUMNS = frozenset(
    {
        "sequence",
        "notification_attempt_id",
        "report_version_id",
        "decision_event_id",
        "status",
        "reasons_payload",
        "recorded_at",
    }
)
_NOTIFICATION_ATTEMPT_STRING_COLUMN_LENGTHS = {
    "notification_attempt_id": 96,
    "report_version_id": 96,
    "decision_event_id": 96,
    "status": 16,
    "reasons_payload": None,
    "recorded_at": 40,
}
_REQUIRED_DECISION_EVENT_COLUMNS = frozenset(
    {
        "decision_event_id",
        "business_object_id",
        "framework_run_id",
        "corrects_event_id",
        "event_payload",
        "committed_at",
    }
)
_DECISION_EVENT_STRING_COLUMN_LENGTHS = {
    "decision_event_id": 96,
    "business_object_id": 96,
    "framework_run_id": 96,
    "corrects_event_id": 96,
    "event_payload": None,
    "committed_at": 40,
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _stage_event_id(
    *,
    business_object_id: str,
    framework_run_id: str,
    decision_event_id: str | None,
    stage_result: dict[str, object],
) -> str:
    payload = {
        "business_object_id": business_object_id,
        "framework_run_id": framework_run_id,
        "decision_event_id": decision_event_id,
        "stage_result": stage_result,
    }
    return f"decision-stage-{sha256(_canonical_json(payload).encode()).hexdigest()}"


def _legacy_stage_results(payload: dict[str, object]) -> list[dict[str, object]]:
    result = payload.get("result")
    result_reasons = result.get("key_reasons", []) if isinstance(result, dict) else []
    reasons = [reason for reason in result_reasons if isinstance(reason, str)]
    stage_results: list[dict[str, object]] = [
        {
            "phase": "FRAMEWORK_RUN",
            "status": "SUCCEEDED",
            "gate_results": [{"gate_id": "RUN_TERMINAL", "status": "PASSED"}],
            "reasons": [],
        },
        {
            "phase": "HOST_VALIDATION",
            "status": "SUCCEEDED",
            "gate_results": [{"gate_id": "FROZEN_RESULT_MATCH", "status": "PASSED"}],
            "reasons": reasons,
        },
    ]
    outcome_code = result.get("outcome_code") if isinstance(result, dict) else None
    business_status = {
        "SYNTHETIC_REVIEW_COMPLETE": "SUCCEEDED",
        "SYNTHETIC_INPUT_REJECTED": "REJECTED",
        "SYNTHETIC_RESULT_ABSTAINED": "ABSTAINED",
        "SYNTHETIC_RESULT_FAILED": "FAILED",
    }.get(outcome_code)
    if business_status is not None:
        stage_results.append(
            {
                "phase": "BUSINESS_DECISION",
                "status": business_status,
                "gate_results": [
                    {"gate_id": "OUTPUT_CONTRACT", "status": "PASSED"},
                    {"gate_id": "FROZEN_RESULT_MATCH", "status": "PASSED"},
                ],
                "reasons": reasons,
            }
        )
    stage_results.append(
        {
            "phase": "BUSINESS_COMMIT",
            "status": "SUCCEEDED",
            "gate_results": [{"gate_id": "HOST_RESULT_SAVED", "status": "PASSED"}],
            "reasons": [],
        }
    )
    return stage_results


def _publication_stage_result() -> dict[str, object]:
    return {
        "phase": "PUBLICATION",
        "status": "SUCCEEDED",
        "gate_results": [{"gate_id": "EVENT_COMMITTED", "status": "PASSED"}],
        "reasons": [],
    }


def _payload(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError("legacy decision ledger payload must be valid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("legacy decision ledger payload must be a JSON object")
    return payload


def _validated_stage_results(payload: dict[str, object]) -> list[dict[str, object]]:
    stage_results = payload.get("stage_results")
    candidates = (
        stage_results if isinstance(stage_results, list) else _legacy_stage_results(payload)
    )
    if not candidates or not all(isinstance(stage_result, dict) for stage_result in candidates):
        raise RuntimeError("legacy decision stage contract is invalid")
    try:
        return [
            StageResult.model_validate(stage_result).model_dump(mode="json")
            for stage_result in candidates
        ]
    except ValidationError as error:
        raise RuntimeError("legacy decision stage contract is invalid") from error


def _validated_event_fact(
    payload: dict[str, object],
    *,
    decision_event_id: str,
    business_object_id: str,
    framework_run_id: str,
) -> DecisionEventFact:
    _validated_stage_results(payload)
    try:
        fact = DecisionEventFact.model_validate(payload)
    except ValidationError as error:
        raise RuntimeError("legacy decision event contract is invalid") from error
    if (
        fact.decision_event_id != decision_event_id
        or fact.business_object_id != business_object_id
        or fact.framework_run_id != framework_run_id
        or fact.case.business_object_id != business_object_id
        or fact.case.framework_run_id != framework_run_id
        or fact.corrects_event_id is not None
        or fact.decision_event_id
        != fact.case.decision_event_id_for_framework_run(framework_run_id)
    ):
        raise RuntimeError("legacy decision event identity does not match its row")
    return fact


def _validated_report_stage_results(
    payload: dict[str, object],
    *,
    report_version_id: str,
    decision_event_id: str,
    generated_at: str,
    event: DecisionEventFact,
) -> list[dict[str, object]]:
    stage_results = _validated_stage_results(payload)
    try:
        report = FormalReport.model_validate(payload)
    except ValidationError as error:
        raise RuntimeError("legacy formal report contract is invalid") from error
    if (
        report != event.formal_report(report_version_id)
        or report.event_id != decision_event_id
        or report.generated_at != generated_at
    ):
        raise RuntimeError("legacy formal report identity does not match its row or event")
    return stage_results


def _event_rows_for_preflight(event_table: str) -> sa.MappingResult:
    """Read the table that was authoritative when an interrupted replacement stopped."""
    bind = op.get_bind()
    if event_table == "decision_events":
        return bind.execute(
            sa.text(
                """
                SELECT
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload
                FROM decision_events
                """
            )
        ).mappings()
    if event_table == "decision_events_replacement":
        return bind.execute(
            sa.text(
                """
                SELECT
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload
                FROM decision_events_replacement
                """
            )
        ).mappings()
    raise RuntimeError("interrupted decision event migration has no authoritative event table")


def _preflight_legacy_stage_history(event_table: str) -> None:
    """Validate every legacy payload before SQLite's non-transactional DDL begins."""
    bind = op.get_bind()
    mappings = {
        row["business_object_id"]: row
        for row in bind.execute(
            sa.text(
                """
                SELECT
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id
                FROM decision_case_business_objects
                """
            )
        ).mappings()
    }
    event_facts: dict[str, DecisionEventFact] = {}
    for row in _event_rows_for_preflight(event_table):
        event = _validated_event_fact(
            _payload(row["event_payload"]),
            decision_event_id=row["decision_event_id"],
            business_object_id=row["business_object_id"],
            framework_run_id=row["framework_run_id"],
        )
        mapping = mappings.get(row["business_object_id"])
        if (
            mapping is None
            or mapping["case_id"] != event.case.case_id
            or mapping["frozen_input_fingerprint"] != event.case.frozen_input_fingerprint
            or mapping["framework_run_id"] != event.framework_run_id
        ):
            raise RuntimeError(
                "legacy decision event identity does not match its durable business mapping"
            )
        event_facts[row["decision_event_id"]] = event

    report_rows = bind.execute(
        sa.text(
            """
            SELECT report_version_id, decision_event_id, report_payload, generated_at
            FROM formal_reports
            """
        )
    ).mappings()
    for row in report_rows:
        event = event_facts.get(row["decision_event_id"])
        if event is None:
            raise RuntimeError("legacy formal report references an unknown decision event")
        _validated_report_stage_results(
            _payload(row["report_payload"]),
            report_version_id=row["report_version_id"],
            decision_event_id=row["decision_event_id"],
            generated_at=row["generated_at"],
            event=event,
        )


def _record_legacy_stage_result(
    *,
    business_object_id: str,
    framework_run_id: str,
    decision_event_id: str | None,
    stage_result: dict[str, object],
    recorded_at: str,
) -> None:
    bind = op.get_bind()
    stage_event_id = _stage_event_id(
        business_object_id=business_object_id,
        framework_run_id=framework_run_id,
        decision_event_id=decision_event_id,
        stage_result=stage_result,
    )
    existing = bind.execute(
        sa.text(
            "SELECT stage_event_id FROM decision_stage_events "
            "WHERE stage_event_id = :stage_event_id"
        ),
        {"stage_event_id": stage_event_id},
    ).scalar_one_or_none()
    if existing is None:
        bind.execute(
            sa.text(
                """
                INSERT INTO decision_stage_events (
                    stage_event_id,
                    business_object_id,
                    framework_run_id,
                    decision_event_id,
                    stage_payload,
                    recorded_at
                ) VALUES (
                    :stage_event_id,
                    :business_object_id,
                    :framework_run_id,
                    :decision_event_id,
                    :stage_payload,
                    :recorded_at
                )
                """
            ),
            {
                "stage_event_id": stage_event_id,
                "business_object_id": business_object_id,
                "framework_run_id": framework_run_id,
                "decision_event_id": decision_event_id,
                "stage_payload": _canonical_json(stage_result),
                "recorded_at": recorded_at,
            },
        )


def _append_legacy_stage_history() -> None:
    bind = op.get_bind()
    event_context: dict[str, tuple[str, str, list[dict[str, object]]]] = {}
    event_rows = bind.execute(
        sa.text(
            """
            SELECT
                decision_event_id,
                business_object_id,
                framework_run_id,
                event_payload,
                committed_at
            FROM decision_events
            ORDER BY committed_at
            """
        )
    ).mappings()
    for row in event_rows:
        event_payload = _payload(row["event_payload"])
        existing_stage_results = event_payload.get("stage_results")
        validated_stage_results = _validated_stage_results(event_payload)
        if not isinstance(existing_stage_results, list):
            for stage_result in validated_stage_results:
                _record_legacy_stage_result(
                    business_object_id=row["business_object_id"],
                    framework_run_id=row["framework_run_id"],
                    decision_event_id=(
                        row["decision_event_id"]
                        if stage_result["phase"] == "BUSINESS_COMMIT"
                        else None
                    ),
                    stage_result=stage_result,
                    recorded_at=row["committed_at"],
                )
        event_context[row["decision_event_id"]] = (
            row["business_object_id"],
            row["framework_run_id"],
            validated_stage_results,
        )

    report_rows = bind.execute(
        sa.text(
            """
            SELECT
                report_version_id,
                decision_event_id,
                report_payload,
                generated_at
            FROM formal_reports
            ORDER BY generated_at
            """
        )
    ).mappings()
    for row in report_rows:
        report_payload = _payload(row["report_payload"])
        stage_results = report_payload.get("stage_results")
        if isinstance(stage_results, list):
            continue
        event = event_context.get(row["decision_event_id"])
        if event is None:
            raise RuntimeError("legacy formal report references an unknown decision event")
        business_object_id, framework_run_id, _ = event
        publication_stage = _publication_stage_result()
        _record_legacy_stage_result(
            business_object_id=business_object_id,
            framework_run_id=framework_run_id,
            decision_event_id=row["decision_event_id"],
            stage_result=publication_stage,
            recorded_at=row["generated_at"],
        )


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _decision_events_have_correction_lineage() -> bool:
    return any(
        column["name"] == "corrects_event_id"
        for column in sa.inspect(op.get_bind()).get_columns("decision_events")
    )


def _decision_events_are_canonical() -> bool:
    """Require the exact event table that permits append-only corrections."""
    inspector = sa.inspect(op.get_bind())
    columns = {
        str(column["name"]): column for column in inspector.get_columns("decision_events")
    }
    if columns.keys() != _REQUIRED_DECISION_EVENT_COLUMNS:
        return False
    if inspector.get_pk_constraint("decision_events").get("constrained_columns") != [
        "decision_event_id"
    ]:
        return False
    if (
        inspector.get_foreign_keys("decision_events")
        or inspector.get_check_constraints("decision_events")
    ):
        return False
    if not all(
        _has_expected_decision_event_column_definition(columns[column_name], column_name)
        for column_name in _REQUIRED_DECISION_EVENT_COLUMNS
    ):
        return False
    if any(inspector.get_unique_constraints("decision_events")):
        return False
    if any(inspector.get_indexes("decision_events")):
        return False
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return True
    for index in bind.execute(sa.text("PRAGMA index_list('decision_events')")).mappings():
        index_name = str(index["name"]).replace("'", "''")
        index_columns = [
            row["name"]
            for row in bind.execute(
                sa.text(f"PRAGMA index_info('{index_name}')")  # noqa: S608
            ).mappings()
        ]
        if index.get("origin") != "pk" or index_columns != ["decision_event_id"]:
            return False
    return True


def _has_expected_decision_event_column_definition(
    column: dict[str, object],
    column_name: str,
) -> bool:
    """Fail closed unless an event column has the canonical storage semantics."""
    if (
        column.get("nullable") is not (column_name == "corrects_event_id")
        or column.get("default") is not None
        or column.get("computed") is not None
    ):
        return False
    column_type = column.get("type")
    expected_length = _DECISION_EVENT_STRING_COLUMN_LENGTHS[column_name]
    return isinstance(column_type, sa.String) and column_type.length == expected_length


def _repair_interrupted_event_table_replacement() -> None:
    """Complete or discard the only non-transactional DDL intermediate state."""
    has_events = _has_table("decision_events")
    has_replacement = _has_table("decision_events_replacement")
    if not has_replacement:
        return
    if has_events:
        op.drop_table("decision_events_replacement")
        return
    op.rename_table("decision_events_replacement", "decision_events")


def _ensure_event_correction_lineage() -> None:
    if _decision_events_are_canonical():
        return
    preserves_correction_lineage = _decision_events_have_correction_lineage()
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
    if preserves_correction_lineage:
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
                corrects_event_id,
                event_payload,
                committed_at
            FROM decision_events
            """
        )
    else:
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


def _ensure_notification_attempts_table() -> None:
    if _has_table("decision_notification_attempts"):
        inspector = sa.inspect(op.get_bind())
        columns = {
            str(column["name"]): column
            for column in inspector.get_columns("decision_notification_attempts")
        }
        missing_columns = _REQUIRED_NOTIFICATION_ATTEMPT_COLUMNS - columns.keys()
        incompatible_columns = [
            column_name
            for column_name in _REQUIRED_NOTIFICATION_ATTEMPT_COLUMNS - missing_columns
            if not _has_expected_notification_attempt_column_definition(
                columns[column_name],
                column_name,
            )
        ]
        has_sequence_primary_key = inspector.get_pk_constraint(
            "decision_notification_attempts"
        ).get("constrained_columns") == ["sequence"]
        sequence_column = columns.get("sequence")
        has_generated_sequence = sequence_column is not None and _has_generated_sequence(
            sequence_column,
            has_sequence_primary_key,
        )
        has_unique_notification_id = _has_unique_notification_attempt_id(
            inspector,
        )
        has_no_extra_columns = columns.keys() == _REQUIRED_NOTIFICATION_ATTEMPT_COLUMNS
        has_no_extra_constraints = (
            not inspector.get_foreign_keys("decision_notification_attempts")
            and not inspector.get_check_constraints("decision_notification_attempts")
        )
        if (
            missing_columns
            or incompatible_columns
            or not has_no_extra_columns
            or not has_sequence_primary_key
            or not has_generated_sequence
            or not has_unique_notification_id
            or not has_no_extra_constraints
        ):
            raise RuntimeError("notification attempts table has unexpected schema")
        return
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


def _has_expected_notification_attempt_column_definition(
    column: dict[str, object],
    column_name: str,
) -> bool:
    """Fail closed unless an interrupted table retains the canonical storage contract."""
    if (
        column.get("nullable") is not False
        or column.get("default") is not None
        or column.get("computed") is not None
    ):
        return False
    column_type = column.get("type")
    if column_name == "sequence":
        return isinstance(column_type, sa.Integer)
    expected_length = _NOTIFICATION_ATTEMPT_STRING_COLUMN_LENGTHS[column_name]
    return isinstance(column_type, sa.String) and column_type.length == expected_length


def _has_generated_sequence(column: dict[str, object], has_primary_key: bool) -> bool:
    """Recognize the SQLite INTEGER PRIMARY KEY rowid allocation used by this migration."""
    if not has_primary_key or not isinstance(column.get("type"), sa.Integer):
        return False
    if op.get_bind().dialect.name == "sqlite":
        return True
    return column.get("autoincrement") is True


def _has_unique_notification_attempt_id(inspector: sa.Inspector) -> bool:
    """Recognize SQLite's automatic UNIQUE index after an interrupted table create."""
    found_notification_id = False
    for constraint in inspector.get_unique_constraints("decision_notification_attempts"):
        if constraint.get("column_names") != ["notification_attempt_id"]:
            return False
        found_notification_id = True
    for index in inspector.get_indexes("decision_notification_attempts"):
        if not index.get("unique"):
            return False
        if index.get("column_names") != ["notification_attempt_id"]:
            return False
        found_notification_id = True
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return found_notification_id
    for index in bind.execute(
        sa.text("PRAGMA index_list('decision_notification_attempts')")
    ).mappings():
        if not index["unique"]:
            return False
        index_name = str(index["name"]).replace("'", "''")
        index_columns = [
            row["name"]
            for row in bind.execute(
                sa.text(f"PRAGMA index_info('{index_name}')")  # noqa: S608
            ).mappings()
        ]
        if index_columns != ["notification_attempt_id"]:
            return False
        found_notification_id = True
    return found_notification_id


def _notification_attempt_count() -> int:
    if not _has_table("decision_notification_attempts"):
        return 0
    return int(
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM decision_notification_attempts"))
        .scalar_one()
    )


def _repair_interrupted_event_table_downgrade() -> bool:
    """Restore the old event table when a SQLite downgrade stopped mid-replacement."""
    has_events = _has_table("decision_events")
    has_replacement = _has_table("decision_events_replacement")
    if has_replacement:
        if has_events:
            op.drop_table("decision_events_replacement")
        else:
            op.rename_table("decision_events_replacement", "decision_events")
            has_events = True
    if not has_events:
        raise RuntimeError("interrupted decision event downgrade has no recoverable event table")
    return not _decision_events_have_correction_lineage()


def _authoritative_event_table_for_upgrade() -> str:
    """Choose the only durable event table before performing any repair DDL."""
    if _has_table("decision_events"):
        return "decision_events"
    if _has_table("decision_events_replacement"):
        return "decision_events_replacement"
    raise RuntimeError("interrupted decision event migration has no authoritative event table")


def upgrade() -> None:
    event_table = _authoritative_event_table_for_upgrade()
    _preflight_legacy_stage_history(event_table)
    _repair_interrupted_event_table_replacement()
    _ensure_event_correction_lineage()
    _append_legacy_stage_history()
    _ensure_notification_attempts_table()


def downgrade() -> None:
    bind = op.get_bind()
    if _repair_interrupted_event_table_downgrade():
        notification_count = _notification_attempt_count()
        if notification_count:
            raise RuntimeError("cannot downgrade while append-only notification attempts exist")
        if _has_table("decision_notification_attempts"):
            op.drop_table("decision_notification_attempts")
        return
    correction_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM decision_events WHERE corrects_event_id IS NOT NULL")
    ).scalar_one()
    if correction_count:
        raise RuntimeError("cannot downgrade while append-only correction facts exist")
    notification_count = _notification_attempt_count()
    if notification_count:
        raise RuntimeError("cannot downgrade while append-only notification attempts exist")
    if _has_table("decision_notification_attempts"):
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
