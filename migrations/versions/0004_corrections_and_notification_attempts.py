"""Add correction lineage and append-only notification attempts."""

from __future__ import annotations

import json
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

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
_STAGE_STATUS_BY_PHASE = {
    "FRAMEWORK_RUN": frozenset(
        {"CREATED", "RUNNING", "WAITING", "SUCCEEDED", "REJECTED", "FAILED", "CANCELLED"}
    ),
    "HOST_VALIDATION": frozenset({"SUCCEEDED", "FAILED"}),
    "BUSINESS_DECISION": frozenset({"SUCCEEDED", "REJECTED", "ABSTAINED", "FAILED"}),
    "ADJUDICATION_LIFECYCLE": frozenset({"PENDING", "UNKNOWN"}),
    "VALIDITY_LIFECYCLE": frozenset({"EXPIRED", "UNKNOWN"}),
    "EXECUTION_LIFECYCLE": frozenset({"EXECUTION_BLOCKED", "UNKNOWN"}),
    "COMMIT_RECONCILIATION": frozenset({"UNKNOWN"}),
    "BUSINESS_COMMIT": frozenset({"SUCCEEDED", "FAILED"}),
    "PUBLICATION": frozenset({"SUCCEEDED", "FAILED", "UNKNOWN"}),
    "NOTIFICATION": frozenset({"SUCCEEDED", "FAILED"}),
    "CORRECTION": frozenset({"SUCCEEDED"}),
}
_REQUIRED_STAGE_RESULT_FIELDS = frozenset({"phase", "status", "gate_results", "reasons"})
_REQUIRED_GATE_RESULT_FIELDS = frozenset({"gate_id", "status"})
_REQUIRED_EVENT_FIELDS = frozenset(
    {
        "decision_event_id",
        "business_object_id",
        "framework_run_id",
        "case",
        "result",
        "validation_status",
        "committed_at",
    }
)
_OPTIONAL_EVENT_FIELDS = frozenset({"stage_results", "corrects_event_id", "generated_at"})
_REQUIRED_CASE_FIELDS = frozenset(
    {
        "synthetic",
        "generator_version",
        "seed",
        "case_id",
        "business_identity",
        "knowledge_cutoff",
        "report_generated_at",
        "evidence_clock",
        "qualification_scope",
        "version_bundle",
        "agent_definition",
        "input",
        "expected_external_result",
    }
)
_REQUIRED_RESULT_FIELDS = frozenset({"outcome_code", "summary", "key_reasons"})
_REQUIRED_VERSION_BUNDLE_FIELDS = frozenset(
    {
        "case_contract_version",
        "host_contract_version",
        "host_application_version",
        "host_source_sha",
        "agent_definition_id",
        "agent_definition_version",
        "model_adapter_id",
        "routing_policy_version",
        "output_contract_version",
        "report_projection_contract_version",
        "m_agent_version",
        "m_agent_wheel_url",
        "m_agent_wheel_sha256",
        "m_agent_release_commit",
    }
)
_REQUIRED_EVIDENCE_CLOCK_FIELDS = frozenset(
    {"fact_effective_at", "source_published_at", "acquired_at", "validated_at"}
)


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
    normalized_results: list[dict[str, object]] = []
    for stage_result in candidates:
        if set(stage_result) != _REQUIRED_STAGE_RESULT_FIELDS:
            raise RuntimeError("legacy decision stage contract is invalid")
        phase = stage_result.get("phase")
        status = stage_result.get("status")
        gates = stage_result.get("gate_results")
        reasons = stage_result.get("reasons")
        if (
            not isinstance(phase, str)
            or not isinstance(status, str)
            or status not in _STAGE_STATUS_BY_PHASE.get(phase, ())
            or not isinstance(gates, list)
            or not isinstance(reasons, list)
            or not all(isinstance(reason, str) for reason in reasons)
        ):
            raise RuntimeError("legacy decision stage contract is invalid")
        for gate in gates:
            if (
                not isinstance(gate, dict)
                or set(gate) != _REQUIRED_GATE_RESULT_FIELDS
                or not isinstance(gate.get("gate_id"), str)
                or gate.get("status") not in {"PASSED", "FAILED", "UNKNOWN"}
            ):
                raise RuntimeError("legacy decision stage contract is invalid")
        normalized_results.append(stage_result)
    return normalized_results


