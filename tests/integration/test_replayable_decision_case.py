from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from threading import Barrier
from typing import Any, Literal

import pytest
from alembic import command, op
from alembic.config import Config
from m_agent.adapters import DeterministicModelAdapter
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    OutputContract,
    Runner,
    RunStatus,
    StructuredOutputMode,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from stock_profiler.adapters.m_agent import frozen_decision_case as frozen_adapter
from stock_profiler.adapters.m_agent.frozen_decision_case import FrameworkRunResult
from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionEventCommitUncertainError,
    DecisionLedger,
    FormalReportCommitUncertainError,
)
from stock_profiler.adapters.persistence.runtime_ownership import (
    RuntimeStorage,
    initialize_runtime_storage,
)
from stock_profiler.bootstrap import decision_cases as case_bootstrap
from stock_profiler.bootstrap.decision_cases import (
    correct_default_frozen_decision_case,
    get_formal_report,
    retry_default_frozen_decision_case_notification,
    run_default_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings, load_settings
from stock_profiler.modules.decision_cases import domain as decision_domain
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FormalReport,
    FrozenDecisionCase,
    GateResult,
    StageResult,
    load_frozen_decision_case,
)

ROOT = Path(__file__).resolve().parents[2]


class MutableClock:
    """Drive observable write times through the public D0 service seam."""

    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


def _load_result_family_fixture(name: str) -> FrozenDecisionCase:
    payload = json.loads(
        (ROOT / "tests" / "fixtures" / "synthetic" / "result-families" / f"{name}.json").read_text(
            encoding="utf-8"
        )
    )
    return FrozenDecisionCase.model_validate(payload)


def _replay_frozen_case_in_child(
    settings: Settings,
    start_together: Any,
    outcomes: Any,
) -> None:
    """Exercise the durable Run lease with no process-local execution lock."""
    try:
        start_together.wait(timeout=5)
        execution = run_default_frozen_decision_case(settings)
    except BaseException as error:
        outcomes.put(("ERROR", f"{type(error).__name__}: {error}"))
    else:
        outcomes.put(("OK", execution.model_dump(mode="json")))


def test_successful_case_publishes_one_event_and_report_after_a_framework_run(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status == "SUCCEEDED"
    assert outcome.business_lifecycle is None
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report is not None
    assert outcome.report.event_id == outcome.decision_event_id
    assert outcome.report.framework_run_id == outcome.framework_run_id
    assert outcome.report.synthetic is True
    assert outcome.report.qualification_scope == "D0_SYNTHETIC_CONTRACT_ONLY"
    assert outcome.report.evidence_clock.validated_at == "2042-05-17T15:18:00Z"
    assert outcome.report.generated_at == case.report_generated_at
    assert [stage.status for stage in outcome.stage_results if stage.phase == "FRAMEWORK_RUN"] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
    ]
    assert [
        (stage.phase, stage.status)
        for stage in outcome.stage_results
        if stage.phase == "HOST_VALIDATION"
    ] == [("HOST_VALIDATION", "SUCCEEDED")]
    decision_stage = next(
        stage for stage in outcome.stage_results if stage.phase == "BUSINESS_DECISION"
    )
    assert decision_stage.reasons == (
        "All required fictional evidence records are present.",
        "The output is D0 synthetic evidence and is not a recommendation.",
    )
    assert get_formal_report(outcome.report_version_id, migrated_settings) == outcome.report


def test_formal_report_projection_fixture_matches_the_frozen_runtime(
    migrated_settings: Settings,
) -> None:
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "synthetic" / "formal_report_projection.json").read_text(
            encoding="utf-8"
        )
    )
    outcome = run_default_frozen_decision_case(
        migrated_settings,
        clock=MutableClock(datetime(2042, 5, 17, 16, 1, tzinfo=UTC)),
    )

    assert outcome.report is not None
    assert fixture["report"] == outcome.report.model_dump(mode="json")


def test_append_only_ledger_records_use_controlled_observation_and_write_clocks(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    clock = MutableClock(datetime(2042, 5, 17, 16, 1, tzinfo=UTC))
    execution = run_default_frozen_decision_case(migrated_settings, clock=clock)
    assert execution.report is not None
    assert execution.report.generated_at == "2042-05-17T16:01:00Z"
    clock.advance(timedelta(minutes=1))
    retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
        clock=clock,
    )
    clock.advance(timedelta(minutes=1))
    retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "SUCCEEDED",
        clock=clock,
    )
    clock.advance(timedelta(minutes=1))
    correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
        clock=clock,
    )

    engine = create_engine(migrated_settings.app_database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT created_at FROM decision_case_business_objects "
                    "WHERE business_object_id = :business_object_id"
                ),
                {"business_object_id": case.business_object_id},
            ).scalar_one()
            == case.report_generated_at
        )
        initial_stage_clocks = set(
            connection.execute(
                text(
                    """
                    SELECT recorded_at
                    FROM decision_stage_events
                    WHERE business_object_id = :business_object_id
                      AND decision_event_id IS NULL
                    """
                ),
                {"business_object_id": case.business_object_id},
            ).scalars()
        )
        correction_stage_clocks = set(
            connection.execute(
                text(
                    """
                    SELECT recorded_at
                    FROM decision_stage_events
                    WHERE decision_event_id = :decision_event_id
                    """
                ),
                {"decision_event_id": correction.report.event_id},
            ).scalars()
        )
        notification_clocks = (
            connection.execute(
                text(
                    """
                    SELECT recorded_at
                    FROM decision_notification_attempts
                    WHERE report_version_id = :report_version_id
                    ORDER BY sequence
                    """
                ),
                {"report_version_id": execution.report.report_version_id},
            )
            .scalars()
            .all()
        )
        event_commit_clocks = (
            connection.execute(
                text(
                    """
                    SELECT committed_at
                    FROM decision_events
                    WHERE business_object_id = :business_object_id
                    ORDER BY committed_at
                    """
                ),
                {"business_object_id": case.business_object_id},
            )
            .scalars()
            .all()
        )

    assert initial_stage_clocks == {case.report_generated_at}
    assert correction_stage_clocks == {"2042-05-17T16:04:00Z"}
    assert notification_clocks == [
        "2042-05-17T16:02:00Z",
        "2042-05-17T16:03:00Z",
    ]
    assert event_commit_clocks == [
        "2042-05-17T16:01:00Z",
        "2042-05-17T16:04:00Z",
    ]


def test_report_lookup_by_event_requires_a_committed_event(
    migrated_settings: Settings,
) -> None:
    outcome = run_default_frozen_decision_case(migrated_settings)
    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM decision_events WHERE decision_event_id = :decision_event_id"),
            {"decision_event_id": outcome.decision_event_id},
        )

    with engine.connect() as connection:
        assert (
            DecisionLedger.from_settings(migrated_settings).get_formal_report_for_event(
                outcome.decision_event_id,
                connection,
            )
            is None
        )


def test_framework_success_and_unmatched_host_rejection_remain_distinct_and_closed(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        frozen_adapter,
        "_deterministic_model_response",
        lambda _: (
            '{"key_reasons":["The host rejected the frozen input."],'
            '"outcome_code":"SYNTHETIC_INPUT_REJECTED",'
            '"summary":"Synthetic D0 decision case was rejected by a host gate."}'
        ),
    )

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [stage.status for stage in outcome.stage_results if stage.phase == "FRAMEWORK_RUN"] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
    ]
    validation_stage = next(
        stage for stage in outcome.stage_results if stage.phase == "HOST_VALIDATION"
    )
    assert validation_stage.status == "FAILED"
    assert validation_stage.gate_results[0].gate_id == "FROZEN_RESULT_MATCH"
    assert validation_stage.gate_results[0].status == "FAILED"
    assert validation_stage.reasons == ("UNSUPPORTED_SYNTHETIC_RESULT",)


