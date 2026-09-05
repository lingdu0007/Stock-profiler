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
    return [
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
        {
            "phase": "BUSINESS_COMMIT",
            "status": "SUCCEEDED",
            "gate_results": [{"gate_id": "HOST_RESULT_SAVED", "status": "PASSED"}],
            "reasons": [],
        },
    ]


def _publication_stage_result() -> dict[str, object]:
    return {
        "phase": "PUBLICATION",
        "status": "SUCCEEDED",
        "gate_results": [{"gate_id": "EVENT_COMMITTED", "status": "PASSED"}],
        "reasons": [],
    }


def _payload(value: str) -> dict[str, object]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise RuntimeError("legacy decision ledger payload must be a JSON object")
    return payload


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
        stage_results = event_payload.get("stage_results")
        if isinstance(stage_results, list):
            validated_stage_results = stage_results
        else:
            validated_stage_results = _legacy_stage_results(event_payload)
            for stage_result in validated_stage_results:
                if not isinstance(stage_result, dict):
                    raise RuntimeError("legacy decision stage result must be a JSON object")
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
    _append_legacy_stage_history()
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
        sa.text("SELECT COUNT(*) FROM decision_events WHERE corrects_event_id IS NOT NULL")
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
