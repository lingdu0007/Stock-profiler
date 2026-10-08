"""Independent D0 calendar facts execute a formal look through the complete host."""

import json
from calendar import monthrange
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from test_investable_universe import manifest
from test_prospective_population_join import registration_payload, sources, store
from test_prospective_shadow_cycle import observation_case, review_case, run_case, saved
from test_scoped_qualification import case_payload
from test_selection_cohort import (
    bind_selection_evidence,
    monthly_training_calendar,
    selection_payload,
)

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.candidate_selection.calibrated_candidates import CandidateReleaseOutcome
from stock_profiler.modules.candidate_selection.screening import ScreeningArtifact
from stock_profiler.modules.candidate_selection.selection import SelectionCommand, freeze_selection
from stock_profiler.modules.candidate_selection.universe import UniverseCommand, freeze_universe
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FrozenDecisionCase,
)
from stock_profiler.modules.evaluation.cohort_metrics import historical_sessions
from stock_profiler.modules.evaluation.contracts import StandardOutcomeCommand
from stock_profiler.modules.portfolio.market_calendar import (
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
)
from stock_profiler.modules.prospective.contracts import CycleCommand, PlanNode
from stock_profiler.modules.research.contracts import (
    RAW_SCORE_MARKET_CALENDAR_VERSION,
    FrozenDualTargetScreening,
    RawScoreModelSnapshot,
    _frozen_raw_score_training_records,
    _raw_score_mature_training_months,
    screening_output_sha256,
    selection_binding_sha256,
)


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def nodes() -> tuple[PlanNode, ...]:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert calendar is not None
    result = []
    history = historical_sessions(calendar)
    for index in range(25):
        year, month = divmod(2042 * 12 + 4 + index, 12)
        month += 1
        last = max(
            item.closed_at
            for item in history
            if item.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
            == f"{year}-{month:02}"
        )
        cutoff = last.replace(hour=15, minute=59, second=59)
        sessions = calendar.sessions_after_open(cutoff, 5)
        disclosure = sessions[-1].closed_at
        result.append(
            PlanNode(
                plan_month=f"{year}-{month:02}",
                knowledge_cutoff=cutoff,
                first_entry_at=sessions[0].closed_at - timedelta(hours=7),
                disclosure_at=disclosure,
                matures_at=six_month_terminal_evaluation_at(disclosure, calendar.version_id),
                calendar_version=calendar.version_id,
            )
        )
    return tuple(result)


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    payload["prospective"] = CycleCommand.model_validate(payload["prospective"]).model_dump(
        mode="json"
    )
    payload["input"]["prospective"] = payload["prospective"]
    return payload


