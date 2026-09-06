"""Immutable implementation identity shared by decisions and qualification."""

from pydantic import BaseModel, ConfigDict


class DecisionCaseVersionBundle(BaseModel):
    """The complete implementation version set that participates in replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_contract_version: str
    host_contract_version: str
    host_application_version: str
    host_source_sha: str
    agent_definition_id: str
    agent_definition_version: str
    model_adapter_id: str
    routing_policy_version: str
    output_contract_version: str
    report_projection_contract_version: str
    m_agent_version: str
    m_agent_wheel_url: str
    m_agent_wheel_sha256: str
    m_agent_release_commit: str
