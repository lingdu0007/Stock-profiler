from __future__ import annotations

from typing import Any

import pytest
from test_position_state_reconciliation import position_case_payload, position_snapshot_command
from test_scoped_qualification import GovernanceClock, case_payload

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.delivery.access import AccessPrincipal


def exit_case(settings: Settings, identity: str, command: dict[str, Any]) -> dict[str, Any]:
    payload = case_payload(settings, identity, {}, contract_version="security-exit.1.0.0")
    del payload["governance"]
    payload["access_scope"]["account_ids"] = ["synthetic-account-4017", "synthetic-account-8029"]
    payload["exit_assessment"] = command
    payload["input"]["exit_assessment"] = command
    payload["knowledge_cutoff"] = command["cutoff_at"]
    return payload


def registration(settings: Settings) -> dict[str, Any]:
    position = run_frozen_decision_case(
        settings,
        position_case_payload(settings, "exit-origin", position_snapshot_command()),
        clock=GovernanceClock(),
    )
    assert position.report is not None
    return {
        "operation": "REGISTER_THESIS",
        "contract_version": "1.0.0",
        "cutoff_at": "2042-05-17T16:00:00Z",
        "position_event_id": position.decision_event_id,
        "position_id": "synthetic-position-4017-xqz",
        "thesis": {
            "thesis_id": "synthetic-exit-thesis",
            "version": "synthetic-thesis-v1",
            "security_id": "XQZ-4017",
            "lifecycle_id": "synthetic-lifecycle-4017-xqz",
            "origin": "EXTERNAL",
            "registered_at": "2042-05-17T16:00:00Z",
            "valid_until": "2042-11-17T16:00:00Z",
            "propositions": [
                {
                    "proposition_id": "synthetic-business-premise",
                    "statement": "Fictional licensed operation remains active.",
                    "metric": "licensed_operation_count",
                    "authority": "DISCLOSURE",
                    "source": "synthetic-original-disclosure",
                    "operator": "LE",
                    "threshold": "0",
                    "trigger_direction": "LIQUIDATE",
                }
            ],
        },
    }


def test_first_management_freezes_forward_only_thesis_and_replays_report(
    migrated_settings: Settings,
) -> None:
    payload = exit_case(migrated_settings, "thesis-registration", registration(migrated_settings))
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-17T16:00:00Z")
    )
    assert execution.report is not None
    outcome = execution.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "THESIS_REGISTERED", outcome
    assert outcome["thesis"] == payload["exit_assessment"]["thesis"]
    assert not outcome["hard_gates"]
    assert outcome["probabilities"] == []
    assert outcome["thesis_event_id"] == execution.decision_event_id
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-17T16:00:00Z")
        ).report
        == execution.report
    )
    assert (
        get_formal_report(
            execution.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id="stock-profiler-single-user",
                account_ids=("synthetic-account-4017", "synthetic-account-8029"),
                permissions=("REPORT_READ",),
            ),
            clock=GovernanceClock(),
        )
        == execution.report
    )


def assessment(settings: Settings) -> dict[str, Any]:
    command = registration(settings)
    saved = run_frozen_decision_case(
        settings,
        exit_case(settings, "register", command),
        clock=GovernanceClock("2042-05-17T16:00:00Z"),
    )
    assert saved.report is not None
    return {
        "operation": "ASSESS_SECURITY",
        "contract_version": "1.0.0",
        "cutoff_at": "2042-05-18T16:00:00Z",
        "security_id": "XQZ-4017",
        "lifecycle_id": command["thesis"]["lifecycle_id"],
        "thesis_event_id": saved.decision_event_id,
        "market_state": "synthetic-steady",
        "board": "synthetic-board",
        "standard_price": "12",
        "calendar_version": "synthetic-session-calendar-v1",
        "evidence": [
            {
                "evidence_id": f"synthetic-{family.lower()}",
                "family": family,
                "security_id": "XQZ-4017",
                "source": "synthetic-original-disclosure",
                "authority": "DISCLOSURE" if family == "RISK" else "EXCHANGE",
                "source_version": "synthetic-security-evidence-v1",
                "public_at": "2042-05-18T15:00:00Z",
                "acquired_at": "2042-05-18T15:01:00Z",
                "validated_at": "2042-05-18T15:02:00Z",
                "valid_until": "2042-05-19T16:00:00Z",
                "complete": True,
                "metric": "licensed_operation_count"
                if family == "RISK"
                else "standard_price"
                if family == "MARKET"
                else "status",
                "value": "12" if family == "MARKET" else "1",
                "legal_termination": False,
            }
            for family in ("SECURITY", "MARKET", "RISK")
        ],
        "predictions": [],
    }