def monthly_selection(
    settings: Settings,
    template: dict[str, Any],
    universe_template: DecisionEventFact,
    plan: PlanNode,
) -> DecisionEventFact:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert calendar is not None and universe_template.case.universe is not None
    history = tuple(
        item for item in historical_sessions(calendar) if item.closed_at <= plan.knowledge_cutoff
    )
    year, month = map(int, plan.plan_month.split("-"))
    closes = {
        item.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(): iso(item.closed_at)
        for item in history
        if item.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m") == plan.plan_month
    }
    universe = universe_template.case.universe.model_dump(mode="json")
    universe["cutoff_at"] = iso(plan.knowledge_cutoff)
    universe["calendar"]["days"] = [
        {
            "market_date": f"{plan.plan_month}-{day:02}",
            "close_at": closes.get(f"{plan.plan_month}-{day:02}"),
        }
        for day in range(1, monthrange(year, month)[1] + 1)
    ]
    for security in universe["securities"]:
        security.update(
            turnover_dates=[
                item.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                for item in history[-4:]
            ],
            rules_valid_until="2047-12-31T23:59:59+08:00",
        )
    universe["manifest"] = manifest(universe)
    for entry in universe["manifest"]["entries"]:
        entry["evidence"].update(
            evidence_id=f"fictional-{plan.plan_month}-{entry['field_family']}",
            license_valid_until="2047-12-31T23:59:59+08:00",
            fact_effective_at=iso(plan.knowledge_cutoff - timedelta(minutes=10)),
            source_published_at=iso(plan.knowledge_cutoff - timedelta(minutes=4)),
            source_observed_at=iso(plan.knowledge_cutoff - timedelta(minutes=3)),
            acquired_at=iso(plan.knowledge_cutoff - timedelta(minutes=2)),
            validated_at=iso(plan.knowledge_cutoff - timedelta(minutes=1)),
        )
    universe_command = UniverseCommand.model_validate(universe)
    universe_result = freeze_universe(universe_command, business_prerequisite_met=True)
    assert universe_result.disposition == "FROZEN", universe_result.reasons
    payload = universe_template.case.model_dump(mode="json")
    payload.update(
        case_id=f"fictional-shadow-universe-{plan.plan_month}",
        business_identity=f"fictional-shadow-universe-{plan.plan_month}",
        knowledge_cutoff=iso(plan.knowledge_cutoff),
        report_generated_at=iso(plan.knowledge_cutoff + timedelta(seconds=10)),
    )
    payload["access_scope"]["visibility"] = "SHADOW"
    payload["universe"] = universe_command.model_dump(mode="json")
    payload["input"]["universe"] = payload["universe"]
    universe_fact = store(
        settings,
        FrozenDecisionCase.model_validate(payload),
        ExternalResult(
            outcome_code="FICTIONAL_ORIGINAL_UNIVERSE",
            summary="Synthetic original universe.",
            key_reasons=("SYNTHETIC",),
            universe=universe_result,
        ),
    )
    payload = deepcopy(template)
    payload.update(
        case_id=f"fictional-shadow-selection-{plan.plan_month}",
        business_identity=f"fictional-shadow-selection-{plan.plan_month}",
        knowledge_cutoff=iso(plan.knowledge_cutoff),
        report_generated_at=iso(plan.knowledge_cutoff + timedelta(seconds=20)),
    )
    payload["access_scope"]["visibility"] = "SHADOW"
    selection = payload["selection"]
    selection.update(
        cutoff_at=iso(plan.knowledge_cutoff),
        universe_object_id=universe_fact.business_object_id,
        universe_event_id=universe_fact.decision_event_id,
    )
    for row in selection["rows"]:
        row["return_dates"] = [
            item.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for item in history[-8:]
        ]
    artifact = selection["screening"]["artifact"]
    first = 2041 * 12
    last = year * 12 + month - 1 - 15
    training_months = [f"{index // 12}-{index % 12 + 1:02}" for index in range(first, last + 1)]
    artifact["training_calendar"] = [
        {
            "month": current,
            "selection_cutoff_at": (
                f"{current}-{monthrange(int(current[:4]), int(current[5:]))[1]:02}T23:59:59+08:00"
            ),
            "calendar": monthly_training_calendar(current),
        }
        for current in training_months
    ]
    mature_months = []
    for current in training_months:
        selected_year, selected_month = map(int, current.split("-"))
        target_year, target_month = divmod(selected_year * 12 + selected_month - 1 + 15, 12)
        target_month += 1
        maturity = datetime(
            target_year,
            target_month,
            min(
                monthrange(selected_year, selected_month)[1],
                monthrange(target_year, target_month)[1],
            ),
            15,
            59,
            59,
            tzinfo=UTC,
        )
        if maturity <= plan.knowledge_cutoff:
            mature_months.append(current)
    artifact["training_months"] = mature_months[-4:]
    artifact["training_members"] = [
        {
            "month": current,
            "security_id": "fictional-training-security",
            "positive_label": True,
            "terminal_label": False,
            "label_available_at": iso(plan.knowledge_cutoff),
        }
        for current in mature_months[-4:]
    ]
    artifact["training_members_sha256"] = digest(artifact["training_members"])
    artifact.update(
        fitted_at=iso(plan.knowledge_cutoff),
        label_available_through=iso(plan.knowledge_cutoff),
    )
    artifact = ScreeningArtifact.model_validate(artifact).model_dump(mode="json")
    artifact["snapshot_id"] = "sha256:" + digest(
        {key: value for key, value in artifact.items() if key != "snapshot_id"}
    )
    selection["screening"]["artifact"] = artifact
    selection["screening_snapshot_id"] = artifact["snapshot_id"]
    selection["evidence"] = deepcopy(
        universe_command.model_dump(mode="json")["manifest"]["entries"][0]["evidence"]
    )
    bind_selection_evidence(payload, rebuild_screening=False)
    selected_command = SelectionCommand.model_validate(selection)
    outcome = freeze_selection(
        selected_command, universe_command, universe_result, prerequisite="SUCCEEDED"
    )
    assert outcome.disposition == "FROZEN", outcome.reasons
    payload["selection"] = selected_command.model_dump(mode="json")
    payload["input"]["selection"] = payload["selection"]
    return store(
        settings,
        FrozenDecisionCase.model_validate(payload),
        ExternalResult(
            outcome_code="FICTIONAL_ORIGINAL_SELECTION",
            summary="Synthetic original pool.",
            key_reasons=("SYNTHETIC",),
            selection=outcome,
        ),
    )