def _validated_event_fact(
    payload: dict[str, object],
    *,
    decision_event_id: str,
    business_object_id: str,
    framework_run_id: str,
) -> dict[str, object]:
    if (
        not payload.keys() >= _REQUIRED_EVENT_FIELDS
        or not payload.keys() <= (_REQUIRED_EVENT_FIELDS | _OPTIONAL_EVENT_FIELDS)
        or payload.get("corrects_event_id") is not None
        or not isinstance(payload.get("decision_event_id"), str)
        or not isinstance(payload.get("business_object_id"), str)
        or not isinstance(payload.get("framework_run_id"), str)
        or not isinstance(payload.get("validation_status"), str)
        or not isinstance(payload.get("committed_at"), str)
    ):
        raise RuntimeError("legacy decision event contract is invalid")
    stage_results = _validated_stage_results(payload)
    case = _validated_case_payload(payload.get("case"))
    result = _validated_result(payload.get("result"))
    if payload.get("generated_at") is not None and not isinstance(payload.get("generated_at"), str):
        raise RuntimeError("legacy decision event contract is invalid")
    if (
        payload["validation_status"] != "PASSED"
        or result != case["expected_external_result"]
        or not _has_succeeded_stage(stage_results, "HOST_VALIDATION")
    ):
        raise RuntimeError("legacy decision event lacks a confirmed host validation")
    if not _has_succeeded_stage(stage_results, "BUSINESS_COMMIT"):
        raise RuntimeError("legacy decision event lacks a confirmed business commit")
    if (
        payload["decision_event_id"] != decision_event_id
        or payload["business_object_id"] != business_object_id
        or payload["framework_run_id"] != framework_run_id
        or _business_object_id(case) != business_object_id
        or _framework_run_id(case) != framework_run_id
        or decision_event_id
        not in {
            _decision_event_id(case, framework_run_id),
            _legacy_decision_event_id(case, framework_run_id),
        }
    ):
        raise RuntimeError("legacy decision event identity does not match its row")
    return payload


def _validated_report_stage_results(
    payload: dict[str, object],
    *,
    report_version_id: str,
    decision_event_id: str,
    generated_at: str,
    event: dict[str, object],
) -> list[dict[str, object]]:
    case = _validated_case_payload(event.get("case"))
    event_stage_results = _validated_stage_results(event)
    expected_payload = _formal_report_payload(
        event,
        case,
        report_version_id,
        event_stage_results,
    )
    stage_results = _validated_stage_results(payload)
    if (
        payload != expected_payload
        or report_version_id != _report_version_id(case, decision_event_id)
        or payload.get("event_id") != decision_event_id
        or payload.get("generated_at") != generated_at
    ):
        raise RuntimeError("legacy formal report identity does not match its row or event")
    return stage_results


def _validated_case_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _REQUIRED_CASE_FIELDS:
        raise RuntimeError("legacy decision event contract is invalid")
    if (
        not isinstance(value.get("synthetic"), bool)
        or not isinstance(value.get("generator_version"), str)
        or not isinstance(value.get("seed"), int)
        or not isinstance(value.get("case_id"), str)
        or not isinstance(value.get("business_identity"), str)
        or not isinstance(value.get("knowledge_cutoff"), str)
        or not isinstance(value.get("report_generated_at"), str)
        or not isinstance(value.get("qualification_scope"), str)
        or not isinstance(value.get("input"), dict)
    ):
        raise RuntimeError("legacy decision event contract is invalid")
    evidence_clock = value.get("evidence_clock")
    if (
        not isinstance(evidence_clock, dict)
        or set(evidence_clock) != _REQUIRED_EVIDENCE_CLOCK_FIELDS
        or not all(isinstance(clock, str) for clock in evidence_clock.values())
    ):
        raise RuntimeError("legacy decision event contract is invalid")
    version_bundle = value.get("version_bundle")
    if (
        not isinstance(version_bundle, dict)
        or set(version_bundle) != _REQUIRED_VERSION_BUNDLE_FIELDS
        or not all(isinstance(item, str) for item in version_bundle.values())
    ):
        raise RuntimeError("legacy decision event contract is invalid")
    agent_definition = value.get("agent_definition")
    if (
        not isinstance(agent_definition, dict)
        or not isinstance(agent_definition.get("definition_id"), str)
        or not isinstance(agent_definition.get("version"), str)
        or agent_definition.get("definition_id") != version_bundle["agent_definition_id"]
        or agent_definition.get("version") != version_bundle["agent_definition_version"]
    ):
        raise RuntimeError("legacy decision event contract is invalid")
    _validated_result(value.get("expected_external_result"))
    return value


