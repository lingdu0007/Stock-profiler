"""Compose the same frozen journey for CLI and authenticated HTTP delivery."""

from __future__ import annotations

from dataclasses import dataclass

from stock_profiler.adapters.m_agent.frozen_decision_case import (
    execute_frozen_decision_case,
    find_unmapped_legacy_frozen_decision_case,
    validate_frozen_recovery_case,
)
from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.runtime_ownership import (
    RuntimeStorage,
    initialize_runtime_storage,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.clock import Clock
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    DecisionCaseCorrection,
    DecisionCaseExecution,
    FormalReport,
    FrozenDecisionCase,
    NotificationAttempt,
    NotificationAttemptStatus,
    load_frozen_decision_case,
)
from stock_profiler.modules.decision_cases.ports import (
    FrameworkRunResult,
    FrameworkTransitionRecorder,
)


@dataclass(frozen=True)
class _FrozenFramework:
    runtime: RuntimeStorage

    async def validate_recovery(self, case: FrozenDecisionCase) -> None:
        await validate_frozen_recovery_case(case, self.runtime)

    async def recover_unmapped(
        self, case: FrozenDecisionCase, known_run_ids: frozenset[str]
    ) -> FrozenDecisionCase | None:
        return await find_unmapped_legacy_frozen_decision_case(case, self.runtime, known_run_ids)

    async def execute(
        self, case: FrozenDecisionCase, record_transition: FrameworkTransitionRecorder
    ) -> FrameworkRunResult:
        return await execute_frozen_decision_case(case, self.runtime, record_transition)


def run_default_frozen_decision_case(
    settings: Settings, *, clock: Clock | None = None
) -> DecisionCaseExecution:
    case = load_frozen_decision_case(settings)
    runtime = initialize_runtime_storage(settings)
    return service.run_default_frozen_decision_case(
        case, DecisionLedger(runtime.engine, clock=clock), _FrozenFramework(runtime)
    )


def replay_default_frozen_decision_case(
    settings: Settings,
    business_identity: str,
    *,
    clock: Clock | None = None,
    recovery_case: FrozenDecisionCase | None = None,
) -> DecisionCaseExecution:
    case = load_frozen_decision_case(settings)
    runtime = initialize_runtime_storage(settings)
    return service.replay_default_frozen_decision_case(
        case,
        DecisionLedger(runtime.engine, clock=clock),
        _FrozenFramework(runtime),
        business_identity,
        recovery_case=recovery_case,
    )


def correct_default_frozen_decision_case(
    settings: Settings, business_identity: str, *, clock: Clock | None = None
) -> DecisionCaseCorrection:
    return service.correct_default_frozen_decision_case(
        load_frozen_decision_case(settings),
        DecisionLedger.from_settings(settings, clock=clock),
        business_identity,
    )


def retry_default_frozen_decision_case_notification(
    settings: Settings,
    business_identity: str,
    status: NotificationAttemptStatus,
    *,
    clock: Clock | None = None,
) -> NotificationAttempt:
    return service.retry_default_frozen_decision_case_notification(
        load_frozen_decision_case(settings),
        DecisionLedger.from_settings(settings, clock=clock),
        business_identity,
        status,
    )


def get_formal_report(report_version_id: str, settings: Settings) -> FormalReport | None:
    return service.get_formal_report(report_version_id, DecisionLedger.from_settings(settings))
