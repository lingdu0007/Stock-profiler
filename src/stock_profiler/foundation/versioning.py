"""Version identity shared by every public entrypoint."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.metadata import version

from stock_profiler.bootstrap.settings import Settings

APPLICATION_VERSION = "0.1.0.dev0"
M_AGENT_DISTRIBUTION = "m-agent"
M_AGENT_WHEEL_URL = (
    "https://github.com/lingdu0007/M-Agent/releases/download/v0.5.0/m_agent-0.5.0-py3-none-any.whl"
)
M_AGENT_WHEEL_SHA256 = "8c2592715e840f5d8da4ce239c663864d0c24a16fa05edcefef09071c4fb59a6"
M_AGENT_RELEASE_COMMIT = "743651e5c74a4865f25a31dab68d188b5b0aed64"


@dataclass(frozen=True)
class VersionBundle:
    """The diagnostic identity emitted by all processes and delivery surfaces."""

    application_version: str
    source_sha: str
    m_agent_version: str
    m_agent_wheel_url: str
    m_agent_wheel_sha256: str
    m_agent_release_commit: str

    def to_dto(self) -> dict[str, str]:
        return asdict(self)


def build_version_bundle(settings: Settings) -> VersionBundle:
    """Read the installed M-Agent distribution instead of copying a source checkout."""
    return VersionBundle(
        application_version=APPLICATION_VERSION,
        source_sha=settings.source_sha,
        m_agent_version=version(M_AGENT_DISTRIBUTION),
        m_agent_wheel_url=M_AGENT_WHEEL_URL,
        m_agent_wheel_sha256=M_AGENT_WHEEL_SHA256,
        m_agent_release_commit=M_AGENT_RELEASE_COMMIT,
    )