def release_sources(
    settings: Settings,
    selection: DecisionEventFact,
    research_template: DecisionEventFact,
    candidate_template: DecisionEventFact,
    plan: PlanNode,
) -> DecisionEventFact:
    assert selection.result.selection is not None and selection.case.selection is not None
    payload = research_template.case.model_dump(mode="json")
    payload.update(
        case_id=f"fictional-shadow-research-{plan.plan_month}",
        business_identity=f"fictional-shadow-research-{plan.plan_month}",
        knowledge_cutoff=iso(plan.knowledge_cutoff),
        report_generated_at=iso(plan.knowledge_cutoff + timedelta(seconds=30)),
    )
    research = payload["research"]
    training_months = _raw_score_mature_training_months(
        plan.knowledge_cutoff, RAW_SCORE_MARKET_CALENDAR_VERSION
    )[-120:]
    cohorts, records = _frozen_raw_score_training_records(training_months)
    watermark = max(record.label_available_at for record in records)
    snapshot = research["raw_score_model"]
    snapshot.update(
        training_window_id=f"fictional-training-{plan.plan_month}",
        training_window_kind="ROLLING_120" if len(training_months) == 120 else "EXPANDING",
        training_window_month_count=len(training_months),
        training_window_start_month=training_months[0],
        training_window_end_month=training_months[-1],
        training_months=training_months,
        label_watermark_month=watermark.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m"),
        label_watermark_at=iso(watermark),
        training_cohorts=[cohort.model_dump(mode="json") for cohort in cohorts],
        training_records=[record.model_dump(mode="json") for record in records],
        calibration_history_cohorts=[cohort.model_dump(mode="json") for cohort in cohorts],
        calibration_history_records=[record.model_dump(mode="json") for record in records],
        mature_months=len(training_months),
        training_record_count=len(records),
        positive_record_count=sum(record.terminal_label for record in records),
        negative_record_count=sum(not record.terminal_label for record in records),
    )
    research["raw_score_model"] = RawScoreModelSnapshot.model_validate(snapshot).model_dump(
        mode="json"
    )
    research.update(
        cutoff_at=iso(plan.knowledge_cutoff),
        knowledge_cutoff=iso(plan.knowledge_cutoff),
        selection_event_id=selection.decision_event_id,
        selection_object_id=selection.business_object_id,
    )
    screening = research["screening"]
    screening.update(
        strategy_version=selection.case.selection.strategy_version,
        snapshot_id=selection.case.selection.screening_snapshot_id,
        universe_security_ids=[row.security_id for row in selection.case.selection.rows],
        selected_member_ids=list(selection.result.selection.members),
    )
    for head in ("positive", "terminal"):
        screening[f"{head}_scores"] = {
            row.security_id: str(getattr(row, f"{head}_score"))
            for row in selection.case.selection.rows
        }
        screening[f"{head}_percentiles"] = {
            row.security_id: str(getattr(row, f"{head}_percentile"))
            for row in selection.result.selection.ranking
        }
    screening["output_sha256"] = screening_output_sha256(
        FrozenDualTargetScreening.model_construct(**screening)
    )
    for member, security in zip(
        research["members"], selection.result.selection.members, strict=True
    ):
        member.update(security_id=security, knowledge_cutoff=iso(plan.knowledge_cutoff))
        for entry in member["data_manifest"]["entries"]:
            entry["knowledge_cutoff"] = iso(plan.knowledge_cutoff)
        for entry in member["evidence"]:
            entry.update(
                knowledge_cutoff=iso(plan.knowledge_cutoff),
                acquired_at=iso(plan.knowledge_cutoff - timedelta(minutes=2)),
                validated_at=iso(plan.knowledge_cutoff - timedelta(minutes=1)),
                source_published_at=iso(plan.knowledge_cutoff - timedelta(minutes=3)),
            )
    assert research_template.case.research is not None
    research["selection_fingerprint"] = selection_binding_sha256(
        selection.business_object_id,
        selection.decision_event_id,
        plan.knowledge_cutoff,
        FrozenDualTargetScreening.model_validate(screening),
    )
    payload["input"]["research"] = research
    research_fact = store(
        settings,
        FrozenDecisionCase.model_validate(payload),
        research_template.result.model_copy(update={"evaluation_registrations": ()}),
    )
    at = plan.disclosure_at - timedelta(minutes=1)
    payload = candidate_template.case.model_dump(mode="json")
    payload.update(
        case_id=f"fictional-shadow-release-{plan.plan_month}",
        business_identity=f"fictional-shadow-release-{plan.plan_month}",
        knowledge_cutoff=iso(plan.knowledge_cutoff),
        report_generated_at=iso(at),
    )
    command = payload["candidate_release"]
    command.update(
        batch_id=f"fictional-{plan.plan_month}",
        knowledge_cutoff=iso(plan.knowledge_cutoff),
        label_watermark_at=iso(plan.knowledge_cutoff),
        published_at=iso(at),
        research_event_id=research_fact.decision_event_id,
        research_object_id=research_fact.business_object_id,
    )
    command["candidates"] = [
        dict(deepcopy(command["candidates"][index]), security_id=security)
        for index, security in enumerate(selection.result.selection.members)
    ]
    payload["input"]["candidate_release"] = command
    assert candidate_template.result.candidate_release is not None
    release = candidate_template.result.candidate_release.model_dump(mode="json")
    release.update(
        knowledge_cutoff=iso(plan.knowledge_cutoff),
        published_at=iso(at),
        research_event_id=research_fact.decision_event_id,
        research_object_id=research_fact.business_object_id,
    )
    release["members"] = [
        dict(deepcopy(release["members"][index]), security_id=security)
        for index, security in enumerate(selection.result.selection.members)
    ]
    return store(
        settings,
        FrozenDecisionCase.model_validate(payload),
        ExternalResult(
            outcome_code="FICTIONAL_ORIGINAL_RELEASE",
            summary="Independent synthetic probability snapshot.",
            key_reasons=("SYNTHETIC",),
            candidate_release=CandidateReleaseOutcome.model_validate(release),
        ),
    )