@pytest.mark.parametrize("status", ("REJECTED", "FAILED", "CANCELLED"))
def test_terminal_framework_outcomes_pass_the_terminal_gate(
    migrated_settings: Settings,
    status: Literal["REJECTED", "FAILED", "CANCELLED"],
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    stage = service._framework_stage_result(
        case,
        FrameworkRunResult(
            run_id=case.framework_run_id,
            status=status,
            output=None,
            error_code=f"SYNTHETIC_{status}",
        ),
    )

    assert stage.status == status
    assert stage.gate_results[0] == GateResult(gate_id="RUN_TERMINAL", status="PASSED")


@pytest.mark.parametrize(
    (
        "fixture_name",
        "outcome_code",
        "expected_business_status",
        "expected_lifecycle_owner",
        "expected_phase",
        "expected_outcome_gate_id",
        "expected_outcome_gate_status",
    ),
    (
        (
            "input-rejected",
            "SYNTHETIC_INPUT_REJECTED",
            "REJECTED",
            None,
            "BUSINESS_DECISION",
            "DECISION_ACCEPTED",
            "FAILED",
        ),
        (
            "result-abstained",
            "SYNTHETIC_RESULT_ABSTAINED",
            "ABSTAINED",
            None,
            "BUSINESS_DECISION",
            "ABSTENTION_RECORDED",
            "PASSED",
        ),
        (
            "result-failed",
            "SYNTHETIC_RESULT_FAILED",
            "FAILED",
            None,
            "BUSINESS_DECISION",
            "DECISION_COMPLETED",
            "FAILED",
        ),
        (
            "result-pending",
            "SYNTHETIC_RESULT_PENDING",
            None,
            "ADJUDICATION",
            "ADJUDICATION_LIFECYCLE",
            "ADJUDICATION_COMPLETED",
            "UNKNOWN",
        ),
        (
            "result-expired",
            "SYNTHETIC_RESULT_EXPIRED",
            None,
            "VALIDITY",
            "VALIDITY_LIFECYCLE",
            "VALIDITY_WINDOW",
            "FAILED",
        ),
        (
            "result-execution-blocked",
            "SYNTHETIC_RESULT_EXECUTION_BLOCKED",
            None,
            "EXECUTION",
            "EXECUTION_LIFECYCLE",
            "EXECUTION_AVAILABLE",
            "FAILED",
        ),
        (
            "result-unknown",
            "SYNTHETIC_RESULT_UNKNOWN",
            None,
            "ADJUDICATION",
            "ADJUDICATION_LIFECYCLE",
            "DECISION_DETERMINED",
            "UNKNOWN",
        ),
    ),
)
def test_host_result_families_keep_their_own_saved_lifecycle(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    outcome_code: str,
    expected_business_status: str | None,
    expected_lifecycle_owner: str | None,
    expected_phase: str,
    expected_outcome_gate_id: str,
    expected_outcome_gate_status: str,
) -> None:
    variant_case = _load_result_family_fixture(fixture_name)
    expected_result = variant_case.expected_external_result
    monkeypatch.setattr(case_bootstrap, "load_frozen_decision_case", lambda _: variant_case)

    outcome = run_default_frozen_decision_case(migrated_settings)
    replayed = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status == expected_business_status
    assert (
        outcome.business_lifecycle.owner if outcome.business_lifecycle is not None else None
    ) == expected_lifecycle_owner
    assert (
        outcome.business_lifecycle.status if outcome.business_lifecycle is not None else None
    ) == (
        None if expected_lifecycle_owner is None else outcome_code.removeprefix("SYNTHETIC_RESULT_")
    )
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.report is not None
    assert [
        result.status for result in outcome.stage_results if result.phase == "FRAMEWORK_RUN"
    ] == ["CREATED", "RUNNING", "SUCCEEDED"]
    assert [
        (result.phase, result.status)
        for result in outcome.stage_results
        if result.phase == "HOST_VALIDATION"
    ] == [("HOST_VALIDATION", "SUCCEEDED")]
    outcome_stage = next(
        result for result in outcome.stage_results if result.phase == expected_phase
    )
    assert outcome_stage.status == (
        expected_business_status or outcome_code.removeprefix("SYNTHETIC_RESULT_")
    )
    assert [(gate.gate_id, gate.status) for gate in outcome_stage.gate_results] == [
        ("OUTPUT_CONTRACT", "PASSED"),
        (expected_outcome_gate_id, expected_outcome_gate_status),
    ]
    assert outcome_stage.reasons == expected_result.key_reasons
    assert outcome.report.result == expected_result
    assert outcome.report.event_id == variant_case.decision_event_id
    assert outcome.report.report_version_id == variant_case.report_version_id
    assert replayed.framework_run_id == variant_case.framework_run_id
    assert replayed.decision_event_id == variant_case.decision_event_id
    assert replayed.report_version_id == variant_case.report_version_id
    counts = DecisionLedger.from_settings(migrated_settings).counts()
    assert counts == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_default_model_response_does_not_read_the_expected_result_oracle(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    changed_expected_result = case.expected_external_result.model_copy(
        update={"summary": "This changed expected result must not alter model output."}
    )
    variant_case = case.model_copy(update={"expected_external_result": changed_expected_result})
    monkeypatch.setattr(case_bootstrap, "load_frozen_decision_case", lambda _: variant_case)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert outcome.stage_results[-1].phase == "HOST_VALIDATION"
    assert outcome.stage_results[-1].reasons == ("UNSUPPORTED_SYNTHETIC_RESULT",)


def test_framework_waiting_is_saved_without_inventing_a_host_result(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def waiting_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="WAITING",
            output=None,
        )

    monkeypatch.setattr(case_bootstrap, "execute_frozen_decision_case", waiting_framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "WAITING"
    assert outcome.business_result_status is None
    assert outcome.business_lifecycle is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results] == [
        ("FRAMEWORK_RUN", "WAITING")
    ]
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


def test_actual_runtime_waiting_run_recovers_to_a_terminal_result_without_a_replacement_identity(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    runtime = initialize_runtime_storage(migrated_settings)

    async def create_waiting_run() -> None:
        adapter = DeterministicModelAdapter(
            responses=(frozen_adapter._deterministic_model_response(case),),
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
            ),
        )
        definition = AgentDefinition.for_adapter(
            definition_id=case.agent_definition.definition_id,
            version=case.agent_definition.version,
            instructions=case.agent_definition.instructions,
            model_adapter=adapter,
            output_contract=OutputContract(
                contract_id=case.agent_definition.output_contract.contract_id,
                version=case.agent_definition.output_contract.version,
                schema=case.agent_definition.output_contract.json_schema,
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
            ),
        )
        registry = DefinitionRegistry()
        registry.register(definition)
        runner = Runner(registry=registry, store=runtime.run_store)
        created = await runner.create_run(
            definition.definition_id,
            definition.version,
            json.dumps(case.input, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            run_id=case.framework_run_id,
        )
        waiting = await runtime.run_store.transition_run(
            created.run_id,
            expected_version=created.version,
            status=RunStatus.WAITING,
            waiting_reason="DEFINITION_UNAVAILABLE",
        )
        assert waiting.status is RunStatus.WAITING

    asyncio.run(create_waiting_run())

    recovered = run_default_frozen_decision_case(migrated_settings)
    replayed = run_default_frozen_decision_case(migrated_settings)

    assert recovered.framework_run_id == case.framework_run_id
    assert replayed.framework_run_id == case.framework_run_id
    assert recovered.framework_run_status == "SUCCEEDED"
    assert replayed.framework_run_status == "SUCCEEDED"
    assert recovered.publication_status == "PUBLISHED"
    assert replayed.publication_status == "PUBLISHED"
    assert recovered.report is not None
    assert replayed.report == recovered.report
    assert any(
        stage.phase == "FRAMEWORK_RUN" and stage.reasons == ("DEFINITION_UNAVAILABLE",)
        for stage in recovered.stage_results
    )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_durable_framework_creation_is_recorded_before_framework_start_completes(
    migrated_settings: Settings,
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    runtime = initialize_runtime_storage(migrated_settings)
    ledger = DecisionLedger(runtime.engine)
    ledger.persist_business_mapping_before_framework(case)

    async def record_creation_then_interrupt(
        transition: frozen_adapter.FrameworkRunTransition,
    ) -> None:
        with ledger.serialize_case_execution() as connection:
            ledger.record_stage_result(
                connection,
                case=case,
                framework_run_id=case.framework_run_id,
                stage_result=service._framework_transition_stage_result(transition),
            )
        if transition.status == "CREATED":
            raise RuntimeError("synthetic interruption after durable creation")

    with pytest.raises(RuntimeError, match="durable creation"):
        asyncio.run(
            frozen_adapter.execute_frozen_decision_case(
                case,
                runtime,
                record_creation_then_interrupt,
            )
        )

    saved = ledger.get_stage_results(case.business_object_id)
    assert [(stage.status, stage.reasons) for stage in saved] == [
        ("CREATED", ("FRAMEWORK_RUN_CREATED",)),
    ]

    with sqlite3.connect(runtime.m_agent_run_store_path) as connection:
        connection.execute("DELETE FROM run_payloads WHERE run_id = ?", (case.framework_run_id,))
        connection.execute("DELETE FROM runs WHERE run_id = ?", (case.framework_run_id,))

    with pytest.raises(DecisionEventCommitError, match="durable framework recovery failed"):
        run_default_frozen_decision_case(migrated_settings)

    assert ledger.get_stage_results(case.business_object_id) == saved
    assert ledger.counts() == {"business_objects": 1, "decision_events": 0, "reports": 0}


def test_repeated_framework_waiting_observations_are_preserved(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def waiting_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="WAITING",
            output=None,
            waiting_reason="FRAMEWORK_AWAITING_RESOLUTION",
        )

    monkeypatch.setattr(case_bootstrap, "execute_frozen_decision_case", waiting_framework_run)

    first = run_default_frozen_decision_case(migrated_settings)
    second = run_default_frozen_decision_case(migrated_settings)

    assert first.publication_status == "CLOSED"
    assert second.publication_status == "CLOSED"
    assert [
        (stage.status, stage.reasons)
        for stage in DecisionLedger.from_settings(migrated_settings).get_stage_results(
            case.business_object_id
        )
        if stage.phase == "FRAMEWORK_RUN"
    ] == [
        ("WAITING", ("FRAMEWORK_AWAITING_RESOLUTION",)),
        ("WAITING", ("FRAMEWORK_AWAITING_RESOLUTION",)),
    ]


@pytest.mark.parametrize("framework_status", ("REJECTED", "FAILED"))
def test_framework_terminal_failures_remain_framework_results(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    framework_status: Literal["REJECTED", "FAILED"],
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def failed_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status=framework_status,
            output=None,
        )

    monkeypatch.setattr(case_bootstrap, "execute_frozen_decision_case", failed_framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == framework_status
    assert outcome.business_result_status is None
    assert outcome.business_lifecycle is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results] == [
        ("FRAMEWORK_RUN", framework_status)
    ]
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


def test_invalid_framework_output_contract_is_saved_and_keeps_publication_closed(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def invalid_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="SUCCEEDED",
            output='{"outcome_code":"SYNTHETIC_REVIEW_COMPLETE"}',
        )

    monkeypatch.setattr(case_bootstrap, "execute_frozen_decision_case", invalid_framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status is None
    assert outcome.business_lifecycle is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "FAILED"),
    ]
    assert outcome.stage_results[-1].reasons == ("OUTPUT_CONTRACT_INVALID",)
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


def test_real_m_agent_output_contract_failure_keeps_its_explicit_reason(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        frozen_adapter,
        "_deterministic_model_response",
        lambda _: '{"outcome_code":"SYNTHETIC_REVIEW_COMPLETE"}',
    )

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "FAILED"
    assert outcome.business_result_status is None
    assert outcome.business_lifecycle is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(stage.phase, stage.status) for stage in outcome.stage_results] == [
        ("FRAMEWORK_RUN", "CREATED"),
        ("FRAMEWORK_RUN", "RUNNING"),
        ("FRAMEWORK_RUN", "FAILED"),
    ]
    assert outcome.stage_results[-1].reasons == ("MODEL_CONTRACT_VIOLATION",)


def test_replay_reuses_the_original_business_object_framework_run_event_and_report(
    migrated_settings: Settings,
) -> None:
    first = run_default_frozen_decision_case(migrated_settings)
    second = run_default_frozen_decision_case(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings)

    assert second == first
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}


def test_report_reads_fail_closed_when_the_stored_payload_breaks_its_event_identity(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    assert execution.report is not None
    engine = create_engine(migrated_settings.app_database_url)
    tampered_report_payload = execution.report.model_dump(mode="json")
    tampered_report_payload["report_version_id"] = "report-version-tampered"
    tampered_report_payload["event_id"] = "decision-event-tampered"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE formal_reports
                SET report_payload = :report_payload
                WHERE report_version_id = :report_version_id
                """
            ),
            {
                "report_payload": json.dumps(
                    tampered_report_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "report_version_id": execution.report_version_id,
            },
        )

    ledger = DecisionLedger.from_settings(migrated_settings)
    with pytest.raises(DecisionEventCommitError, match="stored formal report"):
        ledger.get_formal_report(execution.report_version_id)
    with ledger.serialize_case_execution() as connection:
        with pytest.raises(DecisionEventCommitError, match="stored formal report"):
            ledger.get_original_formal_report(execution.business_object_id, connection)
        with pytest.raises(DecisionEventCommitError, match="stored formal report"):
            ledger.get_formal_report_for_event(execution.decision_event_id, connection)


def test_snapshot_mapping_rejects_a_tampered_frozen_case_before_replay(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    case = load_frozen_decision_case(migrated_settings)
    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        case_payload = connection.execute(
            text(
                """
                SELECT case_payload
                FROM decision_case_business_objects
                WHERE business_object_id = :business_object_id
                """
            ),
            {"business_object_id": case.business_object_id},
        ).scalar_one()
        tampered_payload = json.loads(case_payload)
        tampered_payload["evidence_clock"]["validated_at"] = "2042-05-17T15:19:00Z"
        connection.execute(
            text(
                """
                UPDATE decision_case_business_objects
                SET case_payload = :case_payload
                WHERE business_object_id = :business_object_id
                """
            ),
            {
                "case_payload": json.dumps(
                    tampered_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "business_object_id": case.business_object_id,
            },
        )
        connection.execute(
            text(
                """
                DELETE FROM formal_reports
                WHERE decision_event_id IN (
                    SELECT decision_event_id
                    FROM decision_events
                    WHERE business_object_id = :business_object_id
                )
                """
            ),
            {"business_object_id": case.business_object_id},
        )
        connection.execute(
            text(
                """
                DELETE FROM decision_events
                WHERE business_object_id = :business_object_id
                """
            ),
            {"business_object_id": case.business_object_id},
        )

    with pytest.raises(DecisionEventCommitError, match="business identity maps"):
        run_default_frozen_decision_case(migrated_settings)

    assert execution.report is not None


def test_snapshot_mapping_rejects_a_tampered_framework_identity_before_replay(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    case = load_frozen_decision_case(migrated_settings)
    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE decision_case_business_objects
                SET framework_run_id = 'framework-run-tampered'
                WHERE business_object_id = :business_object_id
                """
            ),
            {"business_object_id": case.business_object_id},
        )
        connection.execute(
            text(
                """
                DELETE FROM formal_reports
                WHERE decision_event_id IN (
                    SELECT decision_event_id
                    FROM decision_events
                    WHERE business_object_id = :business_object_id
                )
                """
            ),
            {"business_object_id": case.business_object_id},
        )
        connection.execute(
            text(
                """
                DELETE FROM decision_events
                WHERE business_object_id = :business_object_id
                """
            ),
            {"business_object_id": case.business_object_id},
        )

    with pytest.raises(DecisionEventCommitError, match="business identity maps"):
        run_default_frozen_decision_case(migrated_settings)

    assert execution.report is not None


