"""Deterministic portfolio authorization adjudication for the frozen D0 seam."""

from datetime import datetime

from stock_profiler.modules.portfolio.contracts import (
    DatedCashObligation,
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
    knowledge_cutoff: str,
    history: tuple[PortfolioAuthorizationOutcome, ...],
    access_account_ids: tuple[str, ...],
    business_prerequisite_met: bool = True,
) -> PortfolioAuthorizationOutcome:
    """Preview a full scope or freeze its first confirmed synthetic authorization."""
    now = datetime.fromisoformat(observed_at)
    cutoff = datetime.fromisoformat(knowledge_cutoff)
    history = _history_available_by(history, cutoff)
    if isinstance(command, PortfolioUseCommand):
        return _adjudicate_use(
            command,
            now=now,
            history=history,
            access_account_ids=access_account_ids,
            business_prerequisite_met=business_prerequisite_met,
        )
    proposal_reason = _proposal_evidence_reason(command, cutoff, now)
    if proposal_reason is not None:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=(proposal_reason,),
        )
    preview = preview_for(command.proposal)
    if isinstance(command, PortfolioPreviewCommand):
        return PortfolioAuthorizationOutcome(
            disposition="PREVIEWED",
            reasons=("PORTFOLIO_SCOPE_PREVIEWED",),
            preview=preview,
        )
    assert isinstance(command, PortfolioConfirmationCommand)
    confirmation_reason = _confirmation_evidence_reason(command, cutoff, now)
    if confirmation_reason is not None:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=(confirmation_reason,),
        )
    if not business_prerequisite_met:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=("BUSINESS_PREREQUISITE_NOT_MET",),
            preview=preview,
        )
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
    elif (identity_reason := _reused_authorization_identity_reason(command, history)) is not None:
        reasons = (identity_reason,)
    elif (
        obligation_reason := _reused_cash_obligation_identity_reason(command, history)
    ) is not None:
        reasons = (obligation_reason,)
    elif (
        current is not None
        and command.proposal.risk_budget.effective_at <= current.proposal.risk_budget.effective_at
    ):
        reasons = ("RISK_BUDGET_NOT_FORWARD_EFFECTIVE",)
    elif (
        current is not None
        and command.proposal.risk_budget.relaxes(current.proposal.risk_budget)
        and (
            relaxation_reason := _risk_budget_relaxation_reason(
                command,
                current,
                history,
                cutoff,
            )
        )
        is not None
    ):
        reasons = (relaxation_reason,)
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
    now: datetime,
    history: tuple[PortfolioAuthorizationOutcome, ...],
    access_account_ids: tuple[str, ...],
    business_prerequisite_met: bool,
) -> PortfolioAuthorizationOutcome:
    if not business_prerequisite_met and command.requested_action == "NEW_EXPOSURE":
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=("BUSINESS_PREREQUISITE_NOT_MET",),
        )
    authorization = _authorization_by_id(history, command.portfolio_id, command.authorization_id)
    if authorization is None:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=("PORTFOLIO_AUTHORIZATION_REQUIRED",),
        )
    obligations = _unresolved_cash_obligations(history, command.portfolio_id, effective_by=now)
    required_account_ids = set(authorization.proposal.snapshot.account_ids) | {
        obligation.target_account_id for obligation in obligations
    }
    if not required_account_ids.issubset(access_account_ids):
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=("AUTHORIZATION_SCOPE_MISMATCH",),
        )
    preview = preview_for(authorization.proposal)
    _, history_reason = _current_authorization(history, command.portfolio_id)
    if history_reason is not None:
        return PortfolioAuthorizationOutcome(
            disposition="DENIED",
            reasons=(history_reason,),
            preview=preview,
        )
    if now < authorization.proposal.risk_budget.effective_at:
        allowed = False
        reasons = ("RISK_BUDGET_NOT_YET_EFFECTIVE",)
    elif command.requested_action == "DETERMINISTIC_PROTECTION":
        allowed = True
        reasons = ("DETERMINISTIC_PROTECTION_RETAINED",)
    else:
        active, active_reason = _active_authorization(history, command.portfolio_id, now)
        if active_reason is not None:
            return PortfolioAuthorizationOutcome(
                disposition="DENIED",
                reasons=(active_reason,),
                preview=preview,
            )
        if active is None or active.authorization_id != authorization.authorization_id:
            allowed = False
            reasons = ("CURRENT_PORTFOLIO_AUTHORIZATION_REQUIRED",)
        elif now >= authorization.proposal.risk_budget.expires_at:
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
            unfinished_cash_obligations=obligations,
            reasons=reasons,
            checked_at=now,
        ),
    )


def _current_authorization(
    history: tuple[PortfolioAuthorizationOutcome, ...], portfolio_id: str
) -> tuple[PortfolioAuthorization | None, str | None]:
    return _lineage_leaf(_portfolio_authorizations(history, portfolio_id))


def _active_authorization(
    history: tuple[PortfolioAuthorizationOutcome, ...],
    portfolio_id: str,
    observed_at: datetime,
) -> tuple[PortfolioAuthorization | None, str | None]:
    return _lineage_leaf(
        tuple(
            authorization
            for authorization in _portfolio_authorizations(history, portfolio_id)
            if authorization.proposal.risk_budget.effective_at <= observed_at
        )
    )


def _lineage_leaf(
    authorizations: tuple[PortfolioAuthorization, ...],
) -> tuple[PortfolioAuthorization | None, str | None]:
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


