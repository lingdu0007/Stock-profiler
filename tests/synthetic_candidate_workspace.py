"""Original synthetic candidate-case inputs for read-boundary contract tests."""

from stock_profiler.bootstrap.settings import Settings


def failed_candidate_case(settings: Settings) -> dict[str, object]:
    from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case

    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    payload["generator_version"] = "candidate-workspace-case-v1"
    payload["seed"] = 1818
    payload["case_id"] = "synthetic-workspace-failure-case"
    payload["business_identity"] = "synthetic-workspace-failure"
    bundle = payload["version_bundle"]
    for field in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        bundle[field] = "candidate-release.1.0.0"
    bundle["agent_definition_version"] = "2.0.0"
    payload["agent_definition"]["version"] = "2.0.0"
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": ["synthetic-account-4017"],
        "visibility": "USER",
    }
    payload["candidate_release"] = {
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "candidate-workspace-failure-v1",
        "seed": 1818,
        "batch_id": "synthetic-workspace-batch",
        "qualification_scope": "D0_SYNTHETIC_CONTRACT_ONLY",
        "capability_version": "synthetic-candidate-v1",
        "research_object_id": "missing-research",
        "research_event_id": "missing-research-event",
        "purpose": "CANDIDATE_BUY",
        "knowledge_cutoff": payload["knowledge_cutoff"],
        "published_at": payload["report_generated_at"],
        "last_completed_market_session_sequence": 1,
        "market_state": "BULL",
        "market_calendar_version": "synthetic-calendar-v1",
        "qualifications": [],
        "label_watermark_at": payload["knowledge_cutoff"],
        "calibrator_selection_window_months": [],
        "calibrator_selection_records": [],
        "training_window_months": [],
        "training_records": [],
        "recent_diagnostic_window_months": [],
        "recent_diagnostic_records": [],
        "market_sessions": [],
        "candidates": [
            {
                "security_id": "SYNTH-WORKSPACE",
                "research_id": "synthetic-research-1818",
                "raw_success_score": "0",
                "data_complete": True,
                "risk_status": "ACCEPTED",
                "risk_gates": [{"gate_id": "INDEPENDENT_RISK", "status": "PASSED"}],
                "risk_reasons": ["SYNTHETIC_ACCEPTED"],
                "thesis": "Invented synthetic thesis.",
                "principal_risks": ["Invented synthetic risk."],
                "evidence_freshness": "AT_CUTOFF",
            }
        ],
    }
    from stock_profiler.modules.candidate_selection.calibrated_candidates import (
        CandidateReleaseCommand,
    )

    payload["candidate_release"] = CandidateReleaseCommand.model_validate(
        payload["candidate_release"]
    ).model_dump(mode="json")
    payload["input"]["scenario"] = "CANDIDATE_RELEASE_REQUESTED"
    payload["input"]["candidate_release"] = payload["candidate_release"]
    payload["expected_external_result"] = {
        "outcome_code": "CANDIDATE_RELEASE_REQUESTED",
        "summary": "Frozen synthetic result CANDIDATE_RELEASE_REQUESTED.",
        "key_reasons": [
            "The frozen synthetic candidate release request is ready for host evaluation."
        ],
    }
    return payload
