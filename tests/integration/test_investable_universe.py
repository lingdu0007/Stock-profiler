from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from alembic import command as migration
from alembic.config import Config
from test_scoped_qualification import (
    GovernanceClock,
    case_payload,
    qualification_command,
    scope,
    version,
)

from stock_profiler.bootstrap.decision_cases import (
    get_formal_report,
    replay_default_frozen_decision_case,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.delivery.access import AccessPrincipal


def universe_payload(settings: Settings) -> dict[str, Any]:
    payload = case_payload(settings, "universe", {}, contract_version="universe.1.0.0")
    payload.pop("governance")
    payload["knowledge_cutoff"] = "2042-05-30T23:59:59+08:00"
    payload["report_generated_at"] = "2042-05-31T00:01:00+08:00"
    payload["universe"] = {
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "fictional-universe/1",
        "seed": 731,
        "cutoff_at": payload["knowledge_cutoff"],
        "purpose": "SYNTHETIC",
        "market_state": "synthetic-steady",
        "account_ids": ["synthetic-account-4017"],
        "security_inventory": ["XQZ-UNIVERSE-731"],
        "calendar": {
            "version_id": "synthetic-month-calendar-v1",
            "days": [
                {
                    "market_date": f"2042-05-{day:02}",
                    "close_at": f"2042-05-{day:02}T15:00:00+08:00" if day <= 30 else None,
                }
                for day in range(1, 32)
            ],
        },
        "policy": {
            "version_id": "synthetic-universe-policy-v1",
            "minimum_listing_months": 6,
            "turnover_sessions": 4,
            "minimum_median_turnover": "20000",
            "maximum_participation": "0.02",
        },
        "securities": [
            {
                "security_id": "XQZ-UNIVERSE-731",
                "account_id": "synthetic-account-4017",
                "board": "SH_MAIN",
                "asset_type": "RMB_ORDINARY_SHARE",
                "listed_on": "2041-01-01",
                "risk_warning": False,
                "delisting": False,
                "suspended": False,
                "daily_turnover": ["20000", "22000", "18000", "20000"],
                "turnover_dates": ["2042-05-27", "2042-05-28", "2042-05-29", "2042-05-30"],
                "unit_price": "10",
                "minimum_quantity": 30,
                "quantity_increment": 10,
                "acquisition_fixed_cost": "0",
                "acquisition_cost_ratio": "0",
                "rules_valid_from": "2042-01-01T00:00:00+08:00",
                "rules_valid_until": "2042-12-31T23:59:59+08:00",
                "planned_amount": "400",
                "minimum_unit_cost": "300",
                "available_budget": "500",
            }
        ],
    }
    payload["universe"]["manifest"] = manifest(payload["universe"])
    return payload


def test_monthly_universe_is_a_committed_replayable_host_result(
    migrated_settings: Settings,
) -> None:
    payload = universe_payload(migrated_settings)
    clock = GovernanceClock("2042-05-30T16:02:00Z")
    execution = run_frozen_decision_case(migrated_settings, payload, clock=clock)
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "FROZEN"
    assert result["members"] == ["XQZ-UNIVERSE-731"]
    assert result["exclusions"] == []
    assert result["qualification_scope"] == "D0_SYNTHETIC_CONTRACT_ONLY"
    assert execution.business_commit_status == "COMMITTED"
    assert run_frozen_decision_case(migrated_settings, payload, clock=clock) == execution


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"asset_type": "FUND"}, "UNSUPPORTED_ASSET"),
        ({"risk_warning": True}, "RISK_WARNING"),
        ({"delisting": True}, "DELISTING"),
        ({"suspended": True}, "SUSPENDED"),
        ({"listed_on": "2041-12-01"}, "LISTING_IMMATURE"),
        ({"daily_turnover": ["19999"] * 4}, "INSUFFICIENT_LIQUIDITY"),
        ({"planned_amount": "400.01"}, "PARTICIPATION_LIMIT"),
        ({"available_budget": "299.99"}, "UNAFFORDABLE_UNIT"),
        ({"board": "STAR"}, "BOARD_NOT_ENABLED"),
    ],
)
def test_universe_retains_explicit_security_exclusions(
    migrated_settings: Settings, changes: dict[str, Any], reason: str
) -> None:
    payload = universe_payload(migrated_settings)
    payload["universe"]["securities"][0].update(changes)
    payload["universe"]["manifest"] = manifest(payload["universe"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "FROZEN"
    assert result["members"] == []
    assert reason in result["exclusions"][0]["reasons"]


@pytest.mark.parametrize("turnover", [[], ["20000"] * 3, ["20000", "-1", "20000", "20000"]])
def test_incomplete_market_window_fails_the_whole_month(
    migrated_settings: Settings, turnover: list[str]
) -> None:
    payload = universe_payload(migrated_settings)
    payload["universe"]["securities"][0]["daily_turnover"] = turnover
    payload["universe"]["manifest"] = manifest(payload["universe"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "DATA_FAILED"
    assert result["members"] == []
    assert "MARKET_WINDOW_INCOMPLETE" in result["reasons"]
    assert report.result.outcome_code == "UNIVERSE_DATA_FAILED"
    assert any(
        stage.phase == "BUSINESS_DECISION" and stage.status == "FAILED"
        for stage in report.stage_results
    )


def test_real_candidate_purpose_cannot_inherit_synthetic_entitlements(
    migrated_settings: Settings,
) -> None:
    payload = universe_payload(migrated_settings)
    payload["universe"]["purpose"] = "REAL_CANDIDATE"
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "BLOCKED"
    assert result["members"] == []
    assert result["actionable"] is False
    assert "REAL_ENTITLEMENTS_REQUIRED" in result["reasons"]


@pytest.mark.parametrize("defect", ["missing", "late", "conflict", "license", "cutoff"])
def test_universe_requires_on_time_licensed_manifest_and_month_end_calendar(
    migrated_settings: Settings, defect: str
) -> None:
    payload = universe_payload(migrated_settings)
    if defect == "missing":
        payload["universe"].pop("manifest", None)
    elif defect == "cutoff":
        payload["knowledge_cutoff"] = "2042-05-29T23:59:59+08:00"
        payload["universe"]["cutoff_at"] = payload["knowledge_cutoff"]
    else:
        payload["universe"]["manifest"] = manifest(payload["universe"])
        evidence = payload["universe"]["manifest"]["entries"][0]["evidence"]
        if defect == "late":
            evidence["validated_at"] = "2042-05-31T00:00:00+08:00"
        elif defect == "conflict":
            evidence["conflict"] = True
        else:
            evidence["retention_permitted"] = False
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "DATA_FAILED"
    assert result["members"] == []


def manifest(command: dict[str, Any]) -> dict[str, Any]:
    payloads = {
        "INVENTORY": command["security_inventory"],
        "CALENDAR": command.get("calendar"),
        "ENTITLEMENTS": command.get("entitlements", []),
        **{
            family: [{key: security[key] for key in keys} for security in command["securities"]]
            for family, keys in {
                "SECURITIES": [
                    "security_id",
                    "board",
                    "asset_type",
                    "listed_on",
                    "risk_warning",
                    "delisting",
                    "suspended",
                ],
                "MARKET": ["security_id", "daily_turnover", "turnover_dates", "unit_price"],
                "RULES": [
                    "security_id",
                    "board",
                    "minimum_quantity",
                    "quantity_increment",
                    "rules_valid_from",
                    "rules_valid_until",
                ],
                "AFFORDABILITY": [
                    "security_id",
                    "planned_amount",
                    "minimum_unit_cost",
                    "available_budget",
                    "account_id",
                    "acquisition_fixed_cost",
                    "acquisition_cost_ratio",
                ],
            }.items()
        },
    }
    result: dict[str, Any] = {
        "version_id": "synthetic-universe-manifest-v1",
        "entries": [],
    }
    for family, data in payloads.items():
        content = json.dumps(data, sort_keys=True, separators=(",", ":"))
        result["entries"].append(
            {
                "field_family": family,
                "requirement": "REQUIRED",
                "semantics_version": "synthetic-security-semantics-v1",
                "primary_source": "fictional-exchange-731",
                "evidence": {
                    "evidence_id": f"synthetic-{family}-731",
                    "source": "fictional-exchange-731",
                    "source_version": "synthetic-source-v1",
                    "authority": "BROKER"
                    if family in {"ENTITLEMENTS", "AFFORDABILITY"}
                    else "EXCHANGE",
                    "license_id": "synthetic-license-v1",
                    "retention_permitted": True,
                    "complete": True,
                    "conflict": False,
                    "fact_effective_at": "2042-05-30T15:00:00+08:00",
                    "source_published_at": "2042-05-30T15:01:00+08:00",
                    "source_observed_at": "2042-05-30T15:02:00+08:00",
                    "acquired_at": "2042-05-30T15:03:00+08:00",
                    "validated_at": "2042-05-30T15:04:00+08:00",
                    "complete_through": command["cutoff_at"],
                    "content": content,
                    "content_sha256": sha256(content.encode()).hexdigest(),
                },
            }
        )
    return result


@pytest.mark.parametrize(
    "cutoff",
    [
        "2042-05-30T23:59:58+08:00",
        "2042-05-29T23:59:59+08:00",
        "2042-05-31T23:59:59+08:00",
    ],
)
def test_knowledge_cutoff_must_be_the_calendar_last_session(
    migrated_settings: Settings, cutoff: str
) -> None:
    payload = universe_payload(migrated_settings)
    payload["knowledge_cutoff"] = cutoff
    command = payload["universe"]
    command["cutoff_at"] = cutoff
    command["calendar"] = {
        "version_id": "synthetic-month-calendar-v1",
        "days": [
            {
                "market_date": f"2042-05-{day:02}",
                "close_at": f"2042-05-{day:02}T15:00:00+08:00" if day <= 30 else None,
            }
            for day in range(1, 32)
        ],
    }
    command["manifest"] = manifest(command)
    evidence = command["manifest"]["entries"][0]["evidence"]
    for field in (
        "fact_effective_at",
        "source_published_at",
        "source_observed_at",
        "acquired_at",
        "validated_at",
    ):
        evidence[field] = "2042-05-01T15:00:00+08:00"
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-06-01T00:00:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "DATA_FAILED"
    assert "MONTHLY_CUTOFF_INVALID" in result["reasons"]


@pytest.mark.parametrize(
    "state,observed,disposition,reason",
    [
        ("DENIED", "2042-05-30T15:02:00+08:00", "FROZEN", "ACCOUNT_PERMISSION_DENIED"),
        ("UNKNOWN", "2042-05-30T15:02:00+08:00", "DATA_FAILED", "ACCOUNT_PERMISSION_UNKNOWN"),
        ("GRANTED", "2042-04-30T16:00:00+08:00", "DATA_FAILED", "ENTITLEMENTS_STALE"),
    ],
)
def test_actual_entitlements_override_the_conservative_baseline(
    migrated_settings: Settings, state: str, observed: str, disposition: str, reason: str
) -> None:
    payload = universe_payload(migrated_settings)
    command = payload["universe"]
    command["entitlements"] = [
        {
            "account_id": "synthetic-account-4017",
            "snapshot_at": observed,
            "permissions": [{"board": "SH_MAIN", "state": state, "risk_disclosure": True}],
        }
    ]
    command["manifest"] = manifest(command)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == disposition
    assert result["members"] == []
    reasons = (
        result["reasons"] if disposition == "DATA_FAILED" else result["exclusions"][0]["reasons"]
    )
    assert reason in reasons


def test_security_master_alone_cannot_attest_calendar_or_account_facts(
    migrated_settings: Settings,
) -> None:
    payload = universe_payload(migrated_settings)
    payload["universe"]["manifest"]["entries"] = [
        entry
        for entry in payload["universe"]["manifest"]["entries"]
        if entry["field_family"] == "SECURITIES"
    ]
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "DATA_FAILED"
    assert "MANIFEST_COVERAGE_FAILED" in result["reasons"]


@pytest.mark.parametrize("defect", ["dates", "cost", "account", "rule"])
def test_universe_needs_account_local_legality_and_dated_market_evidence(
    migrated_settings: Settings, defect: str
) -> None:
    payload = universe_payload(migrated_settings)
    security = payload["universe"]["securities"][0]
    security.update(
        account_id="synthetic-account-4017",
        turnover_dates=["2042-05-27", "2042-05-28", "2042-05-29", "2042-05-30"],
        unit_price="10",
        minimum_quantity=30,
        quantity_increment=10,
        acquisition_fixed_cost="0",
        acquisition_cost_ratio="0",
        rules_valid_from="2042-01-01T00:00:00+08:00",
        rules_valid_until="2042-12-31T23:59:59+08:00",
    )
    if defect == "dates":
        security["turnover_dates"][0] = "2042-05-26"
    elif defect == "cost":
        security["acquisition_fixed_cost"] = "1"
    elif defect == "account":
        security["account_id"] = "synthetic-uncovered-account"
    else:
        security["rules_valid_until"] = "2042-05-29T00:00:00+08:00"
    payload["universe"]["manifest"] = manifest(payload["universe"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "DATA_FAILED"
    assert result["members"] == []


@pytest.mark.parametrize("board", ["CHINEXT", "STAR", "BSE"])
def test_each_extended_board_uses_its_own_saved_qualification(
    migrated_settings: Settings, board: str
) -> None:
    payload = universe_payload(migrated_settings)
    command = payload["universe"]
    command["market_state"] = "synthetic-steady"
    command["securities"][0]["board"] = board
    command["entitlements"] = [
        {
            "account_id": "synthetic-account-4017",
            "snapshot_at": "2042-05-30T15:02:00+08:00",
            "permissions": [{"board": board, "state": "GRANTED", "risk_disclosure": True}],
        }
    ]
    command["manifest"] = manifest(command)
    board_scope = scope()
    board_scope.update(
        capability="INVESTABLE_UNIVERSE",
        purpose="SYNTHETIC",
        board=board,
        target="MEMBERSHIP",
        source=command["manifest"]["version_id"],
    )
    bundle = version(migrated_settings, command["policy"]["version_id"])
    bundle["implementation"] = payload["version_bundle"]
    grant = qualification_command(migrated_settings)
    grant.update(scope=board_scope, version=bundle)
    grant["evidence"].update(scope=board_scope, version=bundle)
    granted = run_frozen_decision_case(
        migrated_settings,
        case_payload(migrated_settings, f"board-{board}", grant),
        clock=GovernanceClock(),
    )
    assert granted.report is not None
    assert granted.report.result.governance is not None
    assert granted.report.result.governance.disposition == "APPROVED"
    command["board_qualifications"] = [{"scope": board_scope, "version": bundle}]
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["universe"]
    assert result["members"] == ["XQZ-UNIVERSE-731"]
    assert result["board_qualifications"][0]["decision_id"] == granted.decision_event_id


@pytest.mark.parametrize("certified", [True, False])
def test_alternate_source_requires_preregistered_equivalence(
    migrated_settings: Settings, certified: bool
) -> None:
    payload = universe_payload(migrated_settings)
    entry = payload["universe"]["manifest"]["entries"][0]
    authority = deepcopy(entry["evidence"])
    entry["evidence"].update(source="fictional-alternate-731", authority="CERTIFIED_DELIVERY")
    if certified:
        entry["substitution"] = {
            "certification_id": "synthetic-substitution-731",
            "registered_at": "2042-04-01T00:00:00+08:00",
            "valid_until": "2042-12-01T00:00:00+08:00",
            "primary_source": entry["primary_source"],
            "alternate_source": "fictional-alternate-731",
            "alternate_version": "synthetic-source-v1",
            "field_family": entry["field_family"],
            "manifest_version": payload["universe"]["manifest"]["version_id"],
            "semantics_version": entry["semantics_version"],
            "purpose": "SYNTHETIC",
            "reason": "PRIMARY_UNAVAILABLE",
            "checks": ["SEMANTICS", "LICENSE", "COMPLETENESS", "REPLAY", "SHADOW"],
            "authority_evidence": authority,
        }
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == ("FROZEN" if certified else "DATA_FAILED")
    assert result["manifest"]["entries"][0]["evidence"]["source"] == "fictional-alternate-731"


def test_month_identity_cannot_be_replaced_after_late_evidence(
    migrated_settings: Settings,
) -> None:
    payload = universe_payload(migrated_settings)
    payload["universe"]["manifest"]["entries"][0]["evidence"]["validated_at"] = (
        "2042-05-31T00:00:00+08:00"
    )
    clock = GovernanceClock("2042-05-30T16:02:00Z")
    failed = run_frozen_decision_case(migrated_settings, payload, clock=clock)
    assert failed.report is not None
    corrected = universe_payload(migrated_settings)
    corrected["business_identity"] = "synthetic:late-universe-retry"
    corrected["case_id"] = "synthetic-late-universe-retry"
    replay = run_frozen_decision_case(migrated_settings, corrected, clock=clock)
    assert replay == failed


@pytest.mark.parametrize("defect", ["omitted", "duplicate", "unknown-risk", "unknown-listing"])
def test_unexplained_security_inventory_gaps_fail_the_month(
    migrated_settings: Settings, defect: str
) -> None:
    payload = universe_payload(migrated_settings)
    command = payload["universe"]
    command["security_inventory"] = ["XQZ-UNIVERSE-731"]
    if defect == "omitted":
        command["security_inventory"].append("XQZ-UNIVERSE-MISSING")
    elif defect == "duplicate":
        command["securities"].append(deepcopy(command["securities"][0]))
    elif defect == "unknown-risk":
        command["securities"][0]["risk_warning"] = None
    else:
        command["securities"][0]["listed_on"] = None
    command["manifest"] = manifest(command)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "DATA_FAILED"
    assert result["members"] == []


@pytest.mark.parametrize(
    "family,changes",
    [
        ("MARKET", {"fact_effective_at": "2042-04-30T15:00:00+08:00"}),
        ("SECURITIES", {"fact_effective_at": "2042-04-30T15:00:00+08:00"}),
        (
            "ENTITLEMENTS",
            {
                "source_observed_at": "2042-05-30T16:00:00+08:00",
                "acquired_at": "2042-05-30T15:00:00+08:00",
            },
        ),
    ],
)
def test_completeness_claim_cannot_override_stale_or_impossible_clocks(
    migrated_settings: Settings, family: str, changes: dict[str, Any]
) -> None:
    payload = universe_payload(migrated_settings)
    entry = next(
        entry
        for entry in payload["universe"]["manifest"]["entries"]
        if entry["field_family"] == family
    )
    entry["evidence"].update(changes)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    assert report.result.model_dump(mode="json")["universe"]["disposition"] == "DATA_FAILED"


def test_known_permissions_can_be_exercised_without_granting_real_authority(
    migrated_settings: Settings,
) -> None:
    payload = universe_payload(migrated_settings)
    command = payload["universe"]
    command["purpose"] = "REAL_CANDIDATE"
    command["entitlements"] = [
        {
            "account_id": "synthetic-account-4017",
            "snapshot_at": "2042-05-30T15:02:00+08:00",
            "permissions": [{"board": "SH_MAIN", "state": "GRANTED", "risk_disclosure": True}],
        }
    ]
    command["manifest"] = manifest(command)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    ).report
    assert report is not None
    result = report.result.model_dump(mode="json")["universe"]
    assert result["disposition"] == "FROZEN"
    assert result["members"] == ["XQZ-UNIVERSE-731"]
    assert result["actionable"] is False
    assert result["qualification_scope"] == "D0_SYNTHETIC_CONTRACT_ONLY"


def test_universe_rebuild_and_authenticated_projection_preserve_the_same_facts(
    migrated_settings: Settings, tmp_path: Path
) -> None:
    snapshots = []
    for repetition in range(2):
        directory = tmp_path / f"universe-{repetition}"
        directory.mkdir()
        settings = migrated_settings.model_copy(
            update={
                "app_database_url": f"sqlite:///{directory / 'application.sqlite3'}",
                "m_agent_run_store_path": directory / "framework.sqlite3",
            }
        )
        configuration = Config("alembic.ini")
        configuration.set_main_option("sqlalchemy.url", settings.app_database_url)
        migration.upgrade(configuration, "head")
        payload = universe_payload(settings)
        clock = GovernanceClock("2042-05-30T16:02:00Z")
        execution = run_frozen_decision_case(settings, payload, clock=clock)
        assert execution.report is not None
        principal = AccessPrincipal(
            user_id="stock-profiler-single-user",
            account_ids=("synthetic-account-4017",),
            permissions=("REPORT_READ",),
        )
        assert (
            get_formal_report(execution.report_version_id, settings, principal=principal)
            == execution.report
        )
        assert (
            get_formal_report(
                execution.report_version_id,
                settings,
                principal=principal.model_copy(update={"user_id": "synthetic-other-owner"}),
            )
            is None
        )
        assert (
            replay_default_frozen_decision_case(
                settings,
                payload["business_identity"],
                clock=clock,
                recovery_case=FrozenDecisionCase.model_validate(payload),
            )
            == execution
        )
        snapshots.append(execution.model_dump(mode="json"))
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize(
    "fixture,status",
    [
        ("input-rejected", "REJECTED"),
        ("result-failed", "FAILED"),
        ("result-abstained", "ABSTAINED"),
    ],
)
def test_universe_cannot_turn_an_unsuccessful_business_prerequisite_into_success(
    migrated_settings: Settings, fixture: str, status: str
) -> None:
    payload = universe_payload(migrated_settings)
    original = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/synthetic/result-families" / f"{fixture}.json"
        ).read_text()
    )
    payload["input"] = original["input"]
    payload["expected_external_result"] = original["expected_external_result"]
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    )
    assert execution.business_result_status == status
    assert execution.report is not None
    assert execution.report.result.universe is not None
    assert execution.report.result.universe.members == ()
    assert execution.report.result.universe.disposition == "BLOCKED"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"content_sha256": "0" * 64}, "CONTENT_INTEGRITY_FAILED"),
        ({"content": "{"}, "CONTENT_INVALID"),
        ({"content": "[]"}, "SOURCE_FACT_MISMATCH"),
        ({"source_observed_at": None}, "EVIDENCE_NOT_AVAILABLE"),
        ({"complete": False}, "SEMANTIC_COMPLETENESS_FAILED"),
        ({"fact_effective_at": "2042-05-31T00:00:00+08:00"}, "FUTURE_FACT"),
        ({"authority": "EXPLORATORY"}, "FACT_AUTHORITY_MISMATCH"),
    ],
)
def test_manifest_claims_do_not_replace_original_evidence(
    migrated_settings: Settings, changes: dict[str, Any], reason: str
) -> None:
    payload = universe_payload(migrated_settings)
    payload["universe"]["manifest"]["entries"][0]["evidence"].update(changes)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.universe
    assert result is not None
    assert result.disposition == "DATA_FAILED"
    assert result.members == ()
    assert reason in result.reasons


