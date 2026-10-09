"""Original D0 prior facts supplied through the real ledger storage adapter."""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from synthetic_candidate_workspace import failed_candidate_case
from test_execution_plans import risk_handoff_payload
from test_position_state_reconciliation import position_evidence

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.candidate_selection.calibrated_candidates import CandidateReleaseOutcome
from stock_profiler.modules.decision_cases.domain import (
    ExternalResult,
    FormalReport,
    FrozenDecisionCase,
    StageResult,
    load_frozen_decision_case,
)
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar
from stock_profiler.modules.qualification.contracts import (
    CapabilityVersion,
    GovernanceOutcome,
    QualificationEvidence,
    QualificationPolicy,
    QualificationRecord,
    QualificationScope,
)


def save_qualification_prior(
    settings: Settings, source_case: FrozenDecisionCase, record: QualificationRecord
) -> None:
    """Retain an original D0 qualification fact in the same scoped event ledger."""
    from test_scoped_qualification import GovernanceClock

    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    payload.update(business_identity=record.decision_id, case_id=record.decision_id)
    assert source_case.access_scope is not None
    payload["access_scope"] = source_case.access_scope.model_dump(mode="json")
    payload["version_bundle"].update(
        case_contract_version="3.0.0",
        host_contract_version="3.0.0",
        report_projection_contract_version="3.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    case = FrozenDecisionCase.model_validate(payload)
    ledger = DecisionLedger.from_settings(
        settings, clock=GovernanceClock(record.recorded_at.isoformat())
    )
    ledger.persist_business_mapping_before_framework(case)
    with ledger.serialize_case_execution() as connection:
        ledger.commit_event(
            connection,
            case=case,
            framework_run_id=case.framework_run_id,
            result=ExternalResult(
                outcome_code="QUALIFICATION_PRIOR_FACT",
                summary="Original D0 qualification prior fact.",
                key_reasons=(record.cause,),
                governance=GovernanceOutcome(
                    disposition="APPROVED", reasons=(record.cause,), qualification=record
                ),
            ),
            stage_results=(),
            committed_at=record.recorded_at.isoformat(),
        )


def qualified_prior_record(
    case: FrozenDecisionCase, release: CandidateReleaseOutcome
) -> QualificationRecord:
    assert case.access_scope is not None and release.qualification is not None
    scope = QualificationScope(
        capability="candidate-release",
        purpose="CANDIDATE_BUY",
        evidence_level="D0",
        user_id=case.access_scope.user_id,
        account_ids=case.access_scope.account_ids,
        account_type="SYNTHETIC",
        source="SYNTHETIC_D0",
        market_state=release.market_state,
        board="SYNTHETIC",
        target="SIX_MONTH_TERMINAL_20_PERCENT",
    )
    policy = QualificationPolicy(
        contract_version="1.0.0",
        policy_version="synthetic-allocation-qualification-policy",
        synthetic=True,
        generator_version="allocation-qualification-v1",
        seed=2323,
        evaluation_max_age_months=12,
        state_activity_max_age_months=12,
        require_state_activity=False,
    )
    version = CapabilityVersion(
        version_id=release.capability_version,
        policy_version=policy.policy_version,
        implementation=case.version_bundle,
        qualification_policy=policy,
    )
    snapshot = release.qualification
    evidence = QualificationEvidence(
        evidence_id="synthetic-allocation-qualification-proof",
        synthetic=True,
        generator_version="allocation-qualification-v1",
        seed=2323,
        version=version,
        scope=scope,
        kind="QUALIFICATION_PASS",
        digest="c" * 64,
        evaluation_end=snapshot.recorded_at,
        available_at=snapshot.recorded_at,
        expires_at=snapshot.valid_through,
        market_calendar_version=release.market_calendar_version,
    )
    return QualificationRecord(
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


def return_market_dates(cutoff_at: str, calendar_version: str) -> list[str]:
    calendar = synthetic_market_calendar(calendar_version)
    assert calendar is not None
    return [
        session.closed_at.date().isoformat()
        for session in calendar.recent_completed_sessions(datetime.fromisoformat(cutoff_at), 120)
    ]


def feasible_payload(
    settings: Settings,
    *,
    candidate_count: int = 1,
    probabilities: tuple[str, ...] = (),
    account_cash: tuple[str, str] | None = None,
    open_buy: bool = False,
    expected_purchase_fees: str = "0",
    security_id: str = "SYNTH-CANDIDATE",
    capital_state: str | None = None,
    qualification_valid_through: str = "2042-05-22T15:00:00Z",
    seed_qualification: bool = True,
    cutoff_at: str | None = None,
) -> tuple[dict[str, Any], CandidateReleaseOutcome]:
    from test_candidate_allocation import allocation_payload

    payload = allocation_payload(settings)
    risk = risk_handoff_payload(
        settings,
        normal=True,
        cutoff_at=cutoff_at,
        account_cash=account_cash,
        open_buy=open_buy,
        expected_purchase_fees=expected_purchase_fees,
        capital_state=capital_state,
    )["execution_plan"]
    payload["knowledge_cutoff"] = risk["cutoff_at"]
    source_payload = failed_candidate_case(settings)
    source_payload["access_scope"] = deepcopy(payload["access_scope"])
    case = FrozenDecisionCase.model_validate(source_payload)
    view = json.loads(
        (Path(__file__).parent / "fixtures/synthetic/candidate_workspace.json").read_text()
    )
    release = view["workspace"]["releases"][0]["release"]
    release.update(
        knowledge_cutoff="2042-05-17T15:00:00Z",
        published_at="2042-05-17T15:01:00Z",
        valid_market_dates=[
            f"2042-05-{day}" for day in range(19 if cutoff_at else 18, 24 if cutoff_at else 23)
        ],
    )
    release["qualification"].update(
        recorded_at="2042-05-17T14:59:00Z", valid_through=qualification_valid_through
    )
    release["members"][0]["valid_market_dates"] = release["valid_market_dates"]
    release["members"][0]["security_id"] = security_id
    for index in range(1, candidate_count):
        member = deepcopy(release["members"][0])
        member.update(
            security_id=f"SYNTH-CANDIDATE-{index}", research_id=f"synthetic-research-{index}"
        )
        release["members"].append(member)
    for member, probability in zip(release["members"], probabilities, strict=False):
        member["calibrated_probability"] = probability
    source = CandidateReleaseOutcome.model_validate(release)
    if seed_qualification:
        save_qualification_prior(settings, case, qualified_prior_record(case, source))
    runtime = initialize_runtime_storage(settings)
    from test_scoped_qualification import GovernanceClock

    ledger = DecisionLedger(runtime.engine, clock=GovernanceClock("2042-05-17T15:01:00Z"))
    ledger.persist_business_mapping_before_framework(case)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.commit_event(
            connection,
            case=case,
            framework_run_id=case.framework_run_id,
            result=ExternalResult(
                outcome_code="CANDIDATES",
                summary="Original synthetic candidate anchor.",
                key_reasons=("SYNTHETIC_CANDIDATE",),
                candidate_release=source,
            ),
            stage_results=(),
            committed_at="2042-05-17T15:01:00Z",
        )
        ledger.record_stage_result(
            connection,
            case=case,
            framework_run_id=case.framework_run_id,
            decision_event_id=fact.decision_event_id,
            stage_result=StageResult(
                phase="BUSINESS_COMMIT", status="SUCCEEDED", gate_results=(), reasons=()
            ),
        )
        ledger.publish_report(connection, fact)
        ledger.record_stage_result(
            connection,
            case=case,
            framework_run_id=case.framework_run_id,
            decision_event_id=fact.decision_event_id,
            stage_result=StageResult(
                phase="PUBLICATION", status="SUCCEEDED", gate_results=(), reasons=()
            ),
        )
    payload["candidate_allocation"].update(
        cutoff_at=risk["cutoff_at"],
        risk_handoff=risk,
        candidate_event_id=fact.decision_event_id,
        policy={
            "synthetic": True,
            "generator_version": "allocation-policy-v1",
            "seed": 2323,
            "version_id": "synthetic-allocation-policy",
            "entry_target_ratio": "0.07",
            "correlation_ceiling": "0.85",
            "neighborhood_ratio": "0.24",
            "turnover_ratio": "0.02",
            "correlation_window": 120,
        },
        securities=[
            {
                "security_id": security_id,
                "issuer_id": "fictional-new-issuer",
                "median_turnover": "100000",
                "turnover_window_sessions": 20,
                "evidence": position_evidence("synthetic-market", cutoff_at=risk["cutoff_at"]),
            }
        ],
        routes=[
            {
                "security_id": security_id,
                "account_id": "synthetic-account-4017",
                "permission": True,
                "price_cap": "10",
                "price_cap_confirmed": True,
                "current_price": "10",
                "price_tick": "1",
                "minimum_price": "1",
                "maximum_price": "100",
                "minimum_quantity": "10",
                "quantity_increment": "10",
                "commission_ratio": "0",
                "minimum_commission": "0",
                "other_cost_ratio": "0",
                "disposal_friction_ratio": "0",
                "version_id": "synthetic-buy-curve",
                "rule_version": "synthetic-buy-rule",
                "evidence": position_evidence("synthetic-route", cutoff_at=risk["cutoff_at"]),
            }
        ],
        correlations={
            "version_id": "synthetic-return-matrix",
            "market_calendar_version": source.market_calendar_version,
            "return_semantics": "DAILY_ADJUSTED",
            "market_dates": return_market_dates(risk["cutoff_at"], source.market_calendar_version),
            "returns": {
                "fictional-new-issuer": [str((i % 3) - 1) for i in range(120)],
                "FICTIONAL-ORBITAL-MOSAIC": [str((i % 5) - 2) for i in range(120)],
            },
            "evidence": position_evidence("synthetic-correlation", cutoff_at=risk["cutoff_at"]),
        },
        commitments=[],
    )
    from stock_profiler.modules.portfolio.allocation_contracts import CandidateAllocationCommand

    command = payload["candidate_allocation"]
    for index in range(1, candidate_count):
        security, route = deepcopy(command["securities"][0]), deepcopy(command["routes"][0])
        security.update(
            security_id=f"SYNTH-CANDIDATE-{index}", issuer_id=f"fictional-new-issuer-{index}"
        )
        route["security_id"] = security["security_id"]
        command["securities"].append(security)
        command["routes"].append(route)
        command["correlations"]["returns"][security["issuer_id"]] = deepcopy(
            command["correlations"]["returns"]["fictional-new-issuer"]
        )

    payload["candidate_allocation"] = CandidateAllocationCommand.model_validate(
        payload["candidate_allocation"]
    ).model_dump(mode="json")
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    return payload, source


def assert_allocation_preserves_genuine_release(
    settings: Settings, source_report: FormalReport
) -> None:
    """Consume a real calibrated/qualified report through the complete frozen host seam."""
    from test_candidate_allocation import allocation_payload
    from test_scoped_qualification import GovernanceClock

    from stock_profiler.bootstrap.decision_cases import (
        correct_default_frozen_decision_case,
        run_frozen_decision_case,
    )
    from stock_profiler.modules.portfolio.allocation_contracts import CandidateAllocationCommand

    release = source_report.result.candidate_release
    assert (
        release is not None
        and release.calibration is not None
        and release.qualification is not None
    )
    assert release.disposition == "CANDIDATES"
    payload = allocation_payload(settings)
    cutoff_at = "2042-07-01T16:00:00Z"
    assert source_report.access_scope is not None
    risk = risk_handoff_payload(
        settings,
        normal=True,
        single_account=True,
        cutoff_at=cutoff_at,
        user_id=source_report.access_scope.user_id,
    )["execution_plan"]
    payload["knowledge_cutoff"] = cutoff_at
    payload["access_scope"] = (
        source_report.access_scope.model_dump(mode="json") if source_report.access_scope else None
    )
    command = payload["candidate_allocation"]
    command.update(
        cutoff_at=cutoff_at,
        risk_handoff=risk,
        candidate_event_id=source_report.event_id,
        policy={
            "synthetic": True,
            "generator_version": "allocation-policy-v1",
            "seed": 2323,
            "version_id": "synthetic-allocation-policy",
            "entry_target_ratio": "0.07",
            "correlation_ceiling": "0.85",
            "neighborhood_ratio": "0.24",
            "turnover_ratio": "0.02",
            "correlation_window": 120,
        },
        securities=[],
        routes=[],
        commitments=[],
        correlations={
            "version_id": "synthetic-return-matrix",
            "market_calendar_version": release.market_calendar_version,
            "return_semantics": "DAILY_ADJUSTED",
            "market_dates": return_market_dates(cutoff_at, release.market_calendar_version),
            "returns": {"FICTIONAL-ORBITAL-MOSAIC": [str((index % 5) - 2) for index in range(120)]},
            "evidence": position_evidence("synthetic-correlation", cutoff_at=cutoff_at),
        },
    )
    for index, member in enumerate(release.members):
        issuer = (
            "FICTIONAL-ORBITAL-MOSAIC"
            if member.security_id == "XQZ-4017"
            else f"fictional-new-issuer-{index}"
        )
        command["securities"].append(
            {
                "security_id": member.security_id,
                "issuer_id": issuer,
                "median_turnover": "100000",
                "turnover_window_sessions": 20,
                "evidence": position_evidence("synthetic-market", cutoff_at=cutoff_at),
            }
        )
        command["routes"].append(
            {
                "security_id": member.security_id,
                "account_id": "synthetic-account-4017",
                "permission": True,
                "price_cap": "10",
                "price_cap_confirmed": True,
                "current_price": "10",
                "price_tick": "1",
                "minimum_price": "1",
                "maximum_price": "100",
                "minimum_quantity": "10",
                "quantity_increment": "10",
                "commission_ratio": "0",
                "minimum_commission": "0",
                "other_cost_ratio": "0",
                "disposal_friction_ratio": "0",
                "version_id": "synthetic-buy-curve",
                "rule_version": "synthetic-buy-rule",
                "evidence": position_evidence("synthetic-route", cutoff_at=cutoff_at),
            }
        )
        if issuer != "FICTIONAL-ORBITAL-MOSAIC":
            command["correlations"]["returns"][issuer] = [
                str((value % 3) - 1) for value in range(120)
            ]
    payload["candidate_allocation"] = CandidateAllocationCommand.model_validate(command).model_dump(
        mode="json"
    )
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock("2042-07-01T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "PLANNED", plan.reasons
    assert tuple(row.candidate for row in plan.rows) == tuple(
        member for member in release.members if member.candidate
    )
    assert plan.candidate_conclusion_version == source_report.report_version_id
    confirmation_case = deepcopy(payload)
    allocation_command = confirmation_case.pop("candidate_allocation")
    confirmation_case["input"].pop("candidate_allocation")
    confirmation_case["business_identity"] = "synthetic-genuine-release-confirmation"
    confirmation_version = "candidate-confirmation.1.0.0"
    confirmation_case["version_bundle"].update(
        case_contract_version=confirmation_version,
        host_contract_version=confirmation_version,
        report_projection_contract_version=confirmation_version,
    )
    from stock_profiler.modules.portfolio.confirmation_contracts import CandidateConfirmationCommand

    confirmation_case["candidate_confirmation"] = CandidateConfirmationCommand.model_validate(
        {
            "contract_version": "1.0.0",
            "operation": "SUBMIT",
            "user_id": source_report.access_scope.user_id,
            "portfolio_id": risk["portfolio_id"],
            "candidate_batch_id": plan.candidate_batch_id,
            "plan_event_id": report.event_id,
            "plan_id": plan.plan_id,
            "seen_confirmation_id": None,
            "idempotency_key": "synthetic-genuine-release",
            "withdrawal_position_event_id": None,
            "choices": [
                {"security_id": row.candidate.security_id, "choice": "ACCEPT"}
                for row in plan.rows
                if row.principal > 0
            ],
            "revalidation": allocation_command,
        }
    ).model_dump(mode="json")
    confirmation_case["input"]["candidate_confirmation"] = deepcopy(
        confirmation_case["candidate_confirmation"]
    )
    confirmed = run_frozen_decision_case(
        settings, confirmation_case, clock=GovernanceClock("2042-07-01T16:01:00Z")
    ).report
    assert confirmed is not None and confirmed.result.candidate_confirmation is not None
    assert confirmed.result.candidate_confirmation.disposition == "CONFIRMED", (
        confirmed.result.candidate_confirmation.reasons
    )
    assert confirmed.result.candidate_confirmation.reservations
    assert (
        run_frozen_decision_case(
            settings, confirmation_case, clock=GovernanceClock("2042-07-01T16:01:00Z")
        ).report
        == confirmed
    )
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine)
    with runtime.engine.connect() as connection:
        after = ledger.get_formal_report_for_event(source_report.event_id, connection)
    assert after is not None
    assert after == source_report
    assert after.result.evaluation_registrations == source_report.result.evaluation_registrations
    with runtime.engine.connect() as connection:
        research = ledger.get_decision_event(release.research_event_id, connection)
    assert research is not None
    correction = correct_default_frozen_decision_case(
        settings,
        research.case.business_identity,
        clock=GovernanceClock("2042-07-01T16:02:00Z"),
    )
    assert (
        correction.report is not None
        and correction.report.corrects_event_id == research.decision_event_id
    )
    payload.update(
        case_id="synthetic-allocation-after-research-correction",
        business_identity="synthetic-allocation-after-research-correction",
    )
    denied = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock("2042-07-01T16:03:00Z")
    ).report
    assert denied is not None and denied.result.candidate_allocation is not None
    blocked = denied.result.candidate_allocation
    assert blocked.disposition == "BLOCKED"
    assert blocked.reasons == ("RESEARCH_EVIDENCE_CORRECTED",)
    assert blocked.eligibility_evidence_ids == (correction.report.event_id,)
    assert tuple(row.candidate for row in blocked.rows) == tuple(row.candidate for row in plan.rows)
    with runtime.engine.connect() as connection:
        assert ledger.get_formal_report_for_event(report.event_id, connection) == report
        assert (
            ledger.get_formal_report_for_event(source_report.event_id, connection) == source_report
        )
