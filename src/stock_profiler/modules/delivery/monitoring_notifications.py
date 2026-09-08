"""Deterministic routing of frozen synthetic channel observations."""

from datetime import datetime
from hashlib import sha256
from typing import Literal

from stock_profiler.modules.delivery.monitoring_contracts import (
    ChannelResult,
    MonitoringNotification,
    MonitoringOutcome,
    SyntheticNotificationInput,
)


def route_synthetic_notifications(
    source: MonitoringOutcome,
    request: SyntheticNotificationInput,
    observed_at: str,
) -> tuple[MonitoringNotification, ...]:
    attempts: list[MonitoringNotification] = []
    now = datetime.fromisoformat(observed_at)
    for item in source.cases:
        if request.quiet_until is not None and now < request.quiet_until and item.priority != "P0":
            window = source.freshness.next_window_end if source.freshness is not None else None
            if window is None or request.quiet_until < window:
                continue
        roles: list[tuple[Literal["IMMEDIATE", "PERSISTENT"], ChannelResult]] = [
            ("IMMEDIATE", request.immediate_result)
        ]
        if item.priority == "P0" or request.immediate_result != "ACCEPTED":
            roles.append(("PERSISTENT", request.persistent_result))
        freshness = source.freshness
        window_text = (
            f"{freshness.next_window_start} to {freshness.next_window_end}"
            if freshness is not None
            else "Unknown window"
        )
        for role, result in roles:
            identity = sha256(f"{request.identity}\n{item.case_id}\n{role}".encode()).hexdigest()
            attempts.append(
                MonitoringNotification(
                    notification_id=f"monitoring-notification-{identity}",
                    case_id=item.case_id,
                    source_event_id=item.source_event_id,
                    role=role,
                    result=result,
                    attempted_at=observed_at,
                    body=f"{item.priority} | Risk review | {window_text} | "
                    f"{freshness.market_status if freshness else 'Unknown market'} | /monitoring",
                )
            )
    return tuple(attempts)
