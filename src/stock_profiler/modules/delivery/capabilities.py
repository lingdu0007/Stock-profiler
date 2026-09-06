"""A description of the host's registered, non-executable capability surface."""

from pydantic import BaseModel, ConfigDict


class CapabilityInventory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    definition_id: str
    definition_version: str
    context_provider: bool
    tool_names: tuple[str, ...]
    model_routes: tuple[str, ...]
    session_enabled: bool
    order_credentials: bool
