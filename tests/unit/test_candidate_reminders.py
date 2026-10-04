"""Deterministic candidate routing at the saved-view delivery seam."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from stock_profiler.modules.delivery.candidate_notifications import prepare_candidate_reminder
from stock_profiler.modules.delivery.candidate_reminder_contracts import CandidateReminderRequest
from stock_profiler.modules.delivery.candidate_workspace import (
    CandidateReleaseView,
    CandidateWorkspace,
)


def release_view() -> CandidateReleaseView:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/synthetic/candidate_workspace.json").read_text()
    )
    return CandidateWorkspace.model_validate(fixture["workspace"]).releases[0]


def request(kind: str) -> CandidateReminderRequest:
    return CandidateReminderRequest.model_validate(
        {
            "contract_version": "1.0.0",
            "synthetic": True,
            "generator_version": "candidate-reminder-v1",
            "seed": 1818,
            "kind": kind,
            "primary_result": "ACCEPTED",
            "fallback_result": "ACCEPTED",
        }
    )


def test_final_reminder_is_bounded_and_cannot_extend_original_window() -> None:
    release = release_view()
    assert release.final_reminder_before is not None
    now = release.final_reminder_before - timedelta(hours=1)
    initial = prepare_candidate_reminder(release, request("INITIAL"), now, "synthetic-scope", ())
    assert initial is not None
    final = prepare_candidate_reminder(
        release, request("FINAL"), now, "synthetic-scope", (initial,)
    )
    assert final is not None
    assert final.body.window_ends_at == release.valid_through
    assert (
        prepare_candidate_reminder(
            release, request("FINAL"), now, "synthetic-scope", (initial, final)
        )
        == final
    )
    assert (
        prepare_candidate_reminder(
            release, request("FINAL"), release.final_reminder_before, "synthetic-scope", (initial,)
        )
        is None
    )
    assert (
        prepare_candidate_reminder(
            release, request("PLAN_READY"), now, "synthetic-scope", (initial,)
        )
        is None
    )


def test_material_withdrawal_after_alert_bypasses_only_harmful_quiet_delay() -> None:
    release = release_view()
    now = datetime.fromisoformat("2042-07-02T08:00:00+00:00")
    initial = prepare_candidate_reminder(release, request("INITIAL"), now, "synthetic-scope", ())
    assert initial is not None
    withdrawn = release.model_copy(
        update={
            "status": "SUPERSEDED",
            "superseded_by_report_id": "synthetic-candidate-replacement",
            "status_reasons": ("EVIDENCE_INVALIDATED",),
        }
    )
    correction_request = request("CORRECTION").model_copy(
        update={"quiet_until": now + timedelta(hours=3)}
    )
    assert (
        prepare_candidate_reminder(withdrawn, correction_request, now, "synthetic-scope", ())
        is None
    )
    correction = prepare_candidate_reminder(
        withdrawn, correction_request, now, "synthetic-scope", (initial,)
    )
    assert correction is not None and correction.status == "ATTEMPTED"
    assert [channel.role for channel in correction.channels] == ["PRIMARY", "PERSISTENT"]
    assert correction.body.result_type == "WITHDRAWN_DO_NOT_RELY_ON_ORIGINAL"
    assert correction.body.candidate_count == 0
    assert correction.body.entry_path == initial.body.entry_path
    assert (
        prepare_candidate_reminder(
            withdrawn, correction_request, now, "synthetic-scope", (initial, correction)
        )
        == correction
    )
    assert release.valid_through is not None
    after_expiry = release.valid_through + timedelta(seconds=1)
    quiet_request = request("CORRECTION").model_copy(
        update={"quiet_until": after_expiry + timedelta(hours=3)}
    )
    ordinary = prepare_candidate_reminder(
        withdrawn, quiet_request, after_expiry, "synthetic-scope", (initial,)
    )
    assert ordinary is not None and ordinary.status == "DEFERRED"


@pytest.mark.parametrize("status", ["EXPIRED", "SUPERSEDED", "INVALIDATED"])
def test_ineligible_saved_release_cannot_create_current_result_reminder(status: str) -> None:
    release = release_view().model_copy(update={"status": status})
    now = datetime.fromisoformat("2042-07-02T08:00:00+00:00")
    assert (
        prepare_candidate_reminder(release, request("INITIAL"), now, "synthetic-scope", ()) is None
    )
    assert prepare_candidate_reminder(release, request("FINAL"), now, "synthetic-scope", ()) is None


def test_channel_failure_retry_preserves_intent_and_monthly_budget() -> None:
    release = release_view()
    now = datetime.fromisoformat("2042-07-02T08:00:00+00:00")
    failed_request = request("INITIAL").model_copy(
        update={"primary_result": "TIMEOUT", "fallback_result": "REJECTED"}
    )
    failed = prepare_candidate_reminder(release, failed_request, now, "synthetic-scope", ())
    assert failed is not None
    retry = request("INITIAL").model_copy(update={"retry_of": failed.attempt_id})
    success = prepare_candidate_reminder(release, retry, now, "synthetic-scope", (failed,))
    assert success is not None and success.intent_id == failed.intent_id
    assert success.body == failed.body
    assert (
        prepare_candidate_reminder(
            release, request("INITIAL"), now, "synthetic-scope", (failed, success)
        )
        is None
    )


def test_explanatory_revision_of_empty_result_does_not_create_withdrawal_alert() -> None:
    release = release_view()
    failed = release.model_copy(
        update={
            "status": "RESULT",
            "release": release.release.model_copy(update={"disposition": "FAILED", "members": ()}),
        }
    )
    now = datetime.fromisoformat("2042-07-02T08:00:00+00:00")
    initial = prepare_candidate_reminder(failed, request("INITIAL"), now, "synthetic-scope", ())
    assert initial is not None and initial.body.candidate_count == 0
    revised = failed.model_copy(
        update={"status": "SUPERSEDED", "superseded_by_report_id": "synthetic-explanatory-revision"}
    )
    assert (
        prepare_candidate_reminder(
            revised, request("CORRECTION"), now, "synthetic-scope", (initial,)
        )
        is None
    )


@pytest.mark.parametrize("failed", [False, True])
def test_withdrawal_identity_and_retry_survive_natural_expiry(failed: bool) -> None:
    release = release_view()
    now = datetime.fromisoformat("2042-07-02T08:00:00+00:00")
    initial = prepare_candidate_reminder(release, request("INITIAL"), now, "synthetic-scope", ())
    assert initial is not None and release.valid_through is not None
    withdrawn = release.model_copy(
        update={
            "status": "INVALIDATED",
            "status_reasons": ("AUTHORIZATION_REVOKED",),
            "status_evidence_ids": ("synthetic-revocation",),
            "withdrawal_evidence_ids": ("synthetic-revocation",),
        }
    )
    correction_request = request("CORRECTION").model_copy(
        update={
            "primary_result": "TIMEOUT" if failed else "ACCEPTED",
            "fallback_result": "REJECTED" if failed else "ACCEPTED",
        }
    )
    correction = prepare_candidate_reminder(
        withdrawn, correction_request, now, "synthetic-scope", (initial,)
    )
    assert correction is not None
    expired = withdrawn.model_copy(
        update={
            "status": "EXPIRED",
            "status_reasons": ("CANDIDATE_WINDOW_ENDED",),
            "status_evidence_ids": (release.event_id,),
        }
    )
    later = release.valid_through + timedelta(seconds=1)
    assert (
        prepare_candidate_reminder(
            expired, correction_request, later, "synthetic-scope", (initial, correction)
        )
        == correction
    )
    if failed:
        retry = prepare_candidate_reminder(
            expired,
            request("CORRECTION").model_copy(update={"retry_of": correction.attempt_id}),
            later,
            "synthetic-scope",
            (initial, correction),
        )
        assert retry is not None and retry.intent_id == correction.intent_id
        assert retry.body == correction.body