def test_concurrent_replay_resolves_to_the_one_committed_identity_set(
    migrated_settings: Settings,
) -> None:
    start_together = Barrier(2)

    def replay() -> object:
        start_together.wait(timeout=5)
        return run_default_frozen_decision_case(migrated_settings)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(replay)
        second_future = executor.submit(replay)
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)

    assert first == second
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_cross_process_replay_resolves_to_the_one_committed_identity_set(
    migrated_settings: Settings,
) -> None:
    context = get_context("fork")
    start_together = context.Barrier(2)
    outcomes = context.Queue()
    processes = tuple(
        context.Process(
            target=_replay_frozen_case_in_child,
            args=(migrated_settings, start_together, outcomes),
        )
        for _ in range(2)
    )

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    recorded = [outcomes.get(timeout=5) for _ in processes]
    assert [outcome[0] for outcome in recorded] == ["OK", "OK"], recorded
    assert recorded[0][1] == recorded[1][1]
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_definite_event_commit_failure_closes_publication_until_the_original_identity_recovers(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)

        failed = run_default_frozen_decision_case(migrated_settings)

    assert failed.framework_run_status == "SUCCEEDED"
    assert failed.business_result_status == "SUCCEEDED"
    assert failed.business_lifecycle is None
    assert failed.business_commit_status == "FAILED"
    assert failed.publication_status == "CLOSED"
    assert failed.report is None
    assert failed.stage_results[-1].phase == "BUSINESS_COMMIT"
    assert failed.stage_results[-1].status == "FAILED"
    assert failed.stage_results[-1].gate_results[0].status == "FAILED"
    assert failed.stage_results[-1].reasons == ("COMMIT_STORAGE_FAILED",)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 0, "reports": 0}
    assert [result.status for result in ledger.get_stage_results(failed.business_object_id)] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
    ]

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.framework_run_id == failed.framework_run_id
    assert recovered.decision_event_id == failed.decision_event_id
    assert recovered.report is not None
    assert recovered.publication_status == "PUBLISHED"
    assert [(stage.phase, stage.status) for stage in recovered.report.stage_results] == [
        ("FRAMEWORK_RUN", "CREATED"),
        ("FRAMEWORK_RUN", "RUNNING"),
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "SUCCEEDED"),
        ("BUSINESS_DECISION", "SUCCEEDED"),
        ("BUSINESS_COMMIT", "FAILED"),
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("BUSINESS_COMMIT", "SUCCEEDED"),
        ("PUBLICATION", "SUCCEEDED"),
    ]
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    assert [result.status for result in ledger.get_stage_results(recovered.business_object_id)] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_cross_build_commit_recovery_resumes_the_original_m_agent_run(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)
        failed = run_default_frozen_decision_case(migrated_settings)

    upgraded_settings = migrated_settings.model_copy(
        update={
            "configuration_version": "0.1.1.dev0",
            "source_sha": "b" * 40,
        }
    )
    recovered = run_default_frozen_decision_case(upgraded_settings)

    assert failed.publication_status == "CLOSED"
    assert recovered.publication_status == "PUBLISHED"
    assert recovered.business_object_id == failed.business_object_id
    assert recovered.framework_run_id == failed.framework_run_id
    assert recovered.report is not None
    assert recovered.report.framework_run_id == failed.framework_run_id
    assert DecisionLedger.from_settings(upgraded_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_cross_build_worker_interruption_resumes_the_original_m_agent_run(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_execute = frozen_adapter.execute_frozen_decision_case

    async def interrupt_after_framework_checkpoint(
        case: FrozenDecisionCase,
        runtime: RuntimeStorage,
        record_transition: frozen_adapter.FrameworkTransitionRecorder | None = None,
    ) -> FrameworkRunResult:
        await original_execute(case, runtime, record_transition)
        raise RuntimeError("synthetic worker interruption after framework checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(
            case_bootstrap, "execute_frozen_decision_case", interrupt_after_framework_checkpoint
        )
        with pytest.raises(RuntimeError, match="worker interruption"):
            run_default_frozen_decision_case(migrated_settings)

    original_case = load_frozen_decision_case(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 0, "reports": 0}

    upgraded_settings = migrated_settings.model_copy(
        update={
            "configuration_version": "0.1.1.dev0",
            "source_sha": "b" * 40,
        }
    )
    recovered = run_default_frozen_decision_case(upgraded_settings)

    assert recovered.business_object_id == original_case.business_object_id
    assert recovered.framework_run_id == original_case.framework_run_id
    assert recovered.decision_event_id == original_case.decision_event_id
    assert recovered.report_version_id == original_case.report_version_id
    assert recovered.publication_status == "PUBLISHED"
    assert recovered.report is not None
    assert recovered.report.stage_results == DecisionLedger.from_settings(
        migrated_settings
    ).get_stage_results(original_case.business_object_id)
    assert [
        stage.status for stage in recovered.stage_results if stage.phase == "FRAMEWORK_RUN"
    ] == ["CREATED", "RUNNING", "SUCCEEDED", "SUCCEEDED"]


def test_unmapped_v1_framework_run_recovers_without_creating_a_v2_replacement(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_current_case = load_frozen_decision_case(migrated_settings)
    legacy_case = legacy_current_case.model_copy(
        update={
            "version_bundle": legacy_current_case.version_bundle.model_copy(
                update={
                    "case_contract_version": "1.0.0",
                    "host_contract_version": "1.0.0",
                    "report_projection_contract_version": "1.0.0",
                }
            )
        }
    )
    runtime = initialize_runtime_storage(migrated_settings)
    framework = asyncio.run(frozen_adapter.execute_frozen_decision_case(legacy_case, runtime))

    assert framework.run_id == legacy_case.framework_run_id
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 0,
        "decision_events": 0,
        "reports": 0,
    }

    observed_run_ids: list[str] = []
    original_get_run = runtime.run_store.get_run

    async def observe_public_run_lookup(run_id: str) -> Any:
        observed_run_ids.append(run_id)
        return await original_get_run(run_id)

    monkeypatch.setattr(runtime.run_store, "get_run", observe_public_run_lookup)
    recovered_legacy_case = asyncio.run(
        frozen_adapter.find_unmapped_legacy_frozen_decision_case(legacy_current_case, runtime)
    )

    assert recovered_legacy_case is not None
    assert recovered_legacy_case.framework_run_id == legacy_case.framework_run_id
    assert observed_run_ids == [legacy_case.framework_run_id]

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.business_object_id == legacy_case.business_object_id
    assert recovered.framework_run_id == legacy_case.framework_run_id
    assert recovered.decision_event_id == legacy_case.decision_event_id
    assert recovered.report_version_id == legacy_case.report_version_id
    with sqlite3.connect(runtime.m_agent_run_store_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)


def test_legacy_mapping_without_a_snapshot_recovers_the_original_durable_run(
    migrated_settings: Settings,
) -> None:
    current_case = load_frozen_decision_case(migrated_settings)
    original_case = current_case.model_copy(
        update={
            "version_bundle": current_case.version_bundle.model_copy(
                update={"report_projection_contract_version": "1.0.0"}
            )
        }
    )
    framework = asyncio.run(
        frozen_adapter.execute_frozen_decision_case(
            original_case,
            initialize_runtime_storage(migrated_settings),
        )
    )
    assert framework.run_id == original_case.framework_run_id

    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    case_payload,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    NULL,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": original_case.business_object_id,
                "case_id": original_case.case_id,
                "frozen_input_fingerprint": original_case.frozen_input_fingerprint,
                "framework_run_id": original_case.framework_run_id,
                "created_at": original_case.report_generated_at,
            },
        )

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.framework_run_id == original_case.framework_run_id
    assert recovered.decision_event_id == original_case.decision_event_id
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_legacy_mapping_without_a_snapshot_rejects_an_unverifiable_build_change(
    migrated_settings: Settings,
) -> None:
    current_case = load_frozen_decision_case(migrated_settings)
    original_case = current_case.model_copy(
        update={
            "version_bundle": current_case.version_bundle.model_copy(
                update={"report_projection_contract_version": "1.0.0"}
            )
        }
    )
    framework = asyncio.run(
        frozen_adapter.execute_frozen_decision_case(
            original_case,
            initialize_runtime_storage(migrated_settings),
        )
    )
    assert framework.run_id == original_case.framework_run_id

    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    case_payload,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    NULL,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": original_case.business_object_id,
                "case_id": original_case.case_id,
                "frozen_input_fingerprint": original_case.frozen_input_fingerprint,
                "framework_run_id": original_case.framework_run_id,
                "created_at": original_case.report_generated_at,
            },
        )

    upgraded_settings = migrated_settings.model_copy(
        update={
            "configuration_version": "0.1.1.dev0",
            "source_sha": "b" * 40,
        }
    )
    with pytest.raises(DecisionEventCommitError, match="lacks a frozen case snapshot"):
        run_default_frozen_decision_case(upgraded_settings)

    assert DecisionLedger.from_settings(upgraded_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


def test_legacy_mapping_without_a_snapshot_rejects_a_mutated_frozen_input(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_case = load_frozen_decision_case(migrated_settings)
    framework = asyncio.run(
        frozen_adapter.execute_frozen_decision_case(
            original_case,
            initialize_runtime_storage(migrated_settings),
        )
    )
    assert framework.run_id == original_case.framework_run_id
    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    case_payload,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    NULL,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": original_case.business_object_id,
                "case_id": original_case.case_id,
                "frozen_input_fingerprint": original_case.frozen_input_fingerprint,
                "framework_run_id": original_case.framework_run_id,
                "created_at": original_case.report_generated_at,
            },
        )
    changed_evidence = [*original_case.input["evidence"]]
    changed_evidence[0] = {
        **changed_evidence[0],
        "statement": "A changed frozen statement must not recover the legacy mapping.",
    }
    mutated_case = original_case.model_copy(
        update={"input": {**original_case.input, "evidence": changed_evidence}}
    )
    monkeypatch.setattr(case_bootstrap, "load_frozen_decision_case", lambda _: mutated_case)

    with pytest.raises(DecisionEventCommitError, match="business identity maps"):
        run_default_frozen_decision_case(migrated_settings)


