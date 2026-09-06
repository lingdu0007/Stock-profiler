from __future__ import annotations

from typing import Any

import pytest

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.delivery.access import AccessPrincipal


def test_scoped_correction_and_replay_keep_original_result_authorization(
    migrated_settings: Settings, scoped_payload: dict[str, Any]
) -> None:
    first = run_frozen_decision_case(migrated_settings, scoped_payload)
    assert run_frozen_decision_case(migrated_settings, scoped_payload).report == first.report
    case = FrozenDecisionCase.model_validate(scoped_payload)
    correction = service.correct_default_frozen_decision_case(
        case, DecisionLedger.from_settings(migrated_settings), case.business_identity
    )
    assert correction.report is not None
    assert correction.report.access_scope == case.access_scope
    assert correction.report.version_bundle.report_projection_contract_version == "3.0.0"
    delivery = ResultDelivery.from_settings(migrated_settings)
    owner = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    assert delivery.read_report(correction.report.report_version_id, owner) == correction.report
    assert delivery.read_report(first.report_version_id, owner) == first.report


def test_uncommitted_host_provenance_is_rejected_and_audited(
    migrated_settings: Settings, scoped_payload: dict[str, Any]
) -> None:
    scoped_payload["version_bundle"]["host_source_sha"] = "b" * 40
    with pytest.raises(ValueError, match="provenance"):
        run_frozen_decision_case(migrated_settings, scoped_payload)
    assert ResultDelivery.from_settings(migrated_settings).audit_history()[0].reason == (
        "UNDECLARED_CAPABILITY"
    )