def test_missing_models_preserves_security_non_actionable_result_and_target_definitions(
    migrated_settings: Settings,
) -> None:
    command = assessment(migrated_settings)
    result = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, "assessment", command),
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )
    assert result.report is not None
    outcome = result.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert "MODEL_TARGETS_INCOMPLETE" in outcome["reasons"]
    assert outcome["targets"] == [
        {
            "target": "D20",
            "market_sessions": 20,
            "return_basis": "NET_TOTAL_RETURN",
            "aggregation": "DAILY_MINIMUM",
            "cash_return": "0",
        },
        {
            "target": "V60",
            "market_sessions": 60,
            "return_basis": "NET_TOTAL_RETURN",
            "aggregation": "TERMINAL_INCREMENT_VS_EXIT_CASH",
            "cash_return": "0",
        },
    ]
    assert outcome["standard_price"] == "12"
    assert not outcome["hard_gates"]


def qualified_assessment(
    settings: Settings, *, qualification_source: str = "EXTERNAL"
) -> dict[str, Any]:
    import json
    from hashlib import sha256

    from test_scoped_qualification import qualification_command, version

    command = assessment(settings)
    model = version(settings, "synthetic-exit-model-v1")
    command["version"] = model
    for target, loss, point in [
        ("D20", "0.05", "0.20"),
        ("D20", "0.10", "0.15"),
        ("D20", "0.15", "0.10"),
        ("D20", "0.20", "0.05"),
        ("V60", None, "0.75"),
    ]:
        artifact = {
            "target": target,
            "loss_boundary": loss,
            "market_state": command["market_state"],
            "band_lower": "0",
            "band_upper": "1",
            "lower_error": "0.03",
            "upper_error": "0.04",
            "version": model,
            "calendar_version": command["calendar_version"],
        }
        grant = qualification_command(settings)
        grant["version"] = model
        grant["scope"].update(
            capability="security-forward-exit",
            purpose="security-forward-assessment",
            target=target,
            probability_grid=loss or "POSITIVE",
            source=qualification_source,
            account_ids=["synthetic-account-4017", "synthetic-account-8029"],
            holding_age_domain="INITIAL",
        )
        grant["evidence"].update(
            version=model,
            scope=grant["scope"],
            market_calendar_version=command["calendar_version"],
            digest=sha256(
                json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        payload = case_payload(settings, f"grant-{target}-{loss}", grant)
        payload["access_scope"]["account_ids"] = grant["scope"]["account_ids"]
        saved = run_frozen_decision_case(
            settings, payload, clock=GovernanceClock("2042-05-17T16:00:00Z")
        )
        assert saved.report is not None and saved.report.result.governance is not None
        assert saved.report.result.governance.disposition == "APPROVED"
        command["predictions"].append(
            {
                "point": point,
                "produced_at": command["cutoff_at"],
                "valid_until": "2042-05-19T16:00:00Z",
                "qualification_scope": grant["scope"],
                "qualification_id": saved.decision_event_id,
                "calibration": artifact,
            }
        )
    digest = sha256(
        json.dumps(
            {key: value for key, value in command.items() if key != "predictions"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    for prediction in command["predictions"]:
        prediction.update(security_id=command["security_id"], assessment_digest=digest)
    return command


def test_independent_qualified_grid_and_hold_value_keep_points_guards_and_provenance(
    migrated_settings: Settings,
) -> None:
    command = qualified_assessment(migrated_settings)
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, "qualified", command),
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "ASSESSED", outcome
    assert [(p["target"], p["loss_boundary"]) for p in outcome["probabilities"]] == [
        ("D20", "0.05"),
        ("D20", "0.10"),
        ("D20", "0.15"),
        ("D20", "0.20"),
        ("V60", None),
    ]
    down, *_, value = outcome["probabilities"]
    assert (down["point"], down["guarded_lower"], down["guarded_upper"]) == ("0.20", "0.17", "0.24")
    assert (value["point"], value["guarded_lower"], value["guarded_upper"]) == (
        "0.75",
        "0.72",
        "0.79",
    )
    assert down["qualification_status"] == "VALID"
    assert value["version"] == command["version"]
    assert value["security_id"] == command["security_id"]
    assert value["assessment_digest"] == command["predictions"][-1]["assessment_digest"]
    assert down["produced_at"] == command["cutoff_at"]
    assert outcome["security_id"] == "XQZ-4017"
    assert outcome["evidence"] == command["evidence"]
    assert "quantity" not in str(outcome) and "cost_basis" not in str(outcome)


def test_authoritative_registered_falsification_survives_model_unavailability(
    migrated_settings: Settings,
) -> None:
    command = assessment(migrated_settings)
    command["evidence"][2]["value"] = "0"
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, "falsified", command),
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert outcome["hard_gates"] == [
        {
            "gate_id": "THESIS_FALSIFIED:synthetic-business-premise",
            "direction": "LIQUIDATE",
            "evidence_id": "synthetic-risk",
            "authority": "DISCLOSURE",
            "source": "synthetic-original-disclosure",
            "thesis_event_id": command["thesis_event_id"],
            "established_at": command["cutoff_at"],
        }
    ]
    assert "MODEL_TARGETS_INCOMPLETE" in outcome["reasons"]
    assert outcome["thesis_event_id"] == command["thesis_event_id"]


def test_data_late_to_cutoff_removes_statistical_guards_without_using_earlier_probabilities(
    migrated_settings: Settings,
) -> None:
    command = qualified_assessment(migrated_settings)
    command["evidence"][0]["validated_at"] = "2042-05-18T16:00:01Z"
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, "late-security", command),
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert "SECURITY_EVIDENCE_INCOMPLETE" in outcome["reasons"]
    assert all(
        p["guarded_lower"] is None and p["guarded_upper"] is None for p in outcome["probabilities"]
    )


def test_exchange_legal_termination_is_independent_of_thesis_and_probabilities(
    migrated_settings: Settings,
) -> None:
    command = assessment(migrated_settings)
    command["thesis_event_id"] = None
    command["evidence"][0]["legal_termination"] = True
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, "legal", command),
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["hard_gates"][0]["gate_id"] == "LEGAL_TERMINATION"
    assert outcome["hard_gates"][0]["evidence_id"] == "synthetic-security"
    assert outcome["hard_gates"][0]["thesis_event_id"] is None
    assert outcome["disposition"] == "NON_ACTIONABLE"


def test_late_registration_cannot_backdate_a_historical_liquidation_reason(
    migrated_settings: Settings,
) -> None:
    command = registration(migrated_settings)
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, "backdated", command),
        clock=GovernanceClock("2042-05-20T16:00:00Z"),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "BLOCKED"
    assert outcome["reasons"] == ["THESIS_REGISTRATION_NOT_FORWARD"]
    assert outcome["thesis"] is None and not outcome["hard_gates"]


