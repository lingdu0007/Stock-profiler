"""Host-owned frozen decision journey: Run evidence, validate, commit, then publish."""

from __future__ import annotations

import asyncio

from stock_profiler.adapters.m_agent.frozen_decision_case import execute_frozen_decision_case
from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    DecisionCaseExecution,
    ExternalResult,
    FormalReport,
    FrozenDecisionCase,
    load_frozen_decision_case,
)


def run_default_frozen_decision_case(settings: Settings) -> DecisionCaseExecution:
    """Run the published public fixture through the same host module used by every entrypoint."""
    return _run_frozen_decision_case(settings)


def replay_default_frozen_decision_case(
    settings: Settings, business_identity: str
) -> DecisionCaseExecution:
    """Replay only when the caller names the fixture's immutable business identity."""
    case = load_frozen_decision_case(settings)
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    return _run_frozen_decision_case(settings)


def _run_frozen_decision_case(settings: Settings) -> DecisionCaseExecution:
    """Run or replay one exact frozen business identity without HTTP transport."""
    case = load_frozen_decision_case(settings)
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine)
    with ledger.serialize_case_execution() as connection:
        existing_report = ledger.get_formal_report(case.report_version_id, connection)
        if existing_report is not None:
            return _execution(case, existing_report)

        ledger.ensure_business_object(connection, case)
        framework = asyncio.run(execute_frozen_decision_case(case, runtime))
        if framework.status != "SUCCEEDED" or framework.output is None:
            raise DecisionEventCommitError("framework did not produce a publishable typed output")
        result = ExternalResult.model_validate_json(framework.output)
        if result != case.expected_external_result:
            raise DecisionEventCommitError("host validation rejected the framework output")
        report = ledger.commit_event_and_report(
            connection,
            case=case,
            framework_run_id=framework.run_id,
            result=result,
        )
    return _execution(case, report)


def get_formal_report(report_version_id: str, settings: Settings) -> FormalReport | None:
    """Read only an already committed formal report."""
    return DecisionLedger.from_settings(settings).get_formal_report(report_version_id)


def _execution(case: FrozenDecisionCase, report: FormalReport) -> DecisionCaseExecution:
    return DecisionCaseExecution(
        business_object_id=case.business_object_id,
        framework_run_id=case.framework_run_id,
        decision_event_id=case.decision_event_id,
        report_version_id=case.report_version_id,
        framework_run_status="SUCCEEDED",
        business_commit_status="COMMITTED",
        publication_status="PUBLISHED",
        report=report,
    )
