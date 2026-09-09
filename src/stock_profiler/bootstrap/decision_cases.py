"""Compose the same frozen journey for CLI and authenticated HTTP delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stock_profiler.adapters.m_agent.frozen_decision_case import (
    execute_frozen_decision_case,
    find_unmapped_legacy_frozen_decision_case,
    validate_frozen_recovery_case,
)
from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
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
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.qualification.governance import InvalidGovernanceRequest


@dataclass(frozen=True)
class _FrozenFramework:
    runtime: RuntimeStorage
    clock: Clock | None = None

    async def validate_recovery(self, case: FrozenDecisionCase) -> None:
        await validate_frozen_recovery_case(case, self.runtime)

    async def recover_unmapped(
        self, case: FrozenDecisionCase, known_run_ids: frozenset[str]
    ) -> FrozenDecisionCase | None:
        return await find_unmapped_legacy_frozen_decision_case(case, self.runtime, known_run_ids)

    async def execute(
        self, case: FrozenDecisionCase, record_transition: FrameworkTransitionRecorder
    ) -> FrameworkRunResult:
        return await execute_frozen_decision_case(
            case, self.runtime, record_transition, clock=self.clock
        )


def run_frozen_decision_case(
    settings: Settings, payload: dict[str, Any], *, clock: Clock | None = None
) -> DecisionCaseExecution:
    """Host-console D0 acceptance of a versioned case, never a public command API."""
    runtime = initialize_runtime_storage(settings)
    try:
        case = FrozenDecisionCase.model_validate(payload)
        if case.recovery_framework_run_id is not None:
            raise ValueError("use explicit original-Run recovery")
        if (
            case.version_bundle.host_application_version != settings.configuration_version
            or case.version_bundle.host_source_sha != settings.source_sha
        ):
            raise ValueError("frozen host provenance does not match the executing build")
    except ValueError:
        ResultDelivery(runtime.engine, clock=clock).record_capability_denial(
            "frozen-case-input", "HOST"
        )
        raise
    try:
        return service.run_default_frozen_decision_case(
            case, DecisionLedger(runtime.engine, clock=clock), _FrozenFramework(runtime, clock)
        )
    except InvalidGovernanceRequest:
        ResultDelivery(runtime.engine, clock=clock).record_capability_denial(
            "frozen-case-input", "HOST"
        )
        raise


def run_default_frozen_decision_case(
    settings: Settings, *, clock: Clock | None = None
) -> DecisionCaseExecution:
    case = load_frozen_decision_case(settings)
    runtime = initialize_runtime_storage(settings)
    return service.run_default_frozen_decision_case(
        case, DecisionLedger(runtime.engine, clock=clock), _FrozenFramework(runtime, clock)
    )


def replay_default_frozen_decision_case(
    settings: Settings,
    business_identity: str,
    *,
    clock: Clock | None = None,
    recovery_case: FrozenDecisionCase | None = None,
) -> DecisionCaseExecution:
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine, clock=clock)
    case = (
        recovery_case
        if recovery_case is not None and recovery_case.access_scope is not None
        else _console_case(settings, business_identity, ledger)
    )
    return service.replay_default_frozen_decision_case(
        case,
        ledger,
        _FrozenFramework(runtime, clock),
        business_identity,
        recovery_case=recovery_case,
    )


def correct_default_frozen_decision_case(
    settings: Settings, business_identity: str, *, clock: Clock | None = None
) -> DecisionCaseCorrection:
    ledger = DecisionLedger.from_settings(settings, clock=clock)
    return service.correct_default_frozen_decision_case(
        _console_case(settings, business_identity, ledger),
        ledger,
        business_identity,
    )


def _console_case(
    settings: Settings, business_identity: str, ledger: DecisionLedger
) -> FrozenDecisionCase:
    default = load_frozen_decision_case(settings)
    if default.business_identity == business_identity:
        return default
    stored = ledger.get_frozen_case_by_identity(business_identity)
    if stored is None:
        raise ValueError("unknown frozen decision-case business identity")
    return stored


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


def get_formal_report(
    report_version_id: str,
    settings: Settings,
    *,
    principal: AccessPrincipal | None = None,
    clock: Clock | None = None,
) -> FormalReport | None:
    return ResultDelivery.from_settings(settings, clock=clock).read_report(
        report_version_id, principal
    )