def test_repeated_business_commit_failures_append_distinct_stage_records(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)
        first = run_default_frozen_decision_case(migrated_settings)
        second = run_default_frozen_decision_case(migrated_settings)

    assert first.publication_status == "CLOSED"
    assert second.publication_status == "CLOSED"
    commit_failures = [
        result
        for result in DecisionLedger.from_settings(migrated_settings).get_stage_results(
            first.business_object_id
        )
        if result.phase == "BUSINESS_COMMIT" and result.status == "FAILED"
    ]
    assert len(commit_failures) == 2
    assert all(result.reasons == ("COMMIT_STORAGE_FAILED",) for result in commit_failures)


def test_uncertain_event_commit_closes_publication_until_the_original_identity_recovers(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitUncertainError("synthetic uncertain commit")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)

        uncertain = run_default_frozen_decision_case(migrated_settings)

    assert uncertain.framework_run_status == "SUCCEEDED"
    assert uncertain.business_result_status == "SUCCEEDED"
    assert uncertain.business_lifecycle is not None
    assert uncertain.business_lifecycle.owner == "COMMIT_RECONCILIATION"
    assert uncertain.business_lifecycle.status == "UNKNOWN"
    assert uncertain.business_commit_status == "UNKNOWN"
    assert uncertain.publication_status == "CLOSED"
    assert uncertain.report is None
    assert uncertain.stage_results[-1].phase == "COMMIT_RECONCILIATION"
    assert uncertain.stage_results[-1].status == "UNKNOWN"
    assert uncertain.stage_results[-1].gate_results[0].status == "UNKNOWN"

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 0, "reports": 0}
    assert [result.status for result in ledger.get_stage_results(uncertain.business_object_id)] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "UNKNOWN",
    ]

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.framework_run_id == uncertain.framework_run_id
    assert recovered.decision_event_id == uncertain.decision_event_id
    assert recovered.report is not None
    assert recovered.business_lifecycle is None
    assert recovered.publication_status == "PUBLISHED"
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    assert [result.status for result in ledger.get_stage_results(recovered.business_object_id)] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "UNKNOWN",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_connection_commit_failure_recovers_a_usable_transaction_before_closing_publication(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings)
    ledger.persist_business_mapping_before_framework(case)
    original_commit = Connection.commit
    original_commit_event = DecisionLedger.commit_event
    event_commit_in_progress = False
    event_commit_attempts = 0

    def lose_first_commit_confirmation(connection: Connection) -> None:
        nonlocal event_commit_attempts
        if event_commit_in_progress:
            event_commit_attempts += 1
        if event_commit_in_progress and event_commit_attempts == 1:
            raise RuntimeError("synthetic commit acknowledgement loss")
        original_commit(connection)

    def mark_event_commit(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        nonlocal event_commit_in_progress
        event_commit_in_progress = True
        try:
            return original_commit_event(
                self,
                connection,
                case=case,
                framework_run_id=framework_run_id,
                result=result,
                stage_results=stage_results,
                decision_event_id=decision_event_id,
                corrects_event_id=corrects_event_id,
                committed_at=committed_at,
                generated_at=generated_at,
            )
        finally:
            event_commit_in_progress = False

    monkeypatch.setattr(
        DecisionLedger,
        "persist_business_mapping_before_framework",
        lambda _self, _case: case.business_object_id,
    )
    monkeypatch.setattr(DecisionLedger, "commit_event", mark_event_commit)
    monkeypatch.setattr(Connection, "commit", lose_first_commit_confirmation)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert event_commit_attempts == 1
    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status == "SUCCEEDED"
    assert outcome.business_commit_status == "UNKNOWN"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }
    assert [stage.status for stage in outcome.stage_results] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "UNKNOWN",
    ]


def test_uncertain_commit_lookup_reuses_the_original_committed_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_commit = DecisionLedger.commit_event

    def commit_then_report_uncertainty(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        original_commit(
            self,
            connection,
            case=case,
            framework_run_id=framework_run_id,
            result=result,
            stage_results=stage_results,
            decision_event_id=decision_event_id,
            corrects_event_id=corrects_event_id,
            committed_at=committed_at,
            generated_at=generated_at,
        )
        raise DecisionEventCommitError("synthetic acknowledgement loss")

    monkeypatch.setattr(DecisionLedger, "commit_event", commit_then_report_uncertainty)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report is not None
    assert outcome.framework_run_id == load_frozen_decision_case(migrated_settings).framework_run_id
    assert (
        outcome.decision_event_id == load_frozen_decision_case(migrated_settings).decision_event_id
    )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_commit_reconciliation_rejects_a_mismatched_original_event(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_commit = DecisionLedger.commit_event

    def commit_mismatched_event_then_fail(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        original_commit(
            self,
            connection,
            case=case,
            framework_run_id=framework_run_id,
            result=result.model_copy(update={"summary": "Mismatched committed result."}),
            stage_results=stage_results,
            decision_event_id=decision_event_id,
            corrects_event_id=corrects_event_id,
            committed_at=committed_at,
            generated_at=generated_at,
        )
        raise DecisionEventCommitError("synthetic mismatched acknowledgement")

    monkeypatch.setattr(DecisionLedger, "commit_event", commit_mismatched_event_then_fail)

    with pytest.raises(DecisionEventCommitError, match="does not match attempted fact"):
        run_default_frozen_decision_case(migrated_settings)

    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 0,
    }


def test_report_projection_failure_preserves_the_unconfirmed_report_but_never_exposes_it(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_publish = DecisionLedger.publish_report

    def write_report_then_lose_confirmation(
        self: DecisionLedger,
        connection: Connection,
        fact: DecisionEventFact,
    ) -> FormalReport:
        original_publish(self, connection, fact)
        raise DecisionEventCommitError("synthetic projection acknowledgement loss")

    with monkeypatch.context() as patch:
        patch.setattr(
            DecisionLedger,
            "publish_report",
            write_report_then_lose_confirmation,
        )
        unpublished = run_default_frozen_decision_case(migrated_settings)

    assert unpublished.framework_run_status == "SUCCEEDED"
    assert unpublished.business_result_status == "SUCCEEDED"
    assert unpublished.business_lifecycle is None
    assert unpublished.business_commit_status == "COMMITTED"
    assert unpublished.publication_status == "CLOSED"
    assert unpublished.report is None
    assert unpublished.stage_results[-1].phase == "PUBLICATION"
    assert unpublished.stage_results[-1].status == "FAILED"
    assert unpublished.stage_results[-1].reasons == ("PUBLICATION_STORAGE_FAILED",)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    assert get_formal_report(unpublished.report_version_id, migrated_settings) is None

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.publication_status == "PUBLISHED"
    assert recovered.report is not None
    assert [
        (stage.status, stage.reasons)
        for stage in recovered.report.stage_results
        if stage.phase == "PUBLICATION"
    ] == [
        ("FAILED", ("PUBLICATION_STORAGE_FAILED",)),
        ("SUCCEEDED", ()),
    ]
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    assert [result.status for result in ledger.get_stage_results(recovered.business_object_id)] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
    ]


def test_report_commit_acknowledgement_loss_reconciles_the_original_report_before_exposure(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_publish = DecisionLedger.publish_report
    original_commit = Connection.commit
    report_commit_started = False
    acknowledgement_lost = False

    def publish_with_report_commit_pending(
        self: DecisionLedger,
        connection: Connection,
        fact: DecisionEventFact,
    ) -> FormalReport:
        nonlocal report_commit_started
        report_commit_started = True
        return original_publish(self, connection, fact)

    def commit_then_lose_report_acknowledgement(self: Connection) -> None:
        nonlocal acknowledgement_lost
        original_commit(self)
        if report_commit_started and not acknowledgement_lost:
            acknowledgement_lost = True
            raise RuntimeError("synthetic report commit acknowledgement loss")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "publish_report", publish_with_report_commit_pending)
        patch.setattr(Connection, "commit", commit_then_lose_report_acknowledgement)
        outcome = run_default_frozen_decision_case(migrated_settings)

    assert acknowledgement_lost is True
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report is not None
    assert get_formal_report(outcome.report_version_id, migrated_settings) == outcome.report
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_unresolved_report_commit_uncertainty_stays_closed_until_the_original_report_recovers(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_report_commit(
        self: DecisionLedger,
        _connection: Connection,
        _fact: DecisionEventFact,
    ) -> FormalReport:
        raise FormalReportCommitUncertainError("synthetic report commit uncertainty")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "publish_report", fail_report_commit)
        uncertain = run_default_frozen_decision_case(migrated_settings)

    assert uncertain.framework_run_status == "SUCCEEDED"
    assert uncertain.business_result_status == "SUCCEEDED"
    assert uncertain.business_lifecycle is None
    assert uncertain.business_commit_status == "COMMITTED"
    assert uncertain.publication_status == "CLOSED"
    assert uncertain.report is None
    assert uncertain.stage_results[-1] == StageResult(
        phase="PUBLICATION",
        status="UNKNOWN",
        gate_results=(
            GateResult(gate_id="EVENT_COMMITTED", status="PASSED"),
            GateResult(gate_id="FORMAL_REPORT_SAVED", status="UNKNOWN"),
        ),
        reasons=("PUBLICATION_COMMIT_UNCERTAIN",),
    )

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.framework_run_id == uncertain.framework_run_id
    assert recovered.decision_event_id == uncertain.decision_event_id
    assert recovered.report_version_id == uncertain.report_version_id
    assert recovered.publication_status == "PUBLISHED"
    assert recovered.report is not None
    assert [
        (stage.status, stage.reasons)
        for stage in recovered.report.stage_results
        if stage.phase == "PUBLICATION"
    ] == [
        ("UNKNOWN", ("PUBLICATION_COMMIT_UNCERTAIN",)),
        ("SUCCEEDED", ()),
    ]


def test_cross_build_publication_failure_keeps_the_committed_fact_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_publication(
        self: DecisionLedger,
        _connection: Connection,
        _fact: DecisionEventFact,
    ) -> FormalReport:
        raise DecisionEventCommitError("synthetic report storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "publish_report", fail_publication)
        initial = run_default_frozen_decision_case(migrated_settings)

    upgraded_settings = migrated_settings.model_copy(update={"source_sha": "b" * 40})
    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "publish_report", fail_publication)
        recovered = run_default_frozen_decision_case(upgraded_settings)

    assert initial.publication_status == "CLOSED"
    assert recovered.publication_status == "CLOSED"
    assert recovered.business_object_id == initial.business_object_id
    assert recovered.framework_run_id == initial.framework_run_id
    assert recovered.decision_event_id == initial.decision_event_id
    assert recovered.report_version_id == initial.report_version_id
    engine = create_engine(upgraded_settings.app_database_url)
    with engine.connect() as connection:
        framework_run_ids = (
            connection.execute(
                text(
                    """
                SELECT DISTINCT framework_run_id
                FROM decision_stage_events
                WHERE decision_event_id = :decision_event_id
                """
                ),
                {"decision_event_id": initial.decision_event_id},
            )
            .scalars()
            .all()
        )
    assert framework_run_ids == [initial.framework_run_id]


def test_notification_failure_and_retry_preserve_the_original_published_report(
    migrated_settings: Settings,
) -> None:
    original_execution = run_default_frozen_decision_case(migrated_settings)
    assert original_execution.report is not None
    case = load_frozen_decision_case(migrated_settings)

    failed = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    repeated_failure = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    recovered = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "SUCCEEDED",
    )

    assert failed.status == "FAILED"
    assert failed.report_version_id == original_execution.report.report_version_id
    assert repeated_failure.status == "FAILED"
    assert repeated_failure.notification_attempt_id != failed.notification_attempt_id
    assert recovered.status == "SUCCEEDED"
    assert recovered.report_version_id == original_execution.report.report_version_id
    assert get_formal_report(original_execution.report.report_version_id, migrated_settings) == (
        original_execution.report
    )
    ledger = DecisionLedger.from_settings(migrated_settings)
    notification_attempts = ledger.get_notification_attempts(case.report_version_id)
    assert [attempt.status for attempt in notification_attempts] == [
        "FAILED",
        "FAILED",
        "SUCCEEDED",
    ]
    assert [
        stage.status
        for stage in ledger.get_stage_results(case.business_object_id)
        if stage.phase == "NOTIFICATION"
    ] == ["FAILED", "FAILED", "SUCCEEDED"]
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}


