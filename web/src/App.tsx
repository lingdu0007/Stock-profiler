import { useEffect, useState } from "react";

import { fetchVersionBundle, type VersionBundle } from "./api/client";

type VersionState =
  { status: "loading" } | { status: "ready"; bundle: VersionBundle } | { status: "unavailable" };

const labels: Array<[keyof VersionBundle, string]> = [
  ["application_version", "Application"],
  ["source_sha", "Source SHA"],
  ["m_agent_version", "M-Agent"],
  ["m_agent_wheel_url", "Release Wheel URL"],
  ["m_agent_release_commit", "M-Agent Release commit"],
  ["m_agent_wheel_sha256", "Wheel SHA-256"]
];

export function App() {
  const [state, setState] = useState<VersionState>({ status: "loading" });

  useEffect(() => {
    void fetchVersionBundle()
      .then((bundle) => setState({ status: "ready", bundle }))
      .catch(() => setState({ status: "unavailable" }));
  }, []);

  return (
    <main className="page">
      <header className="masthead">
        <p className="eyebrow">Engineering baseline</p>
        <h1>Stock Profiler</h1>
      </header>
      <section aria-live="polite" aria-label="Version bundle" className="version-panel">
        {state.status === "loading" && <p>Loading</p>}
        {state.status === "unavailable" && <p>Diagnostics unavailable</p>}
        {state.status === "ready" && (
          <dl>
            {labels.map(([key, label]) => (
              <div className="version-row" key={key}>
                <dt>{label}</dt>
                <dd>{state.bundle[key]}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>
    </main>
  );
}
