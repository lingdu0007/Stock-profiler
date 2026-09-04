import { Slot } from "@radix-ui/react-slot";
import { useQuery, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { BrowserRouter, Route, Routes } from "react-router";

import { fetchVersionBundle, type VersionBundle } from "./api/client";

const labels: Array<[keyof VersionBundle, string]> = [
  ["application_version", "Application"],
  ["source_sha", "Source SHA"],
  ["m_agent_version", "M-Agent"],
  ["m_agent_wheel_url", "Release Wheel URL"],
  ["m_agent_release_commit", "M-Agent Release commit"],
  ["m_agent_wheel_sha256", "Wheel SHA-256"]
];

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: false
    }
  }
});

function VersionDiagnostics() {
  const versionQuery = useQuery({
    queryKey: ["diagnostics", "version"],
    queryFn: fetchVersionBundle
  });

  return (
    <main className="page min-h-screen">
      <header className="masthead">
        <p className="eyebrow">
          <Slot className="masthead-icon">
            <ShieldCheck aria-hidden="true" size={16} strokeWidth={2} />
          </Slot>
          Engineering baseline
        </p>
        <h1>Stock Profiler</h1>
      </header>
      <section aria-live="polite" aria-label="Version bundle" className="version-panel">
        {versionQuery.isPending && <p>Loading</p>}
        {versionQuery.isError && <p>Diagnostics unavailable</p>}
        {versionQuery.data && (
          <dl>
            {labels.map(([key, label]) => (
              <div className="version-row" key={key}>
                <dt>{label}</dt>
                <dd>{versionQuery.data[key]}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>
    </main>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="*" element={<VersionDiagnostics />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