def _reused_authorization_identity_reason(
    command: PortfolioConfirmationCommand,
    history: tuple[PortfolioAuthorizationOutcome, ...],
) -> str | None:
    authorizations = _portfolio_authorizations(history, command.proposal.portfolio_id)
    if any(
        authorization.proposal.risk_budget.version_id == command.proposal.risk_budget.version_id
        for authorization in authorizations
    ):
        return "RISK_BUDGET_VERSION_REUSE"
    if any(
        authorization.proposal.snapshot.snapshot_id == command.proposal.snapshot.snapshot_id
        for authorization in authorizations
    ):
        return "PORTFOLIO_SNAPSHOT_VERSION_REUSE"
    if any(
        authorization.confirmation.confirmation_id == command.confirmation.confirmation_id
        for authorization in authorizations
    ):
        return "PORTFOLIO_CONFIRMATION_REUSE"
    return None


def _reused_cash_obligation_identity_reason(
    command: PortfolioConfirmationCommand,
    history: tuple[PortfolioAuthorizationOutcome, ...],
) -> str | None:
    existing_ids = {
        obligation.obligation_id
        for authorization in _portfolio_authorizations(history, command.proposal.portfolio_id)
        for obligation in authorization.proposal.cash_obligations
    }
    if existing_ids.intersection(
        obligation.obligation_id for obligation in command.proposal.cash_obligations
    ):
        return "CASH_OBLIGATION_ID_REUSE"
    return None


def _risk_budget_relaxation_reason(
    command: PortfolioConfirmationCommand,
    current: PortfolioAuthorization,
    history: tuple[PortfolioAuthorizationOutcome, ...],
    cutoff: datetime,
) -> str | None:
    evidence = command.confirmation.relaxation_evidence
    if evidence is None:
        return "RISK_BUDGET_RELAXATION_EVIDENCE_REQUIRED"
    if (
        evidence.predecessor_authorization_id != current.authorization_id
        or evidence.predecessor_risk_budget_version_id != current.proposal.risk_budget.version_id
        or evidence.normal_from_at != current.proposal.risk_budget.effective_at
        or evidence.normal_through_at != command.confirmation.confirmed_at
        or evidence.available_at > command.confirmation.confirmed_at
        or evidence.available_at > cutoff
        or evidence.monthly_selection_cutoff_at != command.proposal.risk_budget.effective_at
    ):
        return "RISK_BUDGET_RELAXATION_EVIDENCE_INVALID"
    if _unresolved_cash_obligations(history, command.proposal.portfolio_id):
        return "RISK_BUDGET_RELAXATION_OBLIGATIONS_UNRESOLVED"
    return None


def _proposal_evidence_reason(
    command: PortfolioPreviewCommand | PortfolioConfirmationCommand,
    cutoff: datetime,
    now: datetime,
) -> str | None:
    if not command.proposal.evidence_available_by(cutoff):
        return "PORTFOLIO_EVIDENCE_AFTER_CUTOFF"
    if not command.proposal.evidence_available_by(now):
        return "PORTFOLIO_EVIDENCE_NOT_AVAILABLE"
    return None


def _confirmation_evidence_reason(
    command: PortfolioConfirmationCommand,
    cutoff: datetime,
    now: datetime,
) -> str | None:
    evidence = command.confirmation.relaxation_evidence
    if command.confirmation.confirmed_at > cutoff or (
        evidence is not None and evidence.available_at > cutoff
    ):
        return "PORTFOLIO_EVIDENCE_AFTER_CUTOFF"
    if command.confirmation.confirmed_at > now or (
        evidence is not None and evidence.available_at > now
    ):
        return "PORTFOLIO_EVIDENCE_NOT_AVAILABLE"
    return None


def _history_available_by(
    history: tuple[PortfolioAuthorizationOutcome, ...],
    cutoff: datetime,
) -> tuple[PortfolioAuthorizationOutcome, ...]:
    return tuple(
        outcome
        for outcome in history
        if outcome.authorization is None or outcome.authorization.evidence_available_by(cutoff)
    )


def _unresolved_cash_obligations(
    history: tuple[PortfolioAuthorizationOutcome, ...],
    portfolio_id: str,
    *,
    effective_by: datetime | None = None,
) -> tuple[DatedCashObligation, ...]:
    obligations: dict[str, DatedCashObligation] = {}
    for authorization in _portfolio_authorizations(history, portfolio_id):
        if (
            effective_by is not None
            and authorization.proposal.risk_budget.effective_at > effective_by
        ):
            continue
        for obligation in authorization.proposal.cash_obligations:
            obligations.setdefault(obligation.obligation_id, obligation)
    return tuple(obligations.values())


def _portfolio_authorizations(
    history: tuple[PortfolioAuthorizationOutcome, ...], portfolio_id: str
) -> tuple[PortfolioAuthorization, ...]:
    return tuple(
        outcome.authorization
        for outcome in history
        if outcome.authorization is not None
        and outcome.authorization.proposal.portfolio_id == portfolio_id
    )


def _authorization_by_id(
    history: tuple[PortfolioAuthorizationOutcome, ...],
    portfolio_id: str,
    authorization_id: str,
) -> PortfolioAuthorization | None:
    return next(
        (
            authorization
            for authorization in _portfolio_authorizations(history, portfolio_id)
            if authorization.authorization_id == authorization_id
        ),
        None,
    )