def test_cross_build_notification_and_correction_reuse_the_original_published_identity(
    migrated_settings: Settings,
) -> None:
    initial = run_default_frozen_decision_case(migrated_settings)
    assert initial.report is not None
    case = load_frozen_decision_case(migrated_settings)
    initial_notification = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    upgraded_settings = migrated_settings.model_copy(update={"source_sha": "b" * 40})

    replayed = run_default_frozen_decision_case(upgraded_settings)
    retried_notification = retry_default_frozen_decision_case_notification(
        upgraded_settings,
        case.business_identity,
        "SUCCEEDED",
    )
    initial_correction = correct_default_frozen_decision_case(
        upgraded_settings,
        case.business_identity,
    )
    replayed_correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert replayed.report == initial.report
    assert replayed.framework_run_id == initial.framework_run_id
    assert replayed.decision_event_id == initial.decision_event_id
    assert replayed.report_version_id == initial.report_version_id
    assert retried_notification.report_version_id == initial.report_version_id
    assert retried_notification.event_id == initial.decision_event_id
    assert (
        retried_notification.notification_attempt_id != initial_notification.notification_attempt_id
    )
    assert replayed_correction == initial_correction
    assert initial_correction.report.framework_run_id == initial.framework_run_id
    assert initial_correction.report.version_bundle.host_source_sha == upgraded_settings.source_sha
    correction_case = load_frozen_decision_case(upgraded_settings)
    assert initial_correction.report.event_id == correction_case.correction_event_id(
        initial.decision_event_id
    )
    assert (
        initial_correction.report.report_version_id
        == correction_case.correction_report_version_id(initial.decision_event_id)
    )


def test_correction_appends_a_new_report_that_references_the_original_event(
    migrated_settings: Settings,
) -> None:
    clock = MutableClock(datetime(2042, 5, 17, 16, 1, tzinfo=UTC))
    original_execution = run_default_frozen_decision_case(migrated_settings, clock=clock)
    assert original_execution.report is not None
    original_report = original_execution.report
    case = load_frozen_decision_case(migrated_settings)

    clock.advance(timedelta(minutes=1))
    correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
        clock=clock,
    )
    replayed_correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
        clock=clock,
    )

    assert replayed_correction == correction
    assert correction.original_event_id == original_report.event_id
    assert correction.report.event_id != original_report.event_id
    assert correction.report.report_version_id != original_report.report_version_id
    assert correction.report.corrects_event_id == original_report.event_id
    assert correction.report.framework_run_id == original_report.framework_run_id
    assert correction.report.knowledge_cutoff == original_report.knowledge_cutoff
    assert correction.report.evidence_clock == original_report.evidence_clock
    assert correction.report.generated_at == "2042-05-17T16:02:00Z"
    assert (
        get_formal_report(original_report.report_version_id, migrated_settings) == original_report
    )
    assert (
        get_formal_report(correction.report.report_version_id, migrated_settings)
        == correction.report
    )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 2,
        "reports": 2,
    }
    engine = create_engine(migrated_settings.app_database_url)
    with engine.connect() as connection:
        publication_event_ids = (
            connection.execute(
                text(
                    """
                SELECT decision_event_id
                FROM decision_stage_events
                WHERE business_object_id = :business_object_id
                  AND json_extract(stage_payload, '$.phase') = 'PUBLICATION'
                ORDER BY sequence
                """
                ),
                {"business_object_id": original_execution.business_object_id},
            )
            .scalars()
            .all()
        )
    assert publication_event_ids == [
        original_report.event_id,
        correction.report.event_id,
    ]


def test_correction_publication_failure_keeps_the_correction_event_and_closes_delivery(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    assert original.report is not None
    case = load_frozen_decision_case(migrated_settings)

    def fail_correction_publication(
        _self: DecisionLedger,
        _connection: Connection,
        fact: DecisionEventFact,
        _report_version_id: str | None = None,
    ) -> FormalReport:
        if fact.corrects_event_id is not None:
            raise DecisionEventCommitError("synthetic correction publication failure")
        return fact.formal_report(
            _report_version_id or fact.case.report_version_id_for_event(fact.decision_event_id)
        )

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "publish_report", fail_correction_publication)
        with pytest.raises(DecisionEventCommitError, match="correction publication"):
            correct_default_frozen_decision_case(
                migrated_settings,
                case.business_identity,
            )

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 2, "reports": 1}
    failed_publications = [
        stage
        for stage in ledger.get_stage_results(case.business_object_id)
        if stage.phase == "PUBLICATION" and stage.status == "FAILED"
    ]
    assert len(failed_publications) == 1
    assert failed_publications[0].reasons == ("PUBLICATION_STORAGE_FAILED",)

    recovered = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert recovered.original_event_id == original.report.event_id
    assert recovered.report.corrects_event_id == original.report.event_id
    assert ledger.counts() == {"business_objects": 1, "decision_events": 2, "reports": 2}


def test_correction_publication_uncertainty_closes_until_the_original_correction_recovers(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    assert original.report is not None
    case = load_frozen_decision_case(migrated_settings)

    def lose_correction_publication_confirmation(
        _self: DecisionLedger,
        _connection: Connection,
        fact: DecisionEventFact,
        _report_version_id: str | None = None,
    ) -> FormalReport:
        if fact.corrects_event_id is not None:
            raise FormalReportCommitUncertainError(
                "synthetic correction publication acknowledgement loss"
            )
        return fact.formal_report(
            _report_version_id or fact.case.report_version_id_for_event(fact.decision_event_id)
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            DecisionLedger,
            "publish_report",
            lose_correction_publication_confirmation,
        )
        with pytest.raises(DecisionEventCommitError, match="correction publication"):
            correct_default_frozen_decision_case(
                migrated_settings,
                case.business_identity,
            )

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 2, "reports": 1}
    unknown_publications = [
        stage
        for stage in ledger.get_stage_results(case.business_object_id)
        if stage.phase == "PUBLICATION" and stage.status == "UNKNOWN"
    ]
    assert len(unknown_publications) == 1
    assert unknown_publications[0].reasons == ("PUBLICATION_COMMIT_UNCERTAIN",)

    recovered = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert recovered.original_event_id == original.report.event_id
    assert recovered.report.corrects_event_id == original.report.event_id
    assert [
        (stage.status, stage.reasons)
        for stage in recovered.report.stage_results
        if stage.phase == "PUBLICATION"
    ] == [
        ("UNKNOWN", ("PUBLICATION_COMMIT_UNCERTAIN",)),
        ("SUCCEEDED", ()),
    ]
    assert ledger.counts() == {"business_objects": 1, "decision_events": 2, "reports": 2}