def test_qualified_system_source_cannot_authorize_external_position_assessment(
    migrated_settings: Settings,
) -> None:
    command = qualified_assessment(migrated_settings, qualification_source="SYSTEM")
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, "wrong-origin", command),
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert all(p["qualification_status"] == "NOT_QUALIFIED" for p in outcome["probabilities"])
    assert "QUALIFICATION_SCOPE_MISMATCH" in outcome["reasons"]


@pytest.mark.parametrize(
    "defect",
    [
        "missing-family",
        "wrong-authority",
        "conflicting-risk",
        "quote-mismatch",
        "expired-thesis",
        "incoherent-grid",
    ],
)
def test_incomplete_or_conflicting_security_inputs_cannot_be_actionable(
    migrated_settings: Settings,
    defect: str,
) -> None:
    command = qualified_assessment(migrated_settings)
    if defect == "missing-family":
        command["evidence"].pop()
    elif defect == "wrong-authority":
        command["evidence"][0]["authority"] = "NEWS"
    elif defect == "conflicting-risk":
        from copy import deepcopy

        conflicting = deepcopy(command["evidence"][2])
        conflicting.update(evidence_id="synthetic-conflict", value="0")
        command["evidence"].append(conflicting)
    elif defect == "quote-mismatch":
        command["standard_price"] = "900"
    elif defect == "expired-thesis":
        command["cutoff_at"] = "2042-11-18T16:00:00Z"
        for evidence in command["evidence"]:
            evidence["valid_until"] = "2042-11-19T16:00:00Z"
        for prediction in command["predictions"]:
            prediction.update(produced_at=command["cutoff_at"], valid_until="2042-11-19T16:00:00Z")
    elif defect == "incoherent-grid":
        command["predictions"][3]["point"] = "0.9"
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, f"defect-{defect}", command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert not outcome["hard_gates"]
    assert all(
        p["guarded_lower"] is None
        for p in outcome["probabilities"]
        if defect != "incoherent-grid" or p["target"] == "D20"
    )


