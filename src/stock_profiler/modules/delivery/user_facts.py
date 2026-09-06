"""Independent, non-authoritative user facts for the synthetic report journey."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

UserFactKind = Literal["VIEWED", "ACKNOWLEDGED", "CONFIRMED", "EXECUTION_DECLARED"]


class UserFactRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: UserFactKind
    idempotency_key: str = Field(min_length=1, max_length=128)
    choice: Literal["ACCEPT", "DECLINE", "DEFER"] | None = None
    declaration: (
        Literal[
            "PREPARING",
            "REPORTED_SUBMITTED",
            "REPORTED_PARTIAL",
            "REPORTED_FILLED",
            "REPORTED_CANCELLED",
            "UNABLE",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def keep_fact_meanings_separate(self) -> UserFactRequest:
        if (self.kind == "CONFIRMED") != (self.choice is not None):
            raise ValueError("only a confirmation requires a choice")
        if (self.kind == "EXECUTION_DECLARED") != (self.declaration is not None):
            raise ValueError("only an execution declaration requires a reported state")
        return self


class UserFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    report_version_id: str
    event_id: str
    kind: UserFactKind
    choice: Literal["ACCEPT", "DECLINE", "DEFER"] | None
    declaration: str | None
    reconciliation_status: Literal["NOT_APPLICABLE", "PENDING"]
    recorded_at: str
    synthetic: Literal[True] = True
    authoritative_execution: Literal[False] = False