def test_correction_commit_failure_retains_its_append_only_failure_evidence(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    assert original.report is not None
    case = load_frozen_decision_case(migrated_settings)
    original_commit_event = DecisionLedger.commit_event

    def fail_correction_commit(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        if corrects_event_id is not None:
            raise DecisionEventCommitError("synthetic correction storage failure")
        return original_commit_event(
            self,
            connection,
            case=case,
            framework_run_id=framework_run_id,
            result=result,
            stage_results=stage_results,
            decision_event_id=decision_event_id,
            corrects_event_id=corrects_event_id,
            committed_at=committed_at,
            generated_at=generated_at,
        )

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_correction_commit)
        with pytest.raises(DecisionEventCommitError, match="correction commit"):
            correct_default_frozen_decision_case(
                migrated_settings,
                case.business_identity,
            )

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    correction_stages = [
        stage
        for stage in ledger.get_stage_results(case.business_object_id)
        if stage.phase in {"CORRECTION", "BUSINESS_COMMIT"}
    ]
    assert [(stage.phase, stage.status) for stage in correction_stages[-1:]] == [
        ("BUSINESS_COMMIT", "FAILED"),
    ]

    recovered = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert recovered.original_event_id == original.report.event_id
    assert ledger.counts() == {"business_objects": 1, "decision_events": 2, "reports": 2}
    assert [
        (stage.phase, stage.status)
        for stage in recovered.report.stage_results
        if stage.phase in {"CORRECTION", "BUSINESS_COMMIT"}
    ] == [
        ("BUSINESS_COMMIT", "FAILED"),
        ("CORRECTION", "SUCCEEDED"),
        ("BUSINESS_COMMIT", "SUCCEEDED"),
    ]


@pytest.mark.parametrize(
    "commit_error",
    (DecisionEventCommitError, DecisionEventCommitUncertainError),
)
def test_cross_build_correction_recovery_reuses_the_first_attempt_identity(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    commit_error: type[DecisionEventCommitError],
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    assert original.report is not None
    case = load_frozen_decision_case(migrated_settings)
    attempted_correction_event_ids: list[str] = []
    original_commit_event = DecisionLedger.commit_event

    def fail_correction_commit(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        if corrects_event_id is not None:
            assert decision_event_id is not None
            attempted_correction_event_ids.append(decision_event_id)
            raise commit_error("synthetic correction commit interruption")
        return original_commit_event(
            self,
            connection,
            case=case,
            framework_run_id=framework_run_id,
            result=result,
            stage_results=stage_results,
            decision_event_id=decision_event_id,
            corrects_event_id=corrects_event_id,
            committed_at=committed_at,
            generated_at=generated_at,
        )

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_correction_commit)
        with pytest.raises(DecisionEventCommitError, match="correction commit"):
            correct_default_frozen_decision_case(
                migrated_settings,
                case.business_identity,
            )

    upgraded_settings = migrated_settings.model_copy(
        update={
            "configuration_version": "synthetic-config-v2",
            "source_sha": "b" * 40,
        }
    )
    recovered = correct_default_frozen_decision_case(
        upgraded_settings,
        case.business_identity,
    )

    assert attempted_correction_event_ids == [recovered.report.event_id]
    assert recovered.report.event_id == case.correction_event_id(original.report.event_id)
    assert recovered.report.report_version_id == case.correction_report_version_id(
        original.report.event_id
    )


def test_correction_recovery_keeps_the_first_identity_across_semantic_bundle_changes(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    assert original.report is not None
    case = load_frozen_decision_case(migrated_settings)
    original_commit_event = DecisionLedger.commit_event
    attempted_correction_event_ids: list[str] = []

    def fail_correction_commit(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        if corrects_event_id is not None:
            assert decision_event_id is not None
            attempted_correction_event_ids.append(decision_event_id)
            raise DecisionEventCommitUncertainError("synthetic correction commit interruption")
        return original_commit_event(
            self,
            connection,
            case=case,
            framework_run_id=framework_run_id,
            result=result,
            stage_results=stage_results,
            decision_event_id=decision_event_id,
            corrects_event_id=corrects_event_id,
            committed_at=committed_at,
            generated_at=generated_at,
        )

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_correction_commit)
        with pytest.raises(DecisionEventCommitError, match="correction commit"):
            correct_default_frozen_decision_case(
                migrated_settings,
                case.business_identity,
            )

    changed_bundle = case.version_bundle.model_copy(
        update={
            "model_adapter_id": "d0-replacement-model-adapter",
            "routing_policy_version": "d0-replacement-routing-policy",
        }
    )
    changed_case = case.model_copy(
        update={
            "version_bundle": changed_bundle,
            "agent_definition": case.agent_definition.model_copy(
                update={"model_adapter_id": changed_bundle.model_adapter_id}
            ),
        }
    )
    monkeypatch.setattr(case_bootstrap, "load_frozen_decision_case", lambda _: changed_case)

    recovered = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert attempted_correction_event_ids == [recovered.report.event_id]
    assert recovered.report.event_id == case.correction_event_id(original.report.event_id)
    assert recovered.report.report_version_id == case.correction_report_version_id(
        original.report.event_id
    )
    assert recovered.report.version_bundle == case.version_bundle


def test_correction_replay_uses_its_persisted_report_identity_after_a_projection_change(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    assert original.report is not None
    case = load_frozen_decision_case(migrated_settings)
    correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    monkeypatch.setattr(
        decision_domain,
        "FROZEN_REPORT_PROJECTION_CONTRACT_VERSION",
        "3.0.0",
    )
    replayed = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert replayed == correction
    assert replayed.report.report_version_id == correction.report.report_version_id


@pytest.mark.parametrize(
    ("fixture_name", "expected_reason"),
    (
        (
            "input-rejected",
            "The scenario's D0 host-input gate rejects the requested decision.",
        ),
        (
            "result-abstained",
            "The scenario has no eligible synthetic action to record.",
        ),
        (
            "result-failed",
            "The scenario injects a deterministic business-stage fault.",
        ),
        (
            "result-pending",
            "The scenario leaves the adjudication gate unresolved.",
        ),
        (
            "result-expired",
            "The scenario's synthetic validity deadline has passed.",
        ),
        (
            "result-execution-blocked",
            "The scenario's synthetic host execution gate is unavailable.",
        ),
        (
            "result-unknown",
            "The scenario withholds a resolved adjudication outcome.",
        ),
    ),
)
def test_result_family_output_reasons_name_the_synthetic_cause(
    fixture_name: str,
    expected_reason: str,
) -> None:
    case = _load_result_family_fixture(fixture_name)
    result = ExternalResult.model_validate_json(frozen_adapter._deterministic_model_response(case))

    assert result.key_reasons == (expected_reason,)
    assert case.expected_external_result.key_reasons == (expected_reason,)


@pytest.mark.parametrize(
    ("fixture_name", "failure_boundary"),
    (
        ("result-pending", "BUSINESS_COMMIT"),
        ("result-expired", "PUBLICATION"),
        ("result-execution-blocked", "PUBLICATION"),
        ("result-unknown", "BUSINESS_COMMIT"),
    ),
)
def test_lifecycle_owner_survives_closed_commit_and_publication_boundaries(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    failure_boundary: Literal["BUSINESS_COMMIT", "PUBLICATION"],
) -> None:
    case = _load_result_family_fixture(fixture_name)
    monkeypatch.setattr(case_bootstrap, "load_frozen_decision_case", lambda _: case)

    if failure_boundary == "BUSINESS_COMMIT":

        def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
            raise DecisionEventCommitError("synthetic event storage failure")

        monkeypatch.setattr(DecisionLedger, "commit_event", fail_commit)
    else:

        def fail_publication(
            self: DecisionLedger,
            _connection: object,
            _fact: object,
            _report_version_id: str | None = None,
        ) -> FormalReport:
            raise DecisionEventCommitError("synthetic report storage failure")

        monkeypatch.setattr(DecisionLedger, "publish_report", fail_publication)

    closed = run_default_frozen_decision_case(migrated_settings)
    expected_stage = next(
        stage
        for stage in closed.stage_results
        if stage.phase
        in {
            "ADJUDICATION_LIFECYCLE",
            "VALIDITY_LIFECYCLE",
            "EXECUTION_LIFECYCLE",
        }
    )

    assert closed.publication_status == "CLOSED"
    assert closed.business_lifecycle is not None
    assert (
        closed.business_lifecycle.owner
        == {
            "ADJUDICATION_LIFECYCLE": "ADJUDICATION",
            "VALIDITY_LIFECYCLE": "VALIDITY",
            "EXECUTION_LIFECYCLE": "EXECUTION",
        }[expected_stage.phase]
    )
    assert closed.business_lifecycle.phase == expected_stage.phase
    assert closed.business_lifecycle.status == expected_stage.status


def test_read_failure_does_not_remove_the_published_report(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_execution = run_default_frozen_decision_case(migrated_settings)
    assert original_execution.report is not None
    original_report = original_execution.report

    with monkeypatch.context() as patch:
        patch.setattr(
            DecisionLedger,
            "get_formal_report",
            lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic read failure")),
        )
        with pytest.raises(RuntimeError, match="synthetic read failure"):
            get_formal_report(original_report.report_version_id, migrated_settings)

    assert (
        get_formal_report(original_report.report_version_id, migrated_settings) == original_report
    )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_stage_result_migration_refuses_to_drop_append_only_history(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_stage_events (
                    stage_event_id,
                    business_object_id,
                    framework_run_id,
                    decision_event_id,
                    stage_payload,
                    recorded_at
                ) VALUES (
                    'stage-event-synthetic',
                    'business-object-synthetic',
                    'framework-run-synthetic',
                    NULL,
                    '{}',
                    '2042-05-17T16:01:00Z'
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="append-only stage results"):
        command.downgrade(config, "0002_decision_case_ledger")

    load_settings.cache_clear()


def test_stage_result_migration_retries_after_table_creation_is_interrupted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0002_decision_case_ledger")
    original_create_table: Any = op.create_table
    interrupted = False

    def create_stage_table_then_interrupt(name: str, *columns: Any, **kwargs: Any) -> Any:
        nonlocal interrupted
        table = original_create_table(name, *columns, **kwargs)
        if name == "decision_stage_events" and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic interruption after stage table creation")
        return table

    with monkeypatch.context() as patch:
        patch.setattr(op, "create_table", create_stage_table_then_interrupt)
        with pytest.raises(RuntimeError, match="stage table creation"):
            command.upgrade(config, "0003_decision_stage_events")

    engine = create_engine(settings.app_database_url)
    with engine.connect() as connection:
        stage_table_exists = (
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'decision_stage_events'"
                )
            ).scalar_one_or_none()
            is not None
        )
    if not stage_table_exists:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE decision_stage_events (
                        sequence INTEGER NOT NULL PRIMARY KEY,
                        stage_event_id VARCHAR(96) NOT NULL UNIQUE,
                        business_object_id VARCHAR(96) NOT NULL,
                        framework_run_id VARCHAR(96) NOT NULL,
                        decision_event_id VARCHAR(96),
                        stage_payload VARCHAR NOT NULL,
                        recorded_at VARCHAR(40) NOT NULL
                    )
                    """
                )
            )

    command.upgrade(config, "0003_decision_stage_events")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_decision_stage_events"
        )

    load_settings.cache_clear()


def test_stage_result_migration_rejects_a_table_without_append_only_identities(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0002_decision_case_ledger")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_stage_events (
                    sequence INTEGER NOT NULL,
                    stage_event_id VARCHAR(96) NOT NULL,
                    business_object_id VARCHAR(96) NOT NULL,
                    framework_run_id VARCHAR(96) NOT NULL,
                    decision_event_id VARCHAR(96),
                    stage_payload VARCHAR NOT NULL,
                    recorded_at VARCHAR(40) NOT NULL
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="incompatible table"):
        command.upgrade(config, "0003_decision_stage_events")

    load_settings.cache_clear()


def test_stage_result_migration_rejects_a_table_without_a_sequence_column(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0002_decision_case_ledger")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_stage_events (
                    stage_event_id VARCHAR(96) NOT NULL UNIQUE,
                    business_object_id VARCHAR(96) NOT NULL,
                    framework_run_id VARCHAR(96) NOT NULL,
                    decision_event_id VARCHAR(96),
                    stage_payload VARCHAR NOT NULL,
                    recorded_at VARCHAR(40) NOT NULL
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="incompatible table"):
        command.upgrade(config, "0003_decision_stage_events")

    load_settings.cache_clear()


def test_stage_result_migration_rejects_an_incompatible_nullable_column(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0002_decision_case_ledger")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_stage_events (
                    sequence INTEGER PRIMARY KEY,
                    stage_event_id VARCHAR(96) NOT NULL UNIQUE,
                    business_object_id VARCHAR(96) NOT NULL,
                    framework_run_id VARCHAR(96) NOT NULL,
                    decision_event_id VARCHAR(96) NOT NULL,
                    stage_payload VARCHAR NOT NULL,
                    recorded_at VARCHAR(40) NOT NULL
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="incompatible table"):
        command.upgrade(config, "0003_decision_stage_events")

    load_settings.cache_clear()


def test_stage_result_migration_rejects_an_extra_unique_constraint(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0002_decision_case_ledger")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_stage_events (
                    sequence INTEGER NOT NULL PRIMARY KEY,
                    stage_event_id VARCHAR(96) NOT NULL UNIQUE,
                    business_object_id VARCHAR(96) NOT NULL UNIQUE,
                    framework_run_id VARCHAR(96) NOT NULL,
                    decision_event_id VARCHAR(96),
                    stage_payload VARCHAR NOT NULL,
                    recorded_at VARCHAR(40) NOT NULL
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="incompatible table"):
        command.upgrade(config, "0003_decision_stage_events")

    load_settings.cache_clear()


def test_snapshot_migration_retries_after_column_addition_is_interrupted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0004_corrections_and_notification_attempts")
    original_add_column: Any = op.add_column
    interrupted = False

    def add_snapshot_column_then_interrupt(*args: Any, **kwargs: Any) -> Any:
        nonlocal interrupted
        result = original_add_column(*args, **kwargs)
        if (
            args[0] == "decision_case_business_objects"
            and getattr(args[1], "name", None) == "case_payload"
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError("synthetic interruption after snapshot column addition")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(op, "add_column", add_snapshot_column_then_interrupt)
        with pytest.raises(RuntimeError, match="snapshot column addition"):
            command.upgrade(config, "0005_persist_frozen_case_snapshots")

    engine = create_engine(settings.app_database_url)
    with engine.connect() as connection:
        snapshot_column_exists = "case_payload" in {
            row.name
            for row in connection.execute(text("PRAGMA table_info(decision_case_business_objects)"))
        }
    if not snapshot_column_exists:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE decision_case_business_objects ADD COLUMN case_payload VARCHAR")
            )

    command.upgrade(config, "0005_persist_frozen_case_snapshots")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0005_persist_frozen_case_snapshots"
        )

    load_settings.cache_clear()


def test_snapshot_migration_rejects_a_nullable_nonstring_payload_column(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0004_corrections_and_notification_attempts")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE decision_case_business_objects "
                "ADD COLUMN case_payload INTEGER DEFAULT 0"
            )
        )

    with pytest.raises(RuntimeError, match="snapshot migration has incompatible column"):
        command.upgrade(config, "0005_persist_frozen_case_snapshots")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0004_corrections_and_notification_attempts"
        )

    load_settings.cache_clear()


def test_correction_migration_downgrade_retries_from_a_replacement_only_state(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0004_corrections_and_notification_attempts")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_events_replacement (
                    decision_event_id VARCHAR(96) PRIMARY KEY,
                    business_object_id VARCHAR(96) NOT NULL,
                    framework_run_id VARCHAR(96) NOT NULL UNIQUE,
                    event_payload VARCHAR NOT NULL,
                    committed_at VARCHAR(40) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
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
        )
        connection.execute(text("DROP TABLE decision_events"))
        connection.execute(text("DROP TABLE decision_notification_attempts"))

    command.downgrade(config, "0003_decision_stage_events")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_decision_stage_events"
        )
        assert "corrects_event_id" not in {
            row.name for row in connection.execute(text("PRAGMA table_info(decision_events)"))
        }
        assert (
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'decision_notification_attempts'"
                )
            ).scalar_one_or_none()
            is None
        )

    load_settings.cache_clear()


def test_correction_migration_preflights_legacy_payloads_before_replacing_event_table(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    current_case = load_frozen_decision_case(settings)
    case = current_case.model_copy(
        update={
            "version_bundle": current_case.version_bundle.model_copy(
                update={"report_projection_contract_version": "1.0.0"}
            )
        }
    )
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": case.business_object_id,
                "case_id": case.case_id,
                "frozen_input_fingerprint": case.frozen_input_fingerprint,
                "framework_run_id": case.framework_run_id,
                "created_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decision_events (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    '{invalid-json',
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": case.decision_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "committed_at": case.report_generated_at,
            },
        )

    with pytest.raises(RuntimeError, match="valid JSON"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'decision_events_replacement'"
                )
            ).scalar_one_or_none()
            is None
        )
        assert "corrects_event_id" not in {
            row.name for row in connection.execute(text("PRAGMA table_info(decision_events)"))
        }
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_decision_stage_events"
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE decision_events
                SET event_payload = :event_payload
                WHERE decision_event_id = :decision_event_id
                """
            ),
            {
                "event_payload": json.dumps(
                    {
                        "decision_event_id": case.decision_event_id,
                        "business_object_id": case.business_object_id,
                        "framework_run_id": case.framework_run_id,
                        "case": case.model_dump(mode="json"),
                        "result": case.expected_external_result.model_dump(mode="json"),
                        "validation_status": "PASSED",
                        "committed_at": case.report_generated_at,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "decision_event_id": case.decision_event_id,
            },
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert "corrects_event_id" in {
            row.name for row in connection.execute(text("PRAGMA table_info(decision_events)"))
        }
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0005_persist_frozen_case_snapshots"
        )

    load_settings.cache_clear()


def test_correction_migration_rejects_a_valid_event_with_an_invalid_stage_contract(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    case = load_frozen_decision_case(settings)
    event_payload = {
        "decision_event_id": case.decision_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case": case.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
        "validation_status": "PASSED",
        "committed_at": case.report_generated_at,
        "stage_results": [{}],
    }
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_events (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    :event_payload,
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": case.decision_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "event_payload": json.dumps(
                    event_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "committed_at": case.report_generated_at,
            },
        )

    with pytest.raises(RuntimeError, match="stage contract"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'decision_events_replacement'"
                )
            ).scalar_one_or_none()
            is None
        )
        assert "corrects_event_id" not in {
            row.name for row in connection.execute(text("PRAGMA table_info(decision_events)"))
        }
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_decision_stage_events"
        )

    load_settings.cache_clear()


def test_correction_migration_rejects_an_event_without_a_confirmed_business_commit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    case = load_frozen_decision_case(settings)
    event_payload = {
        "decision_event_id": case.decision_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case": case.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
        "validation_status": "PASSED",
        "committed_at": case.report_generated_at,
        "stage_results": [
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
                "reasons": [],
            },
            {
                "phase": "BUSINESS_DECISION",
                "status": "SUCCEEDED",
                "gate_results": [{"gate_id": "DECISION_DETERMINED", "status": "PASSED"}],
                "reasons": [],
            },
            {
                "phase": "BUSINESS_COMMIT",
                "status": "FAILED",
                "gate_results": [{"gate_id": "HOST_RESULT_SAVED", "status": "FAILED"}],
                "reasons": ["COMMIT_STORAGE_FAILED"],
            },
        ],
    }
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": case.business_object_id,
                "case_id": case.case_id,
                "frozen_input_fingerprint": case.frozen_input_fingerprint,
                "framework_run_id": case.framework_run_id,
                "created_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decision_events (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    :event_payload,
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": case.decision_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "event_payload": json.dumps(
                    event_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "committed_at": case.report_generated_at,
            },
        )

    with pytest.raises(RuntimeError, match="confirmed business commit"):
        command.upgrade(config, "head")

    load_settings.cache_clear()