@pytest.mark.parametrize("origin", ["SYSTEM", "EXTERNAL"])
def test_authoritative_origin_is_frozen_and_cannot_be_replaced(
    migrated_settings: Settings,
    origin: str,
) -> None:
    snapshot = position_snapshot_command()
    snapshot["accounts"][0]["positions"][0]["origin"] = origin
    original_position = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, f"origin-{origin}", snapshot),
        clock=GovernanceClock(),
    )
    assert original_position.report is not None
    command = registration(migrated_settings)
    command["position_event_id"] = original_position.decision_event_id
    command["thesis"]["origin"] = origin
    first = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, f"origin-register-{origin}", command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert first.report is not None
    assert (
        first.report.result.model_dump(mode="json")["exit_assessment"]["disposition"]
        == "THESIS_REGISTERED"
    )
    command["thesis"]["propositions"][0]["threshold"] = "99"
    replacement = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, f"rewrite-{origin}", command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert replacement.report is not None
    assert replacement.report.result.model_dump(mode="json")["exit_assessment"]["reasons"] == [
        "THESIS_ALREADY_FROZEN"
    ]


@pytest.mark.parametrize(
    "defect",
    [
        "old-probability",
        "stale-probability",
        "overall-state",
        "wrong-model",
        "altered-envelope",
        "missing-grid",
    ],
)
def test_probability_gaps_remain_separate_without_fallback(
    migrated_settings: Settings,
    defect: str,
) -> None:
    from copy import deepcopy

    command = deepcopy(qualified_assessment(migrated_settings))
    prediction = command["predictions"][0]
    if defect == "old-probability":
        prediction["produced_at"] = "2042-05-17T16:00:00Z"
    elif defect == "stale-probability":
        prediction["valid_until"] = "2042-05-18T15:59:59Z"
    elif defect == "overall-state":
        prediction["qualification_scope"]["market_state"] = "OVERALL"
    elif defect == "wrong-model":
        prediction["calibration"]["version"] = {
            **prediction["calibration"]["version"],
            "version_id": "synthetic-other-model",
        }
    elif defect == "altered-envelope":
        prediction["calibration"]["upper_error"] = "0"
    elif defect == "missing-grid":
        command["predictions"].pop(0)
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, f"probability-gap-{defect}", command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert outcome["probabilities"][-1]["qualification_status"] == "VALID"
    if defect != "missing-grid":
        assert outcome["probabilities"][0]["guarded_upper"] is None
    assert not outcome["hard_gates"]


@pytest.mark.parametrize("authority", ["MODEL", "NEWS"])
def test_llm_and_news_cannot_falsify_a_thesis_or_declare_legal_termination(
    migrated_settings: Settings,
    authority: str,
) -> None:
    command = assessment(migrated_settings)
    command["evidence"][2].update(authority=authority, value="0")
    command["evidence"][0].update(authority=authority, legal_termination=True)
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, f"advisory-{authority}", command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert not outcome["hard_gates"]
    assert outcome["disposition"] == "NON_ACTIONABLE"


