from __future__ import annotations

from stock_profiler.bootstrap.decision_cases import (
    correct_default_frozen_decision_case,
    get_formal_report,
    run_default_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case
from stock_profiler.modules.delivery.access import AccessPrincipal


def test_correction_retains_changed_evidence_without_backdating_the_original(
    migrated_settings: Settings,
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    assert original.report is not None
    case = load_frozen_decision_case(migrated_settings)
    correction = correct_default_frozen_decision_case(migrated_settings, case.business_identity)
    evidence = correction.report.result.model_dump(mode="json").get("correction_evidence")

    assert evidence is not None
    assert evidence["corrects_evidence_id"] == "synthetic-evidence-001"
    assert evidence["original_statement"] == case.input["evidence"][0]["statement"]
    assert evidence["corrected_statement"] == (
        "The fictional issuer has not completed the imaginary orbital-mosaic checklist."
    )
    assert evidence["evidence_clock"]["source_published_at"] == "2042-05-17T16:01:10Z"
    assert evidence["knowledge_cutoff"] == "2042-05-17T16:01:50Z"
    assert evidence["knowledge_cutoff"] > original.report.knowledge_cutoff
    assert correction.report.framework_run_id == original.framework_run_id
    assert (
        get_formal_report(
            original.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id="stock-profiler-single-user",
                account_ids=("synthetic-account-4017",),
                permissions=("REPORT_READ",),
            ),
        )
        == original.report
    )
    assert (
        correct_default_frozen_decision_case(migrated_settings, case.business_identity)
        == correction
    )