def test_correction_migration_rejects_a_report_with_mismatched_durable_identities(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    current_case = load_frozen_decision_case(settings)
    case = current_case.model_copy(
        update={
            "version_bundle": current_case.version_bundle.model_copy(
                update={"report_projection_contract_version": "1.0.0"}
            )
        }
    )
    event_payload = {
        "decision_event_id": case.decision_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case": case.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
        "validation_status": "PASSED",
        "committed_at": case.report_generated_at,
    }
    report_payload = {
        "report_version_id": "report-version-mismatched-legacy-payload",
        "event_id": case.decision_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case_id": case.case_id,
        "synthetic": True,
        "qualification_scope": case.qualification_scope,
        "generated_at": case.report_generated_at,
        "knowledge_cutoff": case.knowledge_cutoff,
        "evidence_clock": case.evidence_clock.model_dump(mode="json"),
        "version_bundle": case.version_bundle.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
    }
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": case.business_object_id,
                "case_id": case.case_id,
                "frozen_input_fingerprint": case.frozen_input_fingerprint,
                "framework_run_id": case.framework_run_id,
                "created_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decision_events (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    :event_payload,
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": case.decision_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "event_payload": json.dumps(
                    event_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "committed_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO formal_reports (
                    report_version_id,
                    decision_event_id,
                    report_payload,
                    generated_at
                ) VALUES (
                    :report_version_id,
                    :decision_event_id,
                    :report_payload,
                    :generated_at
                )
                """
            ),
            {
                "report_version_id": report_payload["report_version_id"],
                "decision_event_id": case.decision_event_id,
                "report_payload": json.dumps(
                    report_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "generated_at": case.report_generated_at,
            },
        )

    with pytest.raises(RuntimeError, match="formal report identity"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert "corrects_event_id" not in {
            row.name for row in connection.execute(text("PRAGMA table_info(decision_events)"))
        }
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_decision_stage_events"
        )

    load_settings.cache_clear()


def test_correction_migration_retries_after_notification_table_creation_is_interrupted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    original_create_table: Any = op.create_table
    interrupted = False

    def create_notification_table_then_interrupt(name: str, *columns: Any, **kwargs: Any) -> Any:
        nonlocal interrupted
        table = original_create_table(name, *columns, **kwargs)
        if name == "decision_notification_attempts" and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic interruption after notification table creation")
        return table

    with monkeypatch.context() as patch:
        patch.setattr(op, "create_table", create_notification_table_then_interrupt)
        with pytest.raises(RuntimeError, match="notification table creation"):
            command.upgrade(config, "head")

    engine = create_engine(settings.app_database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_decision_stage_events"
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_notification_attempts (
                    sequence INTEGER NOT NULL PRIMARY KEY,
                    notification_attempt_id VARCHAR(96) NOT NULL UNIQUE,
                    report_version_id VARCHAR(96) NOT NULL,
                    decision_event_id VARCHAR(96) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    reasons_payload VARCHAR NOT NULL,
                    recorded_at VARCHAR(40) NOT NULL
                )
                """
            )
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0005_persist_frozen_case_snapshots"
        )

    load_settings.cache_clear()


def test_correction_migration_rebuilds_an_event_table_with_legacy_run_uniqueness(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE decision_events ADD COLUMN corrects_event_id VARCHAR(96)")
        )

    command.upgrade(config, "head")
    original = run_default_frozen_decision_case(settings)
    case = load_frozen_decision_case(settings)
    correction = correct_default_frozen_decision_case(settings, case.business_identity)

    assert original.report is not None
    assert correction.report.framework_run_id == original.framework_run_id

    load_settings.cache_clear()


