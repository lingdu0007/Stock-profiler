"""Narrow user intent translated to the existing saved host command contract."""

from datetime import datetime
from hashlib import sha256
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from stock_profiler.modules.decision_cases.domain import FormalReport, FrozenDecisionCase
from stock_profiler.modules.delivery.access import AccessPrincipal, read_denial
from stock_profiler.modules.delivery.candidate_workspace import CandidateWorkspace
from stock_profiler.modules.portfolio.confirmation_contracts import (
    CandidateChoice,
    CandidateConfirmationCommand,
    SeenCandidateExecution,
)
from stock_profiler.modules.portfolio.execution_contracts import (
    CandidateExecutionCommand,
    ExecutionDeclaration,
)


class CandidateWorkspaceCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["CONFIRM", "REVIEW", "REVIEW_STEP", "DECLARE", "REPLAN", "WITHDRAW"]
    plan_report_version_id: str = Field(min_length=1)
    input_report_version_id: str = Field(min_length=1)
    seen_confirmation_id: str | None
    seen_execution_id: str | None
    idempotency_key: str = Field(min_length=1, max_length=200)
    choices: tuple[CandidateChoice, ...] = ()
    declaration: ExecutionDeclaration | None = None
    withdrawal_position_report_version_id: str | None = None
    trigger_reason: Literal["FACTS_CHANGED", "POLICY_CHANGED", "USER_COUNTERPROPOSAL"] | None = None

    @property
    def request_fingerprint(self) -> str:
        return "workspace-request-" + sha256(self.model_dump_json().encode()).hexdigest()

    @property
    def business_identity(self) -> str:
        return "candidate-workspace:" + sha256(self.idempotency_key.encode()).hexdigest()


class CandidateCommandSource(Protocol):
    def read_report(
        self, report_id: str, principal: AccessPrincipal | None = None
    ) -> FormalReport | None: ...
    def candidate_workspace(
        self, principal: AccessPrincipal | None
    ) -> CandidateWorkspace | None: ...
    def saved_command_case(self, identity: str) -> FrozenDecisionCase | None: ...
    def report_input_case(self, event_id: str) -> FrozenDecisionCase | None: ...
    def observed_at(self) -> str: ...
    def record_capability_denial(self, target: str, surface: str) -> None: ...


def resolve_candidate_command(
    request: CandidateWorkspaceCommand,
    principal: AccessPrincipal | None,
    source: CandidateCommandSource,
) -> FrozenDecisionCase:
    if (
        principal is None
        or "CANDIDATE_ALLOCATION" not in principal.permissions
        or read_denial(principal, principal.account_ids, permission="CANDIDATE_COMMAND") is not None
    ):
        source.record_capability_denial(request.plan_report_version_id, "HTTP")
        raise ValueError("request is not permitted")
    plan = source.read_report(request.plan_report_version_id, principal)
    inputs = source.read_report(request.input_report_version_id, principal)
    if (
        plan is None
        or inputs is None
        or plan.access_scope is None
        or inputs.access_scope is None
        or not plan.access_scope.same_scope_as(inputs.access_scope)
    ):
        raise ValueError("request is not permitted")
    saved = source.saved_command_case(request.business_identity)
    if saved is not None:
        if saved.case_id != request.request_fingerprint:
            raise ValueError("idempotency payload conflict")
        return saved
    input_case = source.report_input_case(inputs.event_id)
    if input_case is None:
        raise ValueError("request is not permitted")
    workspace = source.candidate_workspace(principal)
    view = (
        next((item for item in workspace.allocations if item.plan_event_id == plan.event_id), None)
        if workspace
        else None
    )
    if view is None:
        raise ValueError("request is not permitted")
    if request.input_report_version_id != view.latest_input_report_version_id and (
        request.operation == "REPLAN"
        or not any(
            reason in {"PLAN_INVALIDATED", "PLAN_CHANGED", "PLAN_SUPERSEDED"}
            for reason in view.stop_reasons
        )
    ):
        raise ValueError("input version conflict")
    confirmations = tuple(
        item
        for item in view.confirmations
        if item.result.candidate_confirmation is not None
        and item.result.candidate_confirmation.disposition == "CONFIRMED"
    )
    if request.operation == "REPLAN":
        latest_confirmation = confirmations[-1].event_id if confirmations else None
        latest_execution = view.executions[-1].event_id if view.executions else None
        if (
            request.seen_confirmation_id != latest_confirmation
            or request.seen_execution_id != latest_execution
        ):
            raise ValueError("seen version conflict")
    withdrawal = (
        source.read_report(request.withdrawal_position_report_version_id, principal)
        if request.withdrawal_position_report_version_id
        else None
    )
    if request.operation == "WITHDRAW" and withdrawal is None:
        raise ValueError("saved order exclusion required")
    return prepare_candidate_command(
        request,
        plan,
        input_case,
        datetime.fromisoformat(source.observed_at()),
        confirmations[-1] if confirmations else None,
        withdrawal,
    )


