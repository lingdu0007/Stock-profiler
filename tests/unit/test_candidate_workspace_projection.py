"""Frozen report identities and fail-closed read-time eligibility."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FormalReport, load_frozen_decision_case
from stock_profiler.modules.delivery.candidate_workspace import (
    CandidateWorkspace,
    CandidateWorkspaceSource,
    project_candidate_workspace,
)


def test_missing_current_qualification_does_not_make_saved_snapshot_current(
    settings: Settings,
) -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/synthetic/candidate_workspace.json").read_text()
    )
    view = CandidateWorkspace.model_validate(fixture["workspace"]).releases[0]
    report_payload = json.loads(
        (Path(__file__).parents[1] / "fixtures/synthetic/formal_report_projection.json").read_text()
    )["report"]
    report_payload["result"]["candidate_release"] = view.release.model_dump(mode="json")
    report = FormalReport.model_validate(report_payload)
    case = load_frozen_decision_case(settings)
    workspace = project_candidate_workspace(
        (CandidateWorkspaceSource(report, case),),
        datetime.fromisoformat("2042-07-01T09:00:00+00:00"),
    )
    assert workspace.releases[0].status == "INVALIDATED"
    assert "CURRENT_QUALIFICATION_UNAVAILABLE" in workspace.releases[0].status_reasons


def qualified_source(settings: Settings) -> CandidateWorkspaceSource:
    from synthetic_candidate_workspace import failed_candidate_case

    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.qualification.contracts import (
        CapabilityVersion,
        GovernanceOutcome,
        QualificationEvidence,
        QualificationPolicy,
        QualificationRecord,
        QualificationScope,
    )

    view = CandidateWorkspace.model_validate(
        json.loads(
            (Path(__file__).parents[1] / "fixtures/synthetic/candidate_workspace.json").read_text()
        )["workspace"]
    ).releases[0]
    case = FrozenDecisionCase.model_validate(failed_candidate_case(settings))
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures/synthetic/formal_report_projection.json").read_text()
    )["report"]
    payload["result"]["candidate_release"] = view.release.model_dump(mode="json")
    report = FormalReport.model_validate(payload)
    scope = QualificationScope(
        capability="candidate-release",
        purpose="CANDIDATE_BUY",
        evidence_level="D0",
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        account_type="SYNTHETIC",
        source="SYNTHETIC_D0",
        market_state="BULL",
        board="SYNTHETIC",
        target="SIX_MONTH_TERMINAL_20_PERCENT",
    )
    policy = QualificationPolicy(
        contract_version="1.0.0",
        policy_version="synthetic-workspace-policy-v1",
        synthetic=True,
        generator_version="candidate-workspace-qualification-v1",
        seed=1818,
        evaluation_max_age_months=12,
        state_activity_max_age_months=12,
        require_state_activity=False,
    )
    version = CapabilityVersion(
        version_id=view.release.capability_version,
        policy_version=policy.policy_version,
        implementation=case.version_bundle,
        qualification_policy=policy,
    )
    assert view.release.qualification is not None
    snapshot = view.release.qualification
    evidence = QualificationEvidence(
        evidence_id="synthetic-workspace-qualification-proof",
        synthetic=True,
        generator_version="candidate-workspace-qualification-v1",
        seed=1818,
        version=version,
        scope=scope,
        kind="QUALIFICATION_PASS",
        digest="a" * 64,
        evaluation_end=snapshot.recorded_at,
        available_at=snapshot.recorded_at,
        expires_at=snapshot.valid_through,
        market_calendar_version=view.release.market_calendar_version,
    )
    record = QualificationRecord(
        decision_id=snapshot.qualification_id,
        authorization_id=snapshot.qualification_id,
        scope=scope,
        version=version,
        status="VALID",
        cause="QUALIFICATION_PASS",
        authorization_evidence=evidence,
        recorded_at=snapshot.recorded_at,
        evidence=evidence,
        formal_evidence=evidence,
        formal_passing_evidence=evidence,
    )
    return CandidateWorkspaceSource(
        report,
        case,
        (
            GovernanceOutcome(
                disposition="APPROVED", reasons=("QUALIFICATION_PASS",), qualification=record
            ),
        ),
    )


def test_restoration_and_same_value_review_do_not_revive_original_release(
    settings: Settings,
) -> None:
    from dataclasses import replace

    from stock_profiler.modules.qualification.contracts import GovernanceOutcome

    source = qualified_source(settings)
    now = datetime.fromisoformat("2042-07-02T09:00:00+00:00")
    original = source.qualification_history[0].qualification
    assert original is not None
    assert project_candidate_workspace((source,), now).releases[0].status == "CURRENT"
    revoked = original.model_copy(
        update={
            "decision_id": "synthetic-revocation",
            "previous_decision_id": original.decision_id,
            "recorded_at": now,
            "status": "REVOKED",
            "cause": "AUTHORIZATION_REVOKED",
        }
    )
    revoked_outcome = GovernanceOutcome(
        disposition="APPROVED", reasons=("AUTHORIZATION_REVOKED",), qualification=revoked
    )
    history = (*source.qualification_history, revoked_outcome)
    withdrawn = project_candidate_workspace(
        (replace(source, qualification_history=history),), now
    ).releases[0]
    assert withdrawn.status == "INVALIDATED"
    reviewed = revoked.model_copy(
        update={
            "decision_id": "synthetic-same-value-review",
            "previous_decision_id": revoked.decision_id,
        }
    )
    same_value = project_candidate_workspace(
        (
            replace(
                source,
                qualification_history=(
                    *history,
                    revoked_outcome.model_copy(update={"qualification": reviewed}),
                ),
            ),
        ),
        now,
    ).releases[0]
    assert same_value.status_evidence_ids == withdrawn.status_evidence_ids
    restored = original.model_copy(
        update={
            "decision_id": "synthetic-restoration",
            "previous_decision_id": revoked.decision_id,
            "recorded_at": now,
            "cause": "EXPLICIT_RESTORATION",
        }
    )
    restored_view = project_candidate_workspace(
        (
            replace(
                source,
                qualification_history=(
                    *history,
                    revoked_outcome.model_copy(update={"qualification": restored}),
                ),
            ),
        ),
        now,
    ).releases[0]
    assert restored_view.status == "INVALIDATED"
    assert restored_view.status_evidence_ids == withdrawn.status_evidence_ids
    assert restored_view.release == withdrawn.release
    assert restored_view.valid_through == withdrawn.valid_through


def test_current_at_risk_qualification_is_separate_from_publication_snapshot(
    settings: Settings,
) -> None:
    from dataclasses import replace

    source = qualified_source(settings)
    original = source.qualification_history[0].qualification
    assert original is not None
    now = datetime.fromisoformat("2042-07-02T09:00:00+00:00")
    at_risk = original.model_copy(
        update={
            "decision_id": "synthetic-current-diagnostic-alert",
            "previous_decision_id": original.decision_id,
            "recorded_at": now,
            "status": "AT_RISK",
            "cause": "DIAGNOSTIC_ALERT",
        }
    )
    revised = replace(
        source,
        qualification_history=(
            *source.qualification_history,
            source.qualification_history[0].model_copy(update={"qualification": at_risk}),
        ),
    )
    release = project_candidate_workspace((revised,), now).releases[0]
    assert release.status == "CURRENT"
    assert release.current_qualification_status == "AT_RISK"
    assert (
        release.release.qualification is not None
        and release.release.qualification.status == "VALID"
    )


def test_expiry_retains_original_window_anchor_across_same_value_review(settings: Settings) -> None:
    from dataclasses import replace

    source = qualified_source(settings)
    now = datetime.fromisoformat("2042-07-08T09:00:00+00:00")
    first = project_candidate_workspace((source,), now).releases[0]
    assert first.status == "EXPIRED"
    record = source.qualification_history[0].qualification
    assert record is not None
    reviewed = record.model_copy(
        update={
            "decision_id": "synthetic-expired-window-review",
            "previous_decision_id": record.decision_id,
            "recorded_at": now,
        }
    )
    second = project_candidate_workspace(
        (
            replace(
                source,
                qualification_history=(
                    *source.qualification_history,
                    source.qualification_history[0].model_copy(update={"qualification": reviewed}),
                ),
            ),
        ),
        now,
    ).releases[0]
    assert second.status == "EXPIRED"
    assert second.status_evidence_ids == first.status_evidence_ids
    assert second.valid_through == first.valid_through


def test_withdrawal_evidence_is_retained_after_natural_expiry(settings: Settings) -> None:
    from dataclasses import replace

    source = qualified_source(settings)
    source = replace(source, research_correction_event_id="synthetic-research-withdrawal")
    before = project_candidate_workspace(
        (source,), datetime.fromisoformat("2042-07-02T09:00:00+00:00")
    ).releases[0]
    after = project_candidate_workspace(
        (source,), datetime.fromisoformat("2042-07-08T09:00:00+00:00")
    ).releases[0]
    assert before.status == "INVALIDATED" and after.status == "EXPIRED"
    assert (
        before.withdrawal_evidence_ids
        == after.withdrawal_evidence_ids
        == ("synthetic-research-withdrawal",)
    )


def test_window_expiry_anchor_does_not_change_when_qualification_expires_later(
    settings: Settings,
) -> None:
    source = qualified_source(settings)
    expired = project_candidate_workspace(
        (source,), datetime.fromisoformat("2042-07-08T09:00:00+00:00")
    ).releases[0]
    later = project_candidate_workspace(
        (source,), datetime.fromisoformat("2042-08-02T09:00:00+00:00")
    ).releases[0]
    assert expired.status == later.status == "EXPIRED"
    assert (
        expired.withdrawal_evidence_ids
        == later.withdrawal_evidence_ids
        == (source.report.event_id,)
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("scope", "CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED"),
        ("basis", "CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED"),
        ("conflicting_heads", "CURRENT_QUALIFICATION_AMBIGUOUS"),
        ("no_head", "CURRENT_QUALIFICATION_UNAVAILABLE"),
        ("missing_predecessor", "CURRENT_QUALIFICATION_LINEAGE_UNAVAILABLE"),
        ("cyclic_predecessor", "CURRENT_QUALIFICATION_AMBIGUOUS"),
        ("changed_version", "CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED"),
        ("latest_basis_missing", "CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED"),
        ("latest_calendar", "CURRENT_QUALIFICATION_SCOPE_OR_VERSION_CHANGED"),
        ("original_expired", "RELEASE_QUALIFICATION_EXPIRED"),
        ("latest_expired", "CURRENT_QUALIFICATION_EXPIRED"),
        ("scope_missing", "CURRENT_QUALIFICATION_UNAVAILABLE"),
    ],
)
def test_unusable_qualification_history_preserves_the_original_candidate(
    settings: Settings,
    change: str,
    reason: str,
) -> None:
    from dataclasses import replace

    source = qualified_source(settings)
    original = source.qualification_history[0].qualification
    assert original is not None
    now = datetime.fromisoformat("2042-07-02T09:00:00+00:00")
    latest = original.model_copy(
        update={
            "decision_id": "synthetic-latest-review",
            "previous_decision_id": original.decision_id,
            "recorded_at": now,
        }
    )
    records = [original]
    if change == "scope":
        records = [
            original.model_copy(
                update={
                    "scope": original.scope.model_copy(update={"user_id": "synthetic-other-user"})
                }
            )
        ]
    elif change == "basis":
        records = [
            original.model_copy(
                update={"formal_passing_evidence": None, "authorization_evidence": None}
            )
        ]
    elif change == "conflicting_heads":
        records.append(latest.model_copy(update={"previous_decision_id": None}))
    elif change == "no_head":
        records = [original.model_copy(update={"previous_decision_id": original.decision_id})]
    elif change in {"missing_predecessor", "cyclic_predecessor"}:
        records = [
            original.model_copy(update={"previous_decision_id": original.decision_id}),
            latest.model_copy(update={"previous_decision_id": "synthetic-unmatched-predecessor"}),
        ]
        if change == "cyclic_predecessor":
            records.append(
                latest.model_copy(
                    update={
                        "decision_id": "synthetic-unmatched-predecessor",
                        "previous_decision_id": "synthetic-unmatched-predecessor",
                        "version": original.version.model_copy(
                            update={"version_id": "synthetic-other-version"}
                        ),
                    }
                )
            )
    elif change == "changed_version":
        records.append(
            latest.model_copy(
                update={
                    "version": original.version.model_copy(
                        update={"version_id": "synthetic-other-version"}
                    )
                }
            )
        )
    elif change in {"latest_basis_missing", "latest_calendar"}:
        basis = latest.formal_passing_evidence
        assert basis is not None
        records.append(
            latest.model_copy(
                update={
                    "formal_passing_evidence": None
                    if change == "latest_basis_missing"
                    else basis.model_copy(
                        update={"market_calendar_version": "synthetic-other-calendar"}
                    ),
                    "authorization_evidence": None,
                }
            )
        )
    elif change in {"original_expired", "latest_expired"}:
        target = original if change == "original_expired" else latest
        expired = {}
        for name in ("authorization_evidence", "formal_evidence", "formal_passing_evidence"):
            evidence = getattr(target, name)
            assert evidence is not None
            expired[name] = evidence.model_copy(
                update={"expires_at": datetime.fromisoformat("2042-07-01T08:00:00+00:00")}
            )
        expired_record = target.model_copy(update=expired)
        records = (
            [expired_record, latest] if change == "original_expired" else [original, expired_record]
        )
    elif change == "scope_missing":
        source = replace(source, case=source.case.model_copy(update={"access_scope": None}))
    history = tuple(
        source.qualification_history[0].model_copy(update={"qualification": record})
        for record in records
    )
    projected = project_candidate_workspace(
        (replace(source, qualification_history=history),), now
    ).releases[0]
    assert projected.status == "INVALIDATED"
    assert reason in projected.status_reasons
    assert projected.release == source.report.result.candidate_release
