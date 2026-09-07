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
  completePasskeyRegistration,
  fetchFormalReport,
  fetchVersionBundle,
  type FormalReport,
  type VersionBundle
} from "./api/client";

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        gcTime: 0,
        refetchOnWindowFocus: false,
        retry: false,
        staleTime: 0
      }
    }
  });
}

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

const versionLabels: Array<[keyof VersionBundle, string]> = [
  ["application_version", "Application"],
  ["source_sha", "Source SHA"],
  ["m_agent_version", "M-Agent"],
  ["m_agent_wheel_url", "Release Wheel URL"],
  ["m_agent_release_commit", "M-Agent Release commit"],
  ["m_agent_wheel_sha256", "Wheel SHA-256"]
];

function VersionDiagnostics() {
  const versionQuery = useQuery({
    queryKey: ["diagnostics", "version"],
    queryFn: fetchVersionBundle
  });

  return (
    <Shell>
      <section aria-live="polite" aria-label="Version bundle" className="report-section">
        <h2>Version bundle</h2>
        {versionQuery.isPending && <p>Loading</p>}
        {versionQuery.isError && <p>Diagnostics unavailable</p>}
        {versionQuery.data && (
          <dl className="record-list">
            {versionLabels.map(([key, label]) => (
              <Record key={key} label={label} value={versionQuery.data[key]} />
            ))}
          </dl>
        )}
      </section>
    </Shell>
  );
}