def evidence(selection: DecisionEventFact, plan: PlanNode, *, passed: bool) -> dict[str, Any]:
    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert (
        calendar is not None
        and selection.case.selection is not None
        and selection.result.selection is not None
    )
    sessions = calendar.sessions_after_open(plan.knowledge_cutoff, 5)
    entry_at = sessions[0].closed_at
    terminal_at = six_month_terminal_evaluation_at(entry_at, calendar.version_id)
    history = tuple(
        item for item in historical_sessions(calendar) if item.closed_at <= plan.knowledge_cutoff
    )
    outcome_sessions = tuple(
        item for item in historical_sessions(calendar) if entry_at <= item.closed_at <= terminal_at
    )
    costs = {"commission": "0", "fees": "0", "taxes": "0", "slippage": "0"}

    def fact(identity: str, at: datetime) -> dict[str, Any]:
        return {
            "evidence_id": f"fictional-{plan.plan_month}-{identity}",
            "authority": "EXCHANGE",
            "source_version": "fictional-market-v1",
            "effective_at": iso(at),
            "published_at": iso(at + timedelta(seconds=1)),
            "acquired_at": iso(at + timedelta(seconds=2)),
            "validated_at": iso(at + timedelta(seconds=3)),
            "corrects_evidence_id": None,
            "correction_reason": None,
        }

    observations, paths = [], []
    for security in (row.security_id for row in selection.case.selection.rows):
        price = "13" if passed and security in selection.result.selection.members else "9"
        terminal = dict(
            fact(f"terminal-{security}", terminal_at),
            price=price,
            quantity_multiplier="1",
            cash_distributions="0",
            actions_complete_through=iso(terminal_at),
            costs=costs,
        )
        observations.append(
            {
                "security_id": security,
                "entry": {
                    **fact(f"entry-{security}", entry_at),
                    "sessions": [
                        {
                            "closed_at": iso(entry_at),
                            "buyable": True,
                            "unavailable_reason": None,
                            "turnover": "10000",
                            "volume": "1000",
                            "costs": costs,
                        }
                    ],
                },
                "terminal": terminal,
            }
        )
        paths.append(
            {
                "security_id": security,
                "marks": [
                    dict(
                        fact(f"mark-{security}-{item.ordinal}", item.closed_at),
                        price=price if item.closed_at == terminal_at else "10",
                        quantity_multiplier="1",
                        cash_distributions="0",
                        actions_complete_through=iso(item.closed_at),
                        costs=costs,
                    )
                    for item in outcome_sessions
                ],
            }
        )
    context = fact("index", history[-1].closed_at)
    context.update(
        published_at=iso(plan.knowledge_cutoff - timedelta(seconds=3)),
        acquired_at=iso(plan.knowledge_cutoff - timedelta(seconds=2)),
        validated_at=iso(plan.knowledge_cutoff - timedelta(seconds=1)),
    )
    return {
        "plan_month": plan.plan_month,
        "selection_event_id": selection.decision_event_id,
        "standard_event_id": None,
        "observations": observations,
        "paths": paths,
        "index": {
            **context,
            "prices": [
                {"closed_at": iso(item.closed_at), "total_return_price": str(1000 + index)}
                for index, item in enumerate(history[-120:])
            ],
        },
        "factors": {
            **context,
            "evidence_id": f"fictional-{plan.plan_month}-factors",
            "rows": [
                {
                    "security_id": row.security_id,
                    "net_income_ttm": str(index + 1),
                    "total_capitalization": "100",
                    "average_equity": "100",
                    "momentum_start_price": "10",
                    "momentum_end_price": "11",
                    "momentum_start_at": iso(history[-252].closed_at),
                    "momentum_end_at": iso(history[-21].closed_at),
                    "daily_returns": [".01" if index % 2 else "-.01" for index in range(120)],
                    "return_dates": [iso(item.closed_at) for item in history[-120:]],
                }
                for index, row in enumerate(selection.case.selection.rows)
            ],
        },
        "future_index": None,
    }


