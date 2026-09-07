"""Immutable implementation identity shared by decisions and qualification."""

from pydantic import BaseModel, ConfigDict


class MAgentRelease(BaseModel):
    """An exact runtime artifact identity, independent of a task's creation date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    m_agent_version: str
    m_agent_wheel_url: str
    m_agent_wheel_sha256: str
    m_agent_release_commit: str


CURRENT_M_AGENT_RELEASE = MAgentRelease(
    m_agent_version="0.5.1",
    m_agent_wheel_url=(
        "https://github.com/lingdu0007/M-Agent/releases/download/v0.5.1/"
        "m_agent-0.5.1-py3-none-any.whl"
    ),
    m_agent_wheel_sha256="7528e768c36890d4005c90f2f24a97ae96714b96f8d8c5655542e6289004fbaa",
    m_agent_release_commit="99dd386b6f2c93645334ec81c9791f3b0333d597",
)

HISTORICAL_M_AGENT_RELEASE = MAgentRelease(
    m_agent_version="0.5.0",
    m_agent_wheel_url=(
        "https://github.com/lingdu0007/M-Agent/releases/download/v0.5.0/"
        "m_agent-0.5.0-py3-none-any.whl"
    ),
    m_agent_wheel_sha256="8c2592715e840f5d8da4ce239c663864d0c24a16fa05edcefef09071c4fb59a6",
    m_agent_release_commit="743651e5c74a4865f25a31dab68d188b5b0aed64",
)


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

    @property
    def runtime_release(self) -> MAgentRelease:
        return MAgentRelease.model_validate(
            self.model_dump(include=set(MAgentRelease.model_fields))
        )