@pytest.mark.parametrize("defect", ["downgraded", "missing", "unsupported", "duplicate"])
def test_manifest_schema_cannot_hide_required_evidence(
    migrated_settings: Settings, defect: str
) -> None:
    payload = universe_payload(migrated_settings)
    entries = payload["universe"]["manifest"]["entries"]
    if defect == "downgraded":
        entries[0]["requirement"] = "EXPLORATORY"
    elif defect == "missing":
        entries[0]["evidence"] = None
    elif defect == "unsupported":
        extra = deepcopy(entries[0])
        extra["field_family"] = "SYNTHETIC_UNSUPPORTED"
        entries.append(extra)
    else:
        entries.append(deepcopy(entries[0]))
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    )
    assert execution.report is not None
    assert execution.report.result.universe is not None
    assert execution.report.result.universe.disposition == "DATA_FAILED"


def test_entitlement_snapshot_cannot_postdate_its_evidence_acquisition(
    migrated_settings: Settings,
) -> None:
    payload = universe_payload(migrated_settings)
    command = payload["universe"]
    command["entitlements"] = [
        {
            "account_id": "synthetic-account-4017",
            "snapshot_at": "2042-05-30T16:00:00+08:00",
            "permissions": [{"board": "SH_MAIN", "state": "GRANTED", "risk_disclosure": True}],
        }
    ]
    command["manifest"] = manifest(command)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:02:00Z")
    )
    assert execution.report is not None
    assert execution.report.result.universe is not None
    assert execution.report.result.universe.disposition == "DATA_FAILED"
    assert "ENTITLEMENTS_EVIDENCE_CLOCK" in execution.report.result.universe.reasons