def test_saved_formal_execution_keeps_alpha_and_labels_across_replay_and_review(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    prototypes = sources(settings, persist=False)
    template = selection_payload(settings)
    template["selection"]["policy"].update(cohort_size=10, industry_limit=3, capitalization_limit=5)
    bind_selection_evidence(template)
    ledger = DecisionLedger.from_settings(settings)
    with ledger.serialize_case_execution() as connection:
        universe_template = ledger.get_decision_event(
            template["selection"]["universe_event_id"], connection
        )
    assert universe_template is not None
    plans = nodes()
    payload = registration_payload(settings, prototypes, formal=True)
    payload.update(
        knowledge_cutoff="2042-04-01T00:00:00Z", report_generated_at="2042-04-01T00:00:00Z"
    )
    payload["prospective"]["cutoff_at"] = payload["knowledge_cutoff"]
    registered_policy = payload["prospective"]["registration"]
    registered_policy.update(
        plan_nodes=[plan.model_dump(mode="json") for plan in plans],
        formal_floor={"mature_batches": 12, "nonoverlapping_windows": 2, "high_band_records": 20},
    )
    population = registered_policy["population_policy"]
    population.update(
        cohort_size=10,
        selection_policy=deepcopy(template["selection"]["policy"]),
        selection_strategy_version=template["selection"]["strategy_version"],
    )
    population["maturity"].update(observation_batches=6, observation_windows=1)
    policy = registered_policy["formal_policy"]
    policy.update(
        node_months=[plans[18].plan_month, plans[24].plan_month], bootstrap_repetitions=99
    )
    policy["cohort"].update(
        source_version_bundle=population["selection_bundle"],
        strategy_version=population["selection_strategy_version"],
        selection_policy=population["selection_policy"],
        positive_members_required=5,
        target_members_required=3,
        terminal_target=".25",
        maximum_drawdown=".15",
        random_trials=37,
    )
    registered = saved(settings, run_case(settings, normalize(payload)))
    previous = None
    original_sources = []
    for index, plan in enumerate(plans[:12]):
        selection = monthly_selection(settings, template, universe_template, plan)
        candidate = release_sources(settings, selection, prototypes[1], prototypes[2], plan)
        original_sources.append(selection)
        payload = observation_case(
            settings, registered.decision_event_id, previous, identity=f"observe-{index}"
        )
        payload.update(
            knowledge_cutoff=iso(plan.disclosure_at), report_generated_at=iso(plan.disclosure_at)
        )
        payload["prospective"]["cutoff_at"] = iso(plan.disclosure_at)
        observation = payload["prospective"]["observation"]
        observation.update(
            plan_month=plan.plan_month,
            completed_at=iso(plan.disclosure_at),
            batch_saved_at=candidate.committed_at,
            report_saved_at=candidate.committed_at,
            batch_link={
                "selection_event_id": selection.decision_event_id,
                "candidate_event_id": candidate.decision_event_id,
            },
        )
        source = observation["sources"][0]
        source.update(
            knowledge_cutoff=iso(plan.knowledge_cutoff),
            published_at=iso(plan.knowledge_cutoff - timedelta(minutes=3)),
            acquired_at=iso(plan.knowledge_cutoff - timedelta(minutes=2)),
            validated_at=iso(plan.knowledge_cutoff - timedelta(minutes=1)),
        )
        previous = saved(settings, run_case(settings, normalize(payload))).decision_event_id
    check_at = plans[18].disclosure_at
    monthly_inputs = []
    for index, (selection, plan) in enumerate(zip(original_sources, plans[:12], strict=True)):
        item = evidence(selection, plan, passed=index % 6 != 0)
        standard_at = check_at - timedelta(minutes=1)
        payload = case_payload(
            settings, f"standard-{index}", {}, contract_version="standard-outcomes.1.0.0"
        )
        payload.pop("governance")
        payload["access_scope"]["visibility"] = "SHADOW"
        payload.update(knowledge_cutoff=iso(standard_at), report_generated_at=iso(standard_at))
        assert selection.result.selection is not None
        payload["standard_outcomes"] = StandardOutcomeCommand.model_validate(
            {
                "contract_version": "1.0.0",
                "synthetic": True,
                "generator_version": "fictional-prospective-calendar/1",
                "seed": 2291,
                "selection_event_id": selection.decision_event_id,
                "cutoff_at": iso(standard_at),
                "market_calendar_version": "synthetic-market-calendar-v1",
                "standard_quantity": "100",
                "observations": [
                    row
                    for row in item["observations"]
                    if row["security_id"] in selection.result.selection.members
                ],
                "previous_event_id": None,
            }
        ).model_dump(mode="json")
        payload["input"]["standard_outcomes"] = payload["standard_outcomes"]
        standard = saved(settings, run_case(settings, payload))
        item["standard_event_id"] = standard.decision_event_id
        monthly_inputs.append(item)
    payload = review_case(
        settings,
        registered.decision_event_id,
        previous,
        cutoff=iso(check_at),
        identity="formal-first",
    )
    payload["prospective"].update(
        operation="CHECK", formal_node_month=plans[18].plan_month, formal_months=monthly_inputs
    )
    checked = saved(settings, run_case(settings, normalize(payload)))
    assert (
        checked.result.prospective is not None
        and checked.result.prospective.formal_look is not None
    )
    look = checked.result.prospective.formal_look
    assert look.actual_ordinal == 1 and look.alpha_spent == look.alpha_this == Decimal(".025")
    assert look.inference is not None and not look.inference.qualified
    assert look.inference.calibration_records == look.inference.high_band_records == 120
    assert look.disposition == "INDETERMINATE"
    assert (
        saved(settings, run_case(settings, payload)).decision_event_id == checked.decision_event_id
    )
    later = review_case(
        settings,
        registered.decision_event_id,
        checked.decision_event_id,
        cutoff=iso(check_at + timedelta(days=1)),
        identity="review-after-look",
    )
    reviewed = saved(settings, run_case(settings, later))
    assert (
        reviewed.result.prospective is not None
        and reviewed.result.prospective.formal_look is not None
    )
    assert reviewed.result.prospective.formal_look.actual_ordinal == 1
    assert reviewed.result.prospective.formal_look.alpha_spent == look.alpha_spent
    assert look.inference.gates["coverage"].estimate == 0
    assert look.inference.gates["coverage"].passed is False
    following = review_case(
        settings,
        registered.decision_event_id,
        reviewed.decision_event_id,
        cutoff=iso(plans[24].disclosure_at),
        identity="formal-next-insufficient",
    )
    following["prospective"].update(operation="CHECK", formal_node_month=plans[24].plan_month)
    skipped = saved(settings, run_case(settings, normalize(following)))
    assert (
        skipped.result.prospective is not None
        and skipped.result.prospective.formal_look is not None
    )
    next_look = skipped.result.prospective.formal_look
    assert next_look.disposition == "SKIPPED" and next_look.actual_ordinal == 1
    assert next_look.alpha_spent == look.alpha_spent and next_look.alpha_this == 0
    assert "MATURE_BATCHES" in next_look.waiting_for and next_look.inference is None