function FormalReportView({ report }: { report: FormalReport }) {
  const correction = report.result.correction_evidence;
  const portfolio = report.result.portfolio;
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

      {correction && (
        <section className="report-section" aria-label="Correction evidence">
          <h2>Correction evidence</h2>
          <dl className="record-list">
            <Record label="Evidence" value={correction.evidence_id} />
            <Record label="Corrects evidence" value={correction.corrects_evidence_id} />
            <Record label="Source" value={correction.source} />
            <Record label="Reason" value={correction.reason} />
            <Record label="Original statement" value={correction.original_statement} />
            <Record label="Corrected statement" value={correction.corrected_statement} />
            <Record label="Fact effective" value={correction.evidence_clock.fact_effective_at} />
            <Record
              label="Source published"
              value={correction.evidence_clock.source_published_at}
            />
            <Record label="Acquired" value={correction.evidence_clock.acquired_at} />
            <Record label="Validated" value={correction.evidence_clock.validated_at} />
            <Record label="Correction cutoff" value={correction.knowledge_cutoff} />
            <Record label="Correction formed" value={correction.decision_formed_at} />
          </dl>
        </section>
      )}

      {portfolio && <PortfolioAuthorizationEvidence portfolio={portfolio} />}

      <section className="report-section" aria-label="Decision stages">
        <h2>Decision stages</h2>
        <ol className="stage-list">
          {report.stage_results.map((stage, index) => (
            <li key={`${stage.phase}-${stage.status}-${index}`} className="stage-row">
              <div className="stage-heading">
                <span className="stage-phase">{stage.phase}</span>
                <span className="stage-status">{stage.status}</span>
              </div>
              <dl className="stage-gates">
                {stage.gate_results.map((gate) => (
                  <div key={gate.gate_id}>
                    <dt>{gate.gate_id}</dt>
                    <dd>{gate.status}</dd>
                  </div>
                ))}
              </dl>
              {stage.reasons.length > 0 && (
                <p className="stage-reasons">{stage.reasons.join(", ")}</p>
              )}
            </li>
          ))}
        </ol>
      </section>

      <section className="report-section" aria-label="Report record">
        <h2>Record</h2>
        <dl className="record-list">
          <Record label="Report version" value={report.report_version_id} />
          <Record label="Business event" value={report.event_id} />
          {report.corrects_event_id && (
            <Record label="Corrects event" value={report.corrects_event_id} />
          )}
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

type PortfolioAuthorizationOutcome = NonNullable<FormalReport["result"]["portfolio"]>;
type PortfolioPreview = NonNullable<PortfolioAuthorizationOutcome["preview"]>;
type PortfolioAuthorization = NonNullable<PortfolioAuthorizationOutcome["authorization"]>;
type PortfolioAuthorizationUsage = NonNullable<PortfolioAuthorizationOutcome["usage"]>;

function PortfolioAuthorizationEvidence({
  portfolio
}: {
  portfolio: PortfolioAuthorizationOutcome;
}) {
  const authorization = portfolio.authorization ?? portfolio.usage?.authorization_snapshot;
  return (
    <section className="report-section" aria-label="Portfolio authorization">
      <h2>Portfolio authorization</h2>
      <dl className="record-list">
        <Record label="Disposition" value={portfolio.disposition} />
        <Record label="Reasons" value={portfolio.reasons.join(", ")} />
      </dl>
      {portfolio.preview && <PortfolioPreviewEvidence preview={portfolio.preview} />}
      {authorization && <PortfolioConfirmationEvidence authorization={authorization} />}
      {portfolio.usage && <PortfolioUsageEvidence usage={portfolio.usage} />}
    </section>
  );
}

function PortfolioPreviewEvidence({ preview }: { preview: PortfolioPreview }) {
  return (
    <section className="portfolio-subsection" aria-label="Portfolio scope preview">
      <h3>Scope preview</h3>
      <dl className="record-list">
        <Record label="Portfolio" value={preview.portfolio_id} />
        <Record label="Snapshot" value={preview.snapshot_id} />
        {preview.included_accounts.map((account) => (
          <Record
            key={account.account_id}
            label={`Included ${account.account_id}`}
            value={formatAccountSnapshot(account)}
          />
        ))}
        {preview.excluded_accounts.map((account) => (
          <Record
            key={account.account_id}
            label={`Excluded ${account.account_id}`}
            value={`${account.account_type} | ${account.reason}`}
          />
        ))}
        {preview.blocking_accounts.map((account) => (
          <Record
            key={account.account_id}
            label={`Blocked ${account.account_id}`}
            value={`${account.account_type} | ${account.reason}`}
          />
        ))}
      </dl>
    </section>
  );
}

function PortfolioConfirmationEvidence({
  authorization
}: {
  authorization: PortfolioAuthorization;
}) {
  const { confirmation, proposal } = authorization;
  const { risk_budget: budget } = proposal;
  return (
    <section className="portfolio-subsection" aria-label="Portfolio confirmation">
      <h3>Confirmation</h3>
      <dl className="record-list">
        <Record label="Authorization" value={authorization.authorization_id} />
        {authorization.previous_authorization_id && (
          <Record label="Previous authorization" value={authorization.previous_authorization_id} />
        )}
        <Record label="Recorded" value={authorization.recorded_at} />
        <Record label="Confirmation" value={confirmation.confirmation_id} />
        <Record label="Confirmed" value={confirmation.confirmed_at} />
        <Record label="Selected snapshot" value={proposal.snapshot.snapshot_id} />
        <Record label="Risk budget" value={budget.version_id} />
        <Record label="Effective" value={budget.effective_at} />
        <Record label="Expires" value={budget.expires_at} />
        {proposal.snapshot.accounts
          .filter((account) => proposal.snapshot.selected_account_ids.includes(account.account_id))
          .map((account) => (
            <Record
              key={`selection-${account.account_id}`}
              label={`Selected account ${account.account_id}`}
              value={formatAccountSnapshot(account)}
            />
          ))}
        {proposal.activation_snapshot && (
          <>
            <Record label="Activation snapshot" value={proposal.activation_snapshot.snapshot_id} />
            <Record label="Activation cutoff" value={proposal.activation_snapshot.cutoff_at} />
            {proposal.activation_snapshot.accounts
              .filter((account) =>
                proposal.activation_snapshot?.selected_account_ids.includes(account.account_id)
              )
              .map((account) => (
                <Record
                  key={`activation-${account.account_id}`}
                  label={`Activation account ${account.account_id}`}
                  value={formatAccountSnapshot(account)}
                />
              ))}
          </>
        )}
        <Record
          label="Concentration"
          value={`${budget.concentration.target_ratio} target | ${budget.concentration.hard_ratio} hard`}
        />
        <Record
          label="Stress"
          value={`${budget.stress.target_ratio} target | ${budget.stress.hard_ratio} hard`}
        />
        <Record
          label="Cash"
          value={`${budget.cash.target_ratio} target | ${budget.cash.hard_ratio} hard`}
        />
        <Record
          label="Drawdown"
          value={`${budget.drawdown.caution_ratio} caution | ${budget.drawdown.defensive_ratio} defensive | ${budget.drawdown.preservation_ratio} preservation`}
        />
        <Record label="Downside grid" value={budget.downside_grid.join(", ")} />
        <Record
          label="Protection floor"
          value={budget.protection_floor.retained_directions.join(", ")}
        />
        {confirmation.relaxation_evidence && (
          <>
            <Record
              label="Risk relaxation evidence"
              value={confirmation.relaxation_evidence.evidence_id}
            />
            <Record
              label="Normal state through"
              value={confirmation.relaxation_evidence.normal_through_at}
            />
            <Record
              label="Normal market sessions"
              value={String(confirmation.relaxation_evidence.normal_market_sessions.length)}
            />
            <Record
              label="Market calendar"
              value={
                confirmation.relaxation_evidence.normal_market_sessions[0]
                  ?.market_calendar_version_id ?? ""
              }
            />
            <Record
              label="Monthly activation cutoff"
              value={confirmation.relaxation_evidence.monthly_selection_cutoff_at}
            />
          </>
        )}
        {proposal.cash_obligations.map((obligation) => (
          <Record
            key={obligation.obligation_id}
            label={`Cash obligation ${obligation.obligation_id}`}
            value={formatCashObligation(obligation)}
          />
        ))}
      </dl>
    </section>
  );
}

function PortfolioUsageEvidence({ usage }: { usage: PortfolioAuthorizationUsage }) {
  return (
    <section className="portfolio-subsection" aria-label="Portfolio authorization use">
      <h3>Authorization use</h3>
      <dl className="record-list">
        <Record label="Requested action" value={usage.requested_action} />
        <Record label="Allowed" value={usage.allowed ? "ALLOWED" : "BLOCKED"} />
        <Record label="Checked" value={usage.checked_at} />
        <Record label="Reasons" value={usage.reasons.join(", ")} />
        <Record
          label="Authorization snapshot"
          value={usage.authorization_snapshot.authorization_id}
        />
        <Record
          label="Retained directions"
          value={usage.retained_protection_floor.retained_directions.join(", ")}
        />
        {usage.unfinished_cash_obligations.map((obligation) => (
          <Record
            key={obligation.obligation_id}
            label={`Unfinished obligation ${obligation.obligation_id}`}
            value={formatCashObligation(obligation)}
          />
        ))}
      </dl>
    </section>
  );
}

function formatAccountSnapshot(account: PortfolioPreview["included_accounts"][number]): string {
  return [
    `${account.account_type} | ${account.currency} | ${account.scope}`,
    `permissions ${account.permissions.join(", ")}`,
    `facts captured ${account.captured_at}`,
    `cash ${account.cash_fact_id}`,
    `positions ${account.positions_fact_id}`,
    `receivables ${account.receivables_fact_id}`,
    `payables ${account.payables_fact_id}`,
    `unfinished trades ${account.unfinished_trades_fact_id}`
  ].join(" | ");
}

function formatCashObligation(
  obligation: PortfolioAuthorization["proposal"]["cash_obligations"][number]
): string {
  return [
    obligation.amount,
    obligation.purpose,
    `latest ${obligation.latest_usable_at}`,
    `account ${obligation.target_account_id}`
  ].join(" | ");
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

function EnrollmentPage() {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const grantId = enrollmentGrantFromFragment();

  async function enroll() {
    if (!grantId) {
      setError("Enrollment authorization is unavailable.");
      return;
    }
    setError(null);
    setIsSubmitting(true);
    try {
      await completePasskeyRegistration(grantId);
      window.history.replaceState({}, "", "/sign-in");
      navigate("/sign-in", { replace: true });
    } catch {
      setError("Passkey enrollment was not completed.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <Shell>
      <section aria-label="Passkey enrollment" className="sign-in">
        <h2>Enroll Passkey</h2>
        {grantId ? (
          <button className="passkey-button" disabled={isSubmitting} onClick={() => void enroll()}>
            <KeyRound aria-hidden="true" size={18} />
            {isSubmitting ? "Verifying Passkey" : "Enroll Passkey"}
          </button>
        ) : (
          <p aria-live="polite" className="error-message">
            Enrollment authorization is unavailable.
          </p>
        )}
        {error && (
          <p aria-live="polite" className="error-message">
            {error}
          </p>
        )}
      </section>
    </Shell>
  );
}

function enrollmentGrantFromFragment(): string | null {
  const fragment = window.location.hash.slice(1);
  return fragment || null;
}

function safeReturnPath(value: string | null): string {
  return value?.startsWith("/reports/") ? value : "/";
}

export function App() {
  const [queryClient] = useState(createQueryClient);

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<VersionDiagnostics />} />
          <Route path="/reports/:reportVersionId" element={<ReportPage />} />
          <Route path="/sign-in" element={<SignInPage />} />
          <Route path="/enroll" element={<EnrollmentPage />} />
          <Route path="*" element={<Navigate replace to="/" />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
