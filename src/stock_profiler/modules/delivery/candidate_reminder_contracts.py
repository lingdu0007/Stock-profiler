"""Frozen synthetic reminder observations; no provider or user action capability."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ReminderContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateReminderRequest(ReminderContract):
    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    kind: Literal["INITIAL", "PLAN_READY", "FINAL", "CORRECTION"]
    quiet_until: AwareDatetime | None = None
    reminders_enabled: bool = True
    retry_of: str | None = None
    primary_result: Literal["ACCEPTED", "REJECTED", "UNKNOWN", "TIMEOUT"]
    fallback_result: Literal["ACCEPTED", "REJECTED", "UNKNOWN", "TIMEOUT"]


class CandidateReminderBody(ReminderContract):
    result_type: str
    candidate_count: int = Field(ge=0)
    window_ends_at: AwareDatetime | None = None
    entry_path: str


class CandidateReminderChannel(ReminderContract):
    role: Literal["PRIMARY", "PERSISTENT"]
    result: Literal["ACCEPTED", "REJECTED", "UNKNOWN", "TIMEOUT"]


class CandidateReminderRecord(ReminderContract):
    contract_version: Literal["1.0.0"] = "1.0.0"
    intent_id: str
    attempt_id: str
    report_version_id: str
    plan_month: str
    kind: Literal["INITIAL", "PLAN_READY", "FINAL", "CORRECTION"]
    request_digest: str
    recorded_at: AwareDatetime
    body: CandidateReminderBody
    channels: tuple[CandidateReminderChannel, ...]
    status: Literal["ATTEMPTED", "DEFERRED", "DISABLED"]
    deferred_until: AwareDatetime | None = None
    retry_of: str | None = None
    path_completed: bool
