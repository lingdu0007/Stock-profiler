import { useQuery, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { KeyRound, ShieldCheck } from "lucide-react";
import { useState } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
  useSearchParams
} from "react-router";

import {
  ApiResponseError,
  completePasskeyAuthentication,
  fetchFormalReport,
  type FormalReport
} from "./api/client";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      gcTime: 0,
      refetchOnWindowFocus: false,
      retry: false,
      staleTime: 0
    }
  }
});

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="page">
      <header className="masthead">
        <p className="eyebrow">
          <ShieldCheck aria-hidden="true" size={16} strokeWidth={2} />
          Private decision record
        </p>
        <h1>Stock Profiler</h1>
      </header>
      {children}
    </main>
  );
}

function ReportPage() {
  const { reportVersionId } = useParams();
  const reportQuery = useQuery({
    queryKey: ["formal-report", reportVersionId],
    queryFn: () => fetchFormalReport(reportVersionId ?? ""),
    enabled: Boolean(reportVersionId)
  });

  if (reportQuery.error instanceof ApiResponseError && reportQuery.error.status === 401) {
    const next = encodeURIComponent(`/reports/${reportVersionId}`);
    return <Navigate replace to={`/sign-in?next=${next}`} />;
  }

  return (
    <Shell>
      {reportQuery.isPending && <p aria-live="polite">Loading report</p>}
      {reportQuery.isError && <p aria-live="polite">Report unavailable</p>}
      {reportQuery.data && <FormalReportView report={reportQuery.data} />}
    </Shell>
  );
}

function FormalReportView({ report }: { report: FormalReport }) {
  return (
    <section aria-label="Formal report" className="report-layout">
      <div className="report-heading">
        <p className="synthetic-marker">D0 synthetic</p>
        <p className="outcome-code">{report.result.outcome_code}</p>
        <p className="report-summary">{report.result.summary}</p>
      </div>

      <section className="report-section" aria-label="Key reasons">
        <h2>Key reasons</h2>
        <ol className="reason-list">
          {report.result.key_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ol>
      </section>

      <section className="report-section" aria-label="Report record">
        <h2>Record</h2>
        <dl className="record-list">
          <Record label="Report version" value={report.report_version_id} />
          <Record label="Business event" value={report.event_id} />
          <Record label="M-Agent Run" value={report.framework_run_id} />
          <Record label="Business object" value={report.business_object_id} />
          <Record label="Knowledge cutoff" value={report.knowledge_cutoff} />
          <Record label="Evidence validated" value={report.evidence_clock.validated_at} />
          <Record label="Generated" value={report.generated_at} />
          <Record label="Qualification scope" value={report.qualification_scope} />
        </dl>
      </section>

      <section className="report-section" aria-label="Version bundle">
        <h2>Version bundle</h2>
        <dl className="record-list">
          <Record label="Case contract" value={report.version_bundle.case_contract_version} />
          <Record label="Host contract" value={report.version_bundle.host_contract_version} />
          <Record
            label="Agent definition"
            value={`${report.version_bundle.agent_definition_id} @ ${report.version_bundle.agent_definition_version}`}
          />
          <Record label="Output contract" value={report.version_bundle.output_contract_version} />
          <Record label="M-Agent" value={report.version_bundle.m_agent_version} />
          <Record label="Release commit" value={report.version_bundle.m_agent_release_commit} />
        </dl>
      </section>
    </section>
  );
}

function Record({ label, value }: { label: string; value: string }) {
  return (
    <div className="record-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function SignInPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function signIn() {
    setError(null);
    setIsSubmitting(true);
    try {
      await completePasskeyAuthentication();
      navigate(safeReturnPath(searchParams.get("next")));
    } catch {
      setError("Passkey verification was not completed.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <Shell>
      <section aria-label="Passkey sign-in" className="sign-in">
        <h2>Sign in</h2>
        <button className="passkey-button" disabled={isSubmitting} onClick={() => void signIn()}>
          <KeyRound aria-hidden="true" size={18} />
          {isSubmitting ? "Verifying Passkey" : "Continue with Passkey"}
        </button>
        {error && (
          <p aria-live="polite" className="error-message">
            {error}
          </p>
        )}
      </section>
    </Shell>
  );
}

function safeReturnPath(value: string | null): string {
  return value?.startsWith("/reports/") ? value : "/";
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/reports/:reportVersionId" element={<ReportPage />} />
          <Route path="/sign-in" element={<SignInPage />} />
          <Route path="*" element={<Navigate replace to="/sign-in" />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