def test_correction_migration_rejects_an_incomplete_notification_attempts_table(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_notification_attempts (
                    sequence INTEGER PRIMARY KEY,
                    notification_attempt_id VARCHAR(96) NOT NULL,
                    report_version_id VARCHAR(96) NOT NULL,
                    decision_event_id VARCHAR(96) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    reasons_payload VARCHAR NOT NULL,
                    recorded_at VARCHAR(40) NOT NULL
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="notification attempts table has unexpected schema"):
        command.upgrade(config, "head")

    load_settings.cache_clear()


def test_correction_migration_rejects_semantically_incompatible_notification_attempts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_notification_attempts (
                    sequence INTEGER PRIMARY KEY,
                    notification_attempt_id VARCHAR(96) NOT NULL UNIQUE,
                    report_version_id VARCHAR(96) NOT NULL,
                    decision_event_id VARCHAR(96) NOT NULL,
                    status VARCHAR(16),
                    reasons_payload INTEGER NOT NULL DEFAULT 0,
                    recorded_at VARCHAR(40) NOT NULL
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="notification attempts table has unexpected schema"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_decision_stage_events"
        )

    load_settings.cache_clear()


def test_correction_migration_preflights_a_replacement_table_before_repairing_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    case = load_frozen_decision_case(settings)
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE decision_events_replacement (
                    decision_event_id VARCHAR(96) PRIMARY KEY,
                    business_object_id VARCHAR(96) NOT NULL,
                    framework_run_id VARCHAR(96) NOT NULL,
                    corrects_event_id VARCHAR(96),
                    event_payload VARCHAR NOT NULL,
                    committed_at VARCHAR(40) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO decision_events_replacement (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    corrects_event_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    NULL,
                    '{invalid-json',
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": case.decision_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "committed_at": case.report_generated_at,
            },
        )
        connection.execute(text("DROP TABLE decision_events"))

    with pytest.raises(RuntimeError, match="valid JSON"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'decision_events'"
                )
            ).scalar_one_or_none()
            is None
        )
        assert (
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'decision_events_replacement'"
                )
            ).scalar_one()
            == "decision_events_replacement"
        )

    load_settings.cache_clear()


def test_correction_migration_rejects_a_nested_case_that_breaks_the_mapping_lineage(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    current_case = load_frozen_decision_case(settings)
    case = current_case.model_copy(
        update={
            "version_bundle": current_case.version_bundle.model_copy(
                update={"report_projection_contract_version": "1.0.0"}
            )
        }
    )
    nested_case = case.model_copy(
        update={"business_identity": "synthetic:decision:unrelated-legacy-lineage"}
    )
    event_payload = {
        "decision_event_id": case.decision_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case": nested_case.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
        "validation_status": "PASSED",
        "committed_at": case.report_generated_at,
    }
    event = DecisionEventFact.model_validate(event_payload)
    report = event.formal_report(case.report_version_id)
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": case.business_object_id,
                "case_id": case.case_id,
                "frozen_input_fingerprint": case.frozen_input_fingerprint,
                "framework_run_id": case.framework_run_id,
                "created_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decision_events (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    :event_payload,
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": case.decision_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "event_payload": event.model_dump_json(),
                "committed_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO formal_reports (
                    report_version_id,
                    decision_event_id,
                    report_payload,
                    generated_at
                ) VALUES (
                    :report_version_id,
                    :decision_event_id,
                    :report_payload,
                    :generated_at
                )
                """
            ),
            {
                "report_version_id": case.report_version_id,
                "decision_event_id": case.decision_event_id,
                "report_payload": report.model_dump_json(),
                "generated_at": case.report_generated_at,
            },
        )

    with pytest.raises(RuntimeError, match="legacy decision event identity"):
        command.upgrade(config, "head")

    load_settings.cache_clear()


def test_upgrade_of_a_populated_0002_ledger_preserves_replayable_facts_and_reports(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_settings = settings.model_copy(update={"source_sha": "b" * 40})
    current_case = load_frozen_decision_case(legacy_settings)
    case = current_case.model_copy(
        update={
            "version_bundle": current_case.version_bundle.model_copy(
                update={
                    "case_contract_version": "1.0.0",
                    "host_contract_version": "1.0.0",
                    "report_projection_contract_version": "1.0.0",
                }
            )
        }
    )
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0002_decision_case_ledger")
    legacy_event_id = case.legacy_decision_event_id_for_framework_run(case.framework_run_id)
    legacy_report_version_id = case.report_version_id_for_event(legacy_event_id)

    legacy_event_payload = {
        "decision_event_id": legacy_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case": case.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
        "validation_status": "PASSED",
        "committed_at": case.report_generated_at,
    }
    legacy_report_payload = {
        "report_version_id": legacy_report_version_id,
        "event_id": legacy_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case_id": case.case_id,
        "synthetic": True,
        "qualification_scope": case.qualification_scope,
        "generated_at": case.report_generated_at,
        "knowledge_cutoff": case.knowledge_cutoff,
        "evidence_clock": case.evidence_clock.model_dump(mode="json"),
        "version_bundle": case.version_bundle.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
    }
    legacy_event_payload_json = json.dumps(
        legacy_event_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy_report_payload_json = json.dumps(
        legacy_report_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": case.business_object_id,
                "case_id": case.case_id,
                "frozen_input_fingerprint": case.frozen_input_fingerprint,
                "framework_run_id": case.framework_run_id,
                "created_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decision_events (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    :event_payload,
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": legacy_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "event_payload": legacy_event_payload_json,
                "committed_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO formal_reports (
                    report_version_id,
                    decision_event_id,
                    report_payload,
                    generated_at
                ) VALUES (
                    :report_version_id,
                    :decision_event_id,
                    :report_payload,
                    :generated_at
                )
                """
            ),
            {
                "report_version_id": legacy_report_version_id,
                "decision_event_id": legacy_event_id,
                "report_payload": legacy_report_payload_json,
                "generated_at": case.report_generated_at,
            },
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT event_payload FROM decision_events "
                    "WHERE decision_event_id = :decision_event_id"
                ),
                {"decision_event_id": legacy_event_id},
            ).scalar_one()
            == legacy_event_payload_json
        )
        assert (
            connection.execute(
                text(
                    "SELECT report_payload FROM formal_reports "
                    "WHERE report_version_id = :report_version_id"
                ),
                {"report_version_id": legacy_report_version_id},
            ).scalar_one()
            == legacy_report_payload_json
        )

    ledger = DecisionLedger.from_settings(settings)
    report = ledger.get_formal_report(legacy_report_version_id)

    assert report is not None
    assert [(stage.phase, stage.status) for stage in report.stage_results] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "SUCCEEDED"),
        ("BUSINESS_DECISION", "SUCCEEDED"),
        ("BUSINESS_COMMIT", "SUCCEEDED"),
        ("PUBLICATION", "SUCCEEDED"),
    ]
    assert [
        (stage.phase, stage.status) for stage in ledger.get_stage_results(case.business_object_id)
    ] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "SUCCEEDED"),
        ("BUSINESS_DECISION", "SUCCEEDED"),
        ("BUSINESS_COMMIT", "SUCCEEDED"),
        ("PUBLICATION", "SUCCEEDED"),
    ]
    replayed = run_default_frozen_decision_case(settings)
    assert replayed.report == report
    assert replayed.framework_run_id == case.framework_run_id
    assert replayed.decision_event_id == legacy_event_id
    assert replayed.report_version_id == legacy_report_version_id
    assert replayed.business_result_status == "SUCCEEDED"
    assert ledger.counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }
    notification = retry_default_frozen_decision_case_notification(
        settings,
        current_case.business_identity,
        "FAILED",
    )
    assert notification.report_version_id == legacy_report_version_id
    assert notification.event_id == legacy_event_id
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM formal_reports WHERE report_version_id = :report_version_id"),
            {"report_version_id": legacy_report_version_id},
        )

    recovered_without_projection = run_default_frozen_decision_case(settings)
    assert recovered_without_projection.report == report
    assert recovered_without_projection.framework_run_id == case.framework_run_id
    assert recovered_without_projection.decision_event_id == legacy_event_id
    assert recovered_without_projection.report_version_id == legacy_report_version_id

    correction = correct_default_frozen_decision_case(
        settings,
        current_case.business_identity,
    )
    assert correction.original_event_id == legacy_event_id
    assert correction.report.version_bundle.report_projection_contract_version == "2.0.0"
    assert correction.report.event_id == case.correction_event_id(legacy_event_id)
    assert correction.report.report_version_id == case.correction_report_version_id(legacy_event_id)

    load_settings.cache_clear()


def test_notification_migration_refuses_to_drop_append_only_history(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    case = load_frozen_decision_case(migrated_settings)
    retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE decision_case_business_objects SET case_payload = NULL"))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", migrated_settings.app_database_url)

    with pytest.raises(RuntimeError, match="append-only notification attempts"):
        command.downgrade(config, "0003_decision_stage_events")

    assert execution.report is not None


def test_frozen_case_snapshot_migration_refuses_to_drop_recovery_state(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", migrated_settings.app_database_url)

    with pytest.raises(RuntimeError, match="frozen case snapshots"):
        command.downgrade(config, "0004_corrections_and_notification_attempts")

    assert execution.report is not None


def test_recovery_backfills_event_stage_results_before_publication(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_record = DecisionLedger.record_stage_result

    def crash_after_event_commit(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        stage_result: StageResult,
        decision_event_id: str | None = None,
        stage_event_id: str | None = None,
        framework_run_id: str | None = None,
        allow_repeated_occurrence: bool = False,
    ) -> None:
        if stage_result.phase == "BUSINESS_COMMIT" and decision_event_id is not None:
            raise RuntimeError("synthetic crash after event commit")
        original_record(
            self,
            connection,
            case=case,
            stage_result=stage_result,
            decision_event_id=decision_event_id,
            stage_event_id=stage_event_id,
            framework_run_id=framework_run_id,
            allow_repeated_occurrence=allow_repeated_occurrence,
        )

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "record_stage_result", crash_after_event_commit)
        with pytest.raises(RuntimeError, match="synthetic crash after event commit"):
            run_default_frozen_decision_case(migrated_settings)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 0}

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.publication_status == "PUBLISHED"
    assert [stage.status for stage in ledger.get_stage_results(recovered.business_object_id)] == [
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_snapshot_and_definition_mutations_fail_before_an_official_event_is_published(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    incomplete_snapshot = case.model_copy(update={"input": {**case.input, "evidence": []}})
    monkeypatch.setattr(case_bootstrap, "load_frozen_decision_case", lambda _: incomplete_snapshot)

    incomplete = run_default_frozen_decision_case(migrated_settings)

    assert incomplete.business_result_status is None
    assert incomplete.business_commit_status == "NOT_ATTEMPTED"
    assert incomplete.publication_status == "CLOSED"
    assert incomplete.report is None
    assert incomplete.stage_results[-1].reasons == ("UNSUPPORTED_SYNTHETIC_RESULT",)
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }

    altered_evidence = [*case.input["evidence"]]
    altered_evidence[0] = {
        **altered_evidence[0],
        "statement": "A changed statement with the same synthetic evidence identifier.",
    }
    altered_snapshot = case.model_copy(
        update={"input": {**case.input, "evidence": altered_evidence}}
    )
    monkeypatch.setattr(case_bootstrap, "load_frozen_decision_case", lambda _: altered_snapshot)

    with pytest.raises(DecisionEventCommitError, match="business identity maps"):
        run_default_frozen_decision_case(migrated_settings)

    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


def test_reading_a_published_report_does_not_rebuild_it_from_current_code(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_default_frozen_decision_case(migrated_settings).report
    assert original is not None
    monkeypatch.setattr(
        DecisionEventFact,
        "formal_report",
        lambda *_: (_ for _ in ()).throw(AssertionError("report must be read from storage")),
    )

    assert get_formal_report(original.report_version_id, migrated_settings) == original
