"""All scheduled months remain in the original rolling operations population."""

from datetime import datetime
from decimal import Decimal

from stock_profiler.modules.prospective.contracts import (
    CompletionRate,
    CycleOperations,
    CycleRegistration,
    MonthFailure,
    MonthlyObservation,
    PlanNode,
    SourceWatermark,
)


def rate(numerator: int, denominator: int, *, withheld: bool = False) -> CompletionRate:
    if withheld:
        return CompletionRate(numerator=0, denominator=0, rate=None, disposition="WITHHELD")
    return CompletionRate(
        numerator=numerator,
        denominator=denominator,
        rate=Decimal(numerator) / Decimal(denominator) if denominator else None,
        disposition="OBSERVED" if denominator else "NOT_APPLICABLE",
    )


def data_complete(node: PlanNode, row: MonthlyObservation | None, scopes: tuple[str, ...]) -> bool:
    if row is None:
        return False
    return {item.scope for item in row.sources} == set(scopes) and all(
        item.knowledge_cutoff == node.knowledge_cutoff and item.on_time for item in row.sources
    )


def pipeline_complete(node: PlanNode, row: MonthlyObservation | None) -> bool:
    return bool(
        row is not None
        and row.status in {"CANDIDATES", "ABSTAINED"}
        and row.completed_at <= node.disclosure_at
        and row.batch_saved_at is not None
        and row.batch_saved_at <= node.disclosure_at
    )


def summarize_operations(
    registration: CycleRegistration,
    observations: dict[str, MonthlyObservation],
    cutoff: datetime,
) -> tuple[CycleOperations, tuple[SourceWatermark, ...]]:
    nodes = tuple(node for node in registration.plan_nodes if node.knowledge_cutoff <= cutoff)
    window = nodes[-24:]
    data = pipeline = batches = reports = recommended = timely = 0
    notifications = delivered = 0
    failures: list[MonthFailure] = []
    core_failures: list[bool] = []
    incidents = 0
    unclosed = sum(
        item.closed_at is None or item.closed_at > cutoff
        for row in observations.values()
        for item in row.incidents
    )
    safety_clear = not any(
        item.kind in {"PRIVACY", "SHADOW"}
        for row in observations.values()
        for item in row.incidents
    )
    obligation_results: dict[str, list[bool]] = {
        key: [] for key in ("batch", "report", "timely", "notification")
    }
    candidate_obligations_known = True
    current_opportunity = any(node.disclosure_at > cutoff for node in window)
    for node in window:
        row = observations.get(node.plan_month)
        data_ok = data_complete(node, row, registration.source_scopes)
        path_ok = pipeline_complete(node, row)
        valid = data_ok and path_ok
        data += data_ok
        pipeline += valid
        core_failures.append(
            not data_ok or row is None or row.reason in {"DATA", "SYSTEM", "UNKNOWN"}
        )
        obligation_results["batch"].append(valid)
        if row is None:
            failures.append(MonthFailure(plan_month=node.plan_month, reason="MISSING"))
            candidate_obligations_known = False
            obligation_results["report"].append(False)
            continue
        if not valid:
            failures.append(
                MonthFailure(
                    plan_month=node.plan_month,
                    reason=row.reason
                    if row.reason != "NONE"
                    else ("DATA" if not data_ok else "MISSING"),
                )
            )
        if row.status in {"FAILED", "BLOCKED", "UNKNOWN"}:
            candidate_obligations_known = False
        batches += valid
        report_ok = bool(
            valid and row.report_saved_at is not None and row.report_saved_at <= node.disclosure_at
        )
        reports += report_ok
        obligation_results["report"].append(report_ok)
        candidate = valid and row.status == "CANDIDATES"
        recommended += candidate
        timely_ok = bool(
            candidate
            and row.first_delivery_at is not None
            and row.first_delivery_at < node.first_entry_at
        )
        timely += timely_ok
        if candidate:
            obligation_results["timely"].append(timely_ok)
        obligations = tuple(item for item in row.notifications if item.due_at <= cutoff)
        notifications += len(obligations)
        results = [
            item.delivered_at is not None and item.delivered_at <= item.due_at
            for item in sorted(obligations, key=lambda item: item.due_at)
        ]
        delivered += sum(results)
        obligation_results["notification"].extend(results)
        incidents += len(row.incidents)
    source_watermarks = []
    for scope in registration.source_scopes:
        streak = 0
        for node in nodes:
            row = observations.get(node.plan_month)
            source = (
                next((item for item in row.sources if item.scope == scope), None) if row else None
            )
            streak = (
                streak + 1
                if source is not None
                and source.on_time
                and source.knowledge_cutoff == node.knowledge_cutoff
                else 0
            )
        source_watermarks.append(
            SourceWatermark(
                scope=scope,
                consecutive_months=streak,
                three_month_watermark=streak >= 3,
            )
        )
    hidden = current_opportunity or not candidate_obligations_known
    consecutive_core = any(a and b for a, b in zip(core_failures, core_failures[1:], strict=False))
    consecutive_obligations = any(
        not a and not b
        for results in obligation_results.values()
        for a, b in zip(results, results[1:], strict=False)
    )

    def passing(numerator: int, denominator: int, floor: Decimal) -> bool:
        return denominator > 0 and Decimal(numerator) >= floor * denominator

    gates_passed = (
        len(window) == 24
        and not hidden
        and safety_clear
        and unclosed == 0
        and not consecutive_core
        and not consecutive_obligations
        and passing(data, len(window), Decimal("0.95"))
        and passing(pipeline, len(window), Decimal("0.95"))
        and passing(recommended, pipeline, Decimal("0.50"))
        and passing(batches, len(window), Decimal("0.95"))
        and passing(reports, len(window), Decimal("0.95"))
        and (notifications == 0 or passing(delivered, notifications, Decimal("0.95")))
        and (recommended == 0 or passing(timely, recommended, Decimal("0.95")))
    )
    operations = CycleOperations(
        planned_months=len(window),
        window_months=tuple(node.plan_month for node in window),
        full_window=len(window) == 24,
        data=rate(data, len(window)),
        pipeline=rate(pipeline, len(window)),
        batches=rate(batches, len(window)),
        reports=rate(reports, len(window)),
        notifications=rate(delivered, notifications, withheld=hidden),
        timeliness=rate(timely, recommended, withheld=hidden),
        coverage=rate(
            recommended,
            pipeline,
            withheld=hidden or not passing(pipeline, len(window), Decimal("0.95")),
        ),
        failures=tuple(failures),
        incident_count=incidents,
        unclosed_incidents=unclosed,
        safety_clear=safety_clear,
        consecutive_failure=consecutive_core,
        consecutive_obligation_failure=consecutive_obligations,
        gates_passed=gates_passed,
    )
    return operations, tuple(source_watermarks)