def test_cli_and_authenticated_api_read_the_same_saved_security_result(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    import sys

    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "api")
    from test_authenticated_report_delivery import _authenticated_client_with_csrf

    from stock_profiler.entrypoints.cli import main
    from stock_profiler.foundation.clock import UtcClock

    command = qualified_assessment(migrated_settings)
    payload = exit_case(migrated_settings, "delivery", command)
    case_path = tmp_path / "synthetic-exit-case.json"
    case_path.write_text(json.dumps(payload))
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)
    monkeypatch.setattr(UtcClock, "now", lambda self: GovernanceClock(command["cutoff_at"]).now())
    monkeypatch.setattr(
        sys, "argv", ["stock-profiler", "decision-case-run", "--case", str(case_path)]
    )
    main()
    first = json.loads(capsys.readouterr().out)
    assert first["report"]["result"]["exit_assessment"]["disposition"] == "ASSESSED"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stock-profiler",
            "decision-case-replay",
            "--business-identity",
            payload["business_identity"],
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out) == first
    settings = migrated_settings.model_copy(
        update={"report_account_ids": ("synthetic-account-4017", "synthetic-account-8029")}
    )
    client, _ = _authenticated_client_with_csrf(settings, monkeypatch)
    response = client.get(f"/api/v1/reports/{first['report_version_id']}")
    assert response.status_code == 200
    assert response.json() == first["report"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("registered", [False, True])
def test_historical_bad_results_cannot_create_retrospective_thesis_gates(
    migrated_settings: Settings, registered: bool
) -> None:
    command = assessment(migrated_settings)
    if not registered:
        command["thesis_event_id"] = None
    command["evidence"][2].update(public_at="2042-05-16T15:00:00Z", value="0")
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, f"historical-bad-result-{registered}", command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert not outcome["hard_gates"]
    assert outcome["disposition"] == "NON_ACTIONABLE"


@pytest.mark.parametrize("field", ["cost_basis", "quantity", "account_route", "risk_budget"])
def test_security_assessment_rejects_personal_decision_inputs(
    migrated_settings: Settings, field: str
) -> None:
    from pydantic import ValidationError

    command = assessment(migrated_settings)
    command[field] = "synthetic-personal-value"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        run_frozen_decision_case(
            migrated_settings,
            exit_case(migrated_settings, f"personal-input-{field}", command),
            clock=GovernanceClock(command["cutoff_at"]),
        )


@pytest.mark.parametrize("business", ["result-abstained", "result-failed"])
@pytest.mark.parametrize(
    "gate", ["LEGAL_TERMINATION", "THESIS_FALSIFIED:synthetic-business-premise"]
)
def test_business_non_success_keeps_independent_authoritative_liquidation_gates(
    migrated_settings: Settings, business: str, gate: str
) -> None:
    import json
    from pathlib import Path

    command = qualified_assessment(migrated_settings)
    if gate == "LEGAL_TERMINATION":
        command["evidence"][0]["legal_termination"] = True
    else:
        command["evidence"][2]["value"] = "0"
    payload = exit_case(migrated_settings, f"{business}-{gate}", command)
    source = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/synthetic/result-families" / f"{business}.json"
        ).read_text()
    )
    payload["input"] = {**source["input"], "exit_assessment": command}
    payload["expected_external_result"] = source["expected_external_result"]
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(command["cutoff_at"])
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert [item["gate_id"] for item in outcome["hard_gates"]] == [gate]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert "BUSINESS_PREREQUISITE_FAILED" in outcome["reasons"]
    assert all(item["guarded_lower"] is None for item in outcome["probabilities"])


@pytest.mark.parametrize("changed_input", ["standard-price", "risk-evidence"])
def test_predictions_cannot_be_transplanted_to_another_assessment_basis(
    migrated_settings: Settings, changed_input: str
) -> None:
    command = qualified_assessment(migrated_settings)
    if changed_input == "standard-price":
        command["standard_price"] = command["evidence"][1]["value"] = "13"
    else:
        command["evidence"][2]["value"] = "2"
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, f"transplanted-{changed_input}", command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert saved.report is not None
    outcome = saved.report.result.model_dump(mode="json")["exit_assessment"]
    assert outcome["disposition"] == "NON_ACTIONABLE"
    assert "PREDICTION_ASSESSMENT_MISMATCH" in outcome["reasons"]
    assert all(item["guarded_lower"] is None for item in outcome["probabilities"])
