"""Version identity shared by every public entrypoint."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.metadata import version

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.decision_versions import CURRENT_M_AGENT_RELEASE

APPLICATION_VERSION = "0.1.0.dev0"
M_AGENT_DISTRIBUTION = "m-agent"
M_AGENT_WHEEL_URL = CURRENT_M_AGENT_RELEASE.m_agent_wheel_url
M_AGENT_WHEEL_SHA256 = CURRENT_M_AGENT_RELEASE.m_agent_wheel_sha256
M_AGENT_RELEASE_COMMIT = CURRENT_M_AGENT_RELEASE.m_agent_release_commit


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
