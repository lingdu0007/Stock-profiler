from __future__ import annotations

from stock_profiler.modules.decision_cases.domain import (
    DEFAULT_SYNTHETIC_CASE_PATH,
    load_frozen_decision_case,
)


def test_frozen_synthetic_case_has_stable_independent_identities() -> None:
    first = load_frozen_decision_case(DEFAULT_SYNTHETIC_CASE_PATH)
    second = load_frozen_decision_case(DEFAULT_SYNTHETIC_CASE_PATH)

    assert first.synthetic is True
    assert first.generator_version == "frozen-decision-case-v1"
    assert first.seed == 4017
    assert first.qualification_scope == "D0_SYNTHETIC_CONTRACT_ONLY"
    assert first.knowledge_cutoff == "2042-05-17T16:00:00Z"
    assert first.evidence_clock.validated_at == "2042-05-17T15:18:00Z"
    assert first.version_bundle.m_agent_version == "0.5.0"
    assert first.business_object_id == second.business_object_id
    assert first.framework_run_id == second.framework_run_id
    assert first.decision_event_id == second.decision_event_id
    assert first.report_version_id == second.report_version_id
    assert len(
        {
            first.business_object_id,
            first.framework_run_id,
            first.decision_event_id,
            first.report_version_id,
        }
    ) == 4
