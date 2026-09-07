"""Deterministic portfolio authorization adjudication for the frozen D0 seam."""

from datetime import datetime

from stock_profiler.modules.portfolio.contracts import (
    PortfolioAuthorization,
    PortfolioAuthorizationOutcome,
    PortfolioAuthorizationUsage,
    PortfolioCommand,
    PortfolioConfirmationCommand,
    PortfolioPreviewCommand,
    PortfolioUseCommand,
    confirmation_block_reasons,
    preview_for,
)


def adjudicate(
    command: PortfolioCommand,
    *,
    event_id: str,
    observed_at: str,
    history: tuple[PortfolioAuthorizationOutcome, ...],
    access_account_ids: tuple[str, ...],
) -> PortfolioAuthorizationOutcome:
    """Preview a full scope or freeze its first confirmed synthetic authorization."""
    if isinstance(command, PortfolioUseCommand):
        return _adjudicate_use(
            command,
            observed_at=observed_at,
            history=history,
            access_account_ids=access_account_ids,
        )
    preview = preview_for(command.proposal)
    if isinstance(command, PortfolioPreviewCommand):
        return PortfolioAuthorizationOutcome(
            disposition="PREVIEWED",
            reasons=("PORTFOLIO_SCOPE_PREVIEWED",),
            preview=preview,
        )
    assert isinstance(command, PortfolioConfirmationCommand)
    reasons = confirmation_block_reasons(preview)
    current, history_reason = _current_authorization(history, command.proposal.portfolio_id)
    if history_reason is not None:
        reasons = (history_reason,)
    elif current is None and command.previous_authorization_id is not None:
        reasons = ("PREVIOUS_AUTHORIZATION_NOT_FOUND",)
    elif current is not None and command.previous_authorization_id is None:
        reasons = ("PREVIOUS_AUTHORIZATION_REQUIRED",)
    elif current is not None and command.previous_authorization_id != current.authorization_id:
        reasons = ("AUTHORIZATION_REVISION_CONFLICT",)
    elif (
        current is not None
        and command.proposal.risk_budget.version_id == current.proposal.risk_budget.version_id
    ):
        reasons = ("RISK_BUDGET_VERSION_REUSE",)
    elif (
        current is not None
        and command.proposal.risk_budget.effective_at <= current.proposal.risk_budget.effective_at
    ):
        reasons = ("RISK_BUDGET_NOT_FORWARD_EFFECTIVE",)
    if reasons:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=reasons,
            preview=preview,
        )
    return PortfolioAuthorizationOutcome(
        disposition="APPROVED",
        reasons=("PORTFOLIO_AUTHORIZATION_RECORDED",),
        preview=preview,
        authorization=PortfolioAuthorization(
            authorization_id=event_id,
            proposal=command.proposal,
            confirmation=command.confirmation,
            previous_authorization_id=command.previous_authorization_id,
            recorded_at=datetime.fromisoformat(observed_at),
        ),
    )


def _adjudicate_use(
    command: PortfolioUseCommand,
    *,
    observed_at: str,
    history: tuple[PortfolioAuthorizationOutcome, ...],
    access_account_ids: tuple[str, ...],
) -> PortfolioAuthorizationOutcome:
    authorization = _authorization_by_id(history, command.portfolio_id, command.authorization_id)
    if authorization is None:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=("PORTFOLIO_AUTHORIZATION_REQUIRED",),
        )
    preview = preview_for(authorization.proposal)
    if set(authorization.proposal.snapshot.selected_account_ids) != set(access_account_ids):
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=("AUTHORIZATION_SCOPE_MISMATCH",),
            preview=preview,
        )
    current, history_reason = _current_authorization(history, command.portfolio_id)
    if history_reason is not None:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=(history_reason,),
            preview=preview,
        )
    now = datetime.fromisoformat(observed_at)
    expired = now >= authorization.proposal.risk_budget.expires_at
    if command.requested_action == "DETERMINISTIC_PROTECTION":
        allowed = True
        reasons = ("DETERMINISTIC_PROTECTION_RETAINED",)
    elif current is None or current.authorization_id != authorization.authorization_id:
        allowed = False
        reasons = ("CURRENT_PORTFOLIO_AUTHORIZATION_REQUIRED",)
    elif expired:
        allowed = False
        reasons = ("RISK_BUDGET_EXPIRED",)
    else:
        allowed = True
        reasons = ("NEW_EXPOSURE_AUTHORIZED",)
    return PortfolioAuthorizationOutcome(
        disposition="APPROVED" if allowed else "DENIED",
        reasons=reasons,
        preview=preview,
        usage=PortfolioAuthorizationUsage(
            requested_action=command.requested_action,
            allowed=allowed,
            authorization_snapshot=authorization,
            retained_protection_floor=authorization.proposal.risk_budget.protection_floor,
            unfinished_cash_obligations=authorization.proposal.cash_obligations,
            reasons=reasons,
            checked_at=now,
        ),
    )


def _current_authorization(
    history: tuple[PortfolioAuthorizationOutcome, ...], portfolio_id: str
) -> tuple[PortfolioAuthorization | None, str | None]:
    authorizations = tuple(
        outcome.authorization
        for outcome in history
        if outcome.authorization is not None
        and outcome.authorization.proposal.portfolio_id == portfolio_id
    )
    if not authorizations:
        return None, None
    superseded = {
        authorization.previous_authorization_id
        for authorization in authorizations
        if authorization.previous_authorization_id is not None
    }
    current = tuple(
        authorization
        for authorization in authorizations
        if authorization.authorization_id not in superseded
    )
    if len(current) != 1:
        return None, "AUTHORIZATION_HISTORY_CONFLICT"
    return current[0], None


def _authorization_by_id(
    history: tuple[PortfolioAuthorizationOutcome, ...],
    portfolio_id: str,
    authorization_id: str,
) -> PortfolioAuthorization | None:
    return next(
        (
            outcome.authorization
            for outcome in history
            if outcome.authorization is not None
            and outcome.authorization.proposal.portfolio_id == portfolio_id
            and outcome.authorization.authorization_id == authorization_id
        ),
        None,
    )
