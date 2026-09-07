from __future__ import annotations

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case


def test_independent_scoped_cases_complete_and_replay_in_one_store(
    migrated_settings: Settings,
) -> None:
    payloads = []
    executions = []
    for identity in ("synthetic-sequence-one", "synthetic-sequence-two"):
        payload = load_frozen_decision_case(migrated_settings).model_dump(mode="json")
        payload["case_id"] = identity
        payload["business_identity"] = f"synthetic:sequence:{identity}"
        payload["version_bundle"].update(
            case_contract_version="3.0.0",
            host_contract_version="3.0.0",
            report_projection_contract_version="3.0.0",
            agent_definition_version="2.0.0",
        )
        payload["agent_definition"]["version"] = "2.0.0"
        payload["access_scope"] = {
            "contract_version": "1.0.0",
            "user_id": "stock-profiler-single-user",
            "account_ids": ["synthetic-account-4017"],
            "visibility": "USER",
        }
        execution = run_frozen_decision_case(migrated_settings, payload)
        assert execution.report is not None
        assert execution.report.result.outcome_code == "SYNTHETIC_REVIEW_COMPLETE"
        payloads.append(payload)
        executions.append(execution)

    assert executions[0].framework_run_id != executions[1].framework_run_id
    assert executions[0].decision_event_id != executions[1].decision_event_id
    for payload, execution in zip(payloads, executions, strict=True):
        assert run_frozen_decision_case(migrated_settings, payload).report == execution.report
