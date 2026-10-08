"""Host-owned formal evidence must not exhaust the framework model budget."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from test_prospective_shadow_cycle import cycle_case

from stock_profiler.adapters.m_agent.frozen_decision_case import execute_frozen_decision_case
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.prospective.contracts import CycleCommand


def test_formal_evidence_stays_host_owned_across_framework_execution_and_recovery(
    migrated_settings: Settings,
) -> None:
    payload = cycle_case(migrated_settings, "framework-evidence")
    marks = []
    for index in range(130):
        at = (datetime(2042, 6, 1, tzinfo=UTC) + timedelta(days=index)).isoformat()
        marks.append(
            {
                "evidence_id": f"fictional-daily-mark-{index}",
                "authority": "EXCHANGE",
                "source_version": "fictional-market-v1",
                "effective_at": at,
                "published_at": at,
                "acquired_at": at,
                "validated_at": at,
                "corrects_evidence_id": None,
                "correction_reason": None,
                "price": "10",
                "quantity_multiplier": "1",
                "cash_distributions": "0",
                "actions_complete_through": at,
                "costs": {"commission": "0", "fees": "0", "taxes": "0", "slippage": "0"},
            }
        )
    payload["prospective"].update(
        operation="CHECK",
        registration=None,
        registration_event_id="fictional-original-registration",
        formal_node_month="2043-11",
        formal_months=[
            {
                "plan_month": f"2042-{month:02}",
                "selection_event_id": f"fictional-original-selection-{month}",
                "standard_event_id": None,
                "observations": [],
                "paths": [
                    {"security_id": f"fictional-member-{member}", "marks": marks}
                    for member in range(10)
                ],
                "index": None,
                "factors": None,
                "future_index": None,
            }
            for month in range(1, 5)
        ],
    )
    payload["prospective"] = CycleCommand.model_validate(payload["prospective"]).model_dump(
        mode="json"
    )
    payload["input"]["prospective"] = deepcopy(payload["prospective"])
    case = FrozenDecisionCase.model_validate(payload)
    original = case.model_dump(mode="json")
    runtime = initialize_runtime_storage(migrated_settings)
    result = asyncio.run(execute_frozen_decision_case(case, runtime))
    assert result.status == "SUCCEEDED", result.error_code
    replayed = asyncio.run(execute_frozen_decision_case(case, runtime))
    assert replayed.status == "SUCCEEDED"
    assert replayed.run_id == result.run_id and replayed.output == result.output
    assert case.model_dump(mode="json") == original

    changed = deepcopy(payload)
    changed["prospective"]["formal_months"][0]["paths"][0]["marks"][0]["price"] = "11"
    changed["input"]["prospective"] = deepcopy(changed["prospective"])
    other = FrozenDecisionCase.model_validate(changed)
    assert other.framework_run_id != case.framework_run_id
    assert other.frozen_input_fingerprint != case.frozen_input_fingerprint
    changed_result = asyncio.run(execute_frozen_decision_case(other, runtime))
    assert changed_result.status == "SUCCEEDED"
    assert changed_result.run_id != result.run_id