def _validated_result(value: object) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != _REQUIRED_RESULT_FIELDS
        or not isinstance(value.get("outcome_code"), str)
        or not isinstance(value.get("summary"), str)
        or not isinstance(value.get("key_reasons"), list)
        or not all(isinstance(reason, str) for reason in value["key_reasons"])
    ):
        raise RuntimeError("legacy decision event contract is invalid")
    return value


def _has_succeeded_stage(
    stage_results: list[dict[str, object]],
    phase: str,
) -> bool:
    return any(
        stage_result["phase"] == phase and stage_result["status"] == "SUCCEEDED"
        for stage_result in stage_results
    )


def _stable_id(kind: str, value: object) -> str:
    return f"{kind}-{sha256(_canonical_json(value).encode()).hexdigest()}"


def _business_object_id(case: dict[str, object]) -> str:
    version_bundle = case["version_bundle"]
    assert isinstance(version_bundle, dict)
    return _stable_id(
        "business-object",
        {
            "business_identity": case["business_identity"],
            "case_contract_version": version_bundle["case_contract_version"],
        },
    )


def _framework_run_id(case: dict[str, object]) -> str:
    version_bundle = case["version_bundle"]
    assert isinstance(version_bundle, dict)
    return _stable_id(
        "framework-run",
        {
            "business_object_id": _business_object_id(case),
            "frozen_input_fingerprint": sha256(_canonical_json(case).encode()).hexdigest(),
            "definition_id": version_bundle["agent_definition_id"],
            "definition_version": version_bundle["agent_definition_version"],
        },
    )


def _decision_event_id(case: dict[str, object], framework_run_id: str) -> str:
    version_bundle = dict(case["version_bundle"])
    version_bundle.pop("host_application_version")
    version_bundle.pop("host_source_sha")
    return _decision_event_id_for_version_bundle(case, framework_run_id, version_bundle)


def _legacy_decision_event_id(case: dict[str, object], framework_run_id: str) -> str:
    return _decision_event_id_for_version_bundle(
        case, framework_run_id, dict(case["version_bundle"])
    )


def _decision_event_id_for_version_bundle(
    case: dict[str, object],
    framework_run_id: str,
    version_bundle: dict[str, object],
) -> str:
    return _stable_id(
        "decision-event",
        {
            "business_object_id": _business_object_id(case),
            "framework_run_id": framework_run_id,
            "expected_external_result": case["expected_external_result"],
            "version_bundle": version_bundle,
        },
    )


def _report_version_id(case: dict[str, object], decision_event_id: str) -> str:
    version_bundle = case["version_bundle"]
    assert isinstance(version_bundle, dict)
    return _stable_id(
        "report-version",
        {
            "decision_event_id": decision_event_id,
            "report_projection_contract_version": version_bundle[
                "report_projection_contract_version"
            ],
        },
    )


def _formal_report_payload(
    event: dict[str, object],
    case: dict[str, object],
    report_version_id: str,
    stage_results: list[dict[str, object]],
) -> dict[str, object]:
    version_bundle = case["version_bundle"]
    assert isinstance(version_bundle, dict)
    generated_at = event.get("generated_at") or case["report_generated_at"]
    payload: dict[str, object] = {
        "report_version_id": report_version_id,
        "event_id": event["decision_event_id"],
        "business_object_id": event["business_object_id"],
        "framework_run_id": event["framework_run_id"],
        "case_id": case["case_id"],
        "synthetic": True,
        "qualification_scope": case["qualification_scope"],
        "generated_at": generated_at,
        "knowledge_cutoff": case["knowledge_cutoff"],
        "evidence_clock": case["evidence_clock"],
        "version_bundle": version_bundle,
        "result": event["result"],
    }
    if version_bundle["report_projection_contract_version"] == "1.0.0":
        return payload
    payload["stage_results"] = [
        *stage_results,
        _publication_stage_result(),
    ]
    payload["corrects_event_id"] = None
    return payload


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
    event_facts: dict[str, dict[str, object]] = {}
    for row in _event_rows_for_preflight(event_table):
        event = _validated_event_fact(
            _payload(row["event_payload"]),
            decision_event_id=row["decision_event_id"],
            business_object_id=row["business_object_id"],
            framework_run_id=row["framework_run_id"],
        )
        case = _validated_case_payload(event["case"])
        mapping = mappings.get(row["business_object_id"])
        if (
            mapping is None
            or mapping["case_id"] != case["case_id"]
            or mapping["frozen_input_fingerprint"]
            != sha256(_canonical_json(case).encode()).hexdigest()
            or mapping["framework_run_id"] != event["framework_run_id"]
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