def prepare_candidate_command(
    request: CandidateWorkspaceCommand,
    plan_report: FormalReport,
    input_case: FrozenDecisionCase,
    now: datetime,
    confirmation_report: FormalReport | None = None,
    withdrawal_report: FormalReport | None = None,
) -> FrozenDecisionCase:
    plan = plan_report.result.candidate_allocation
    allocation = input_case.candidate_allocation
    if (
        plan is None
        or plan.risk_handoff is None
        or allocation is None
        or input_case.access_scope is None
    ):
        raise ValueError("saved allocation input required")
    if allocation.candidate_event_id != plan.candidate_event_id:
        raise ValueError("candidate input identity conflict")
    if request.operation == "REPLAN":
        if request.trigger_reason is None:
            raise ValueError("replanning trigger required")
        command_allocation = allocation.model_copy(
            update={
                "replaces_plan_event_id": plan_report.event_id,
                "replanning_reason": request.trigger_reason,
            }
        )
        payload = input_case.model_dump(mode="json")
        payload["case_id"] = request.request_fingerprint
        payload["business_identity"] = request.business_identity
        payload["report_generated_at"] = now.isoformat()
        payload["candidate_allocation"] = command_allocation.model_dump(mode="json")
        payload["input"]["candidate_allocation"] = payload["candidate_allocation"]
        return FrozenDecisionCase.model_validate(payload)
    if request.operation == "DECLARE":
        if confirmation_report is None or request.declaration is None:
            raise ValueError("saved confirmation and declaration required")
        if confirmation_report.event_id != request.seen_confirmation_id:
            raise ValueError("confirmation version conflict")
        declaration = request.declaration.model_copy(update={"declared_at": now})
        command_execution = CandidateExecutionCommand(
            contract_version="1.0.0",
            operation="DECLARE",
            portfolio_id=plan.risk_handoff.portfolio_id,
            plan_event_id=plan_report.event_id,
            confirmation_event_id=confirmation_report.event_id,
            idempotency_key=request.business_identity,
            cutoff_at=now,
            seen_execution_id=request.seen_execution_id,
            declaration=declaration,
        )
        return _case_with_command(input_case, request, command_execution, now, knowledge_cutoff=now)
    if request.operation in {"CONFIRM", "WITHDRAW"} and not request.choices:
        raise ValueError("complete explicit choices required")
    if request.operation == "REVIEW_STEP" and confirmation_report is None:
        raise ValueError("accepted plan required")
    prior = confirmation_report.result.candidate_confirmation if confirmation_report else None
    choices = (
        (
            prior.choices
            if prior is not None
            else tuple(
                CandidateChoice(security_id=row.candidate.security_id, choice="DEFER")
                for row in plan.rows
                if row.principal > 0
            )
        )
        if request.operation in {"REVIEW", "REVIEW_STEP"}
        else request.choices
    )
    command = CandidateConfirmationCommand(
        contract_version="1.0.0",
        operation=(
            "REVIEW"
            if request.operation == "REVIEW"
            else "WITHDRAW"
            if request.operation == "WITHDRAW"
            else "SUBMIT"
        ),
        user_id=input_case.access_scope.user_id,
        portfolio_id=plan.risk_handoff.portfolio_id,
        candidate_batch_id=plan.candidate_batch_id or "",
        plan_event_id=plan_report.event_id,
        plan_id=plan.plan_id or "",
        seen_confirmation_id=request.seen_confirmation_id,
        seen_execution=SeenCandidateExecution(execution_id=request.seen_execution_id),
        idempotency_key=request.business_identity,
        withdrawal_position_event_id=withdrawal_report.event_id if withdrawal_report else None,
        choices=choices,
        revalidation=allocation,
    )
    return _case_with_command(input_case, request, command, now)


def _case_with_command(
    input_case: FrozenDecisionCase,
    request: CandidateWorkspaceCommand,
    command: CandidateConfirmationCommand | CandidateExecutionCommand,
    now: datetime,
    *,
    knowledge_cutoff: datetime | None = None,
) -> FrozenDecisionCase:
    command_name, version = (
        ("candidate_execution", "candidate-execution.1.0.0")
        if isinstance(command, CandidateExecutionCommand)
        else ("candidate_confirmation", "candidate-confirmation.1.0.0")
    )
    payload = input_case.model_dump(mode="json")
    payload.pop("candidate_allocation")
    payload["input"].pop("candidate_allocation")
    payload[command_name] = command.model_dump(mode="json")
    payload["input"][command_name] = payload[command_name]
    payload["case_id"] = request.request_fingerprint
    payload["business_identity"] = request.business_identity
    payload["report_generated_at"] = now.isoformat()
    if knowledge_cutoff is not None:
        payload["knowledge_cutoff"] = knowledge_cutoff.isoformat()
    for field in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        payload["version_bundle"][field] = version
    return FrozenDecisionCase.model_validate(payload)
