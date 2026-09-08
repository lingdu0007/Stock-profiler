import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { AlertTriangle, ArrowUpRight, RefreshCw } from "lucide-react";
import { Link, Navigate, useLocation } from "react-router";

import {
  ApiResponseError,
  appendUserFact,
  fetchMonitoringWorkspace,
  type FormalReport,
  type UserFactRequest
} from "./api/client";
import { ExecutionPlanEvidence } from "./ExecutionPlanEvidence";

type Outcome = NonNullable<FormalReport["result"]["monitoring"]>;
type Case = Outcome["cases"][number];

function monitoringTime(value: string | null | undefined): string {
  if (!value) return "Unknown";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23"
  }).formatToParts(new Date(value));
  const part = (name: string) => parts.find((item) => item.type === name)?.value;
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")} Asia/Shanghai`;
}

function CaseSummary({ item, reportId }: { item: Case; reportId: string }) {
  return (
    <article className="monitoring-item">
      <div className="monitoring-item-heading">
        <span className={`priority ${item.priority.toLowerCase()}`}>
          <AlertTriangle size={16} aria-hidden="true" /> {item.priority}
        </span>
        <strong>{item.priority === "P0" ? "Immediate protection" : "Risk remediation"}</strong>
        <Link to={`/reports/${reportId}`} aria-label={`Open ${item.case_id}`}>
          <ArrowUpRight size={18} aria-hidden="true" />
        </Link>
      </div>
      <p>Deterministic obligation persists</p>
      <dl className="record-list">
        <Fact
          label="Quantity"
          value={item.quantity_status === "UNKNOWN" ? "Quantity unknown" : "Verified at assessment"}
        />
        <Fact label="Established" value={monitoringTime(item.first_established_at)} />
        <Fact label="Last qualified review" value={monitoringTime(item.last_reviewed_at)} />
        <Fact label="Case" value={item.case_id} />
        {item.required_targets?.map((target) => (
          <Fact
            key={target.security_id}
            label={`${target.security_id} persistent target`}
            value={`${target.direction} / ${target.target_quantity}`}
          />
        ))}
      </dl>
    </article>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="record-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function MonitoringActions({ report }: { report: FormalReport }) {
  const client = useQueryClient();
  const [choice, setChoice] = useState<NonNullable<UserFactRequest["choice"]>>("DEFER");
  const [declaration, setDeclaration] =
    useState<NonNullable<UserFactRequest["declaration"]>>("PREPARING");
  const keys = useRef(new Map<string, string>());
  const mutation = useMutation({
    mutationFn: (request: Omit<UserFactRequest, "idempotency_key">) => {
      const identity = `${report.report_version_id}:${JSON.stringify(request)}`;
      const key = keys.current.get(identity) ?? crypto.randomUUID();
      keys.current.set(identity, key);
      return appendUserFact(report.report_version_id, { ...request, idempotency_key: key });
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ["monitoring"] })
  });
  const monitoring = report.result.monitoring;
  const canConfirm =
    monitoring?.disposition === "ASSESSED" &&
    monitoring.cases.some(
      (item) => item.plan?.requires_confirmation && item.plan.disposition !== "BLOCKED"
    );
  return (
    <section className="report-section" aria-label="User facts">
      <h3>User facts</h3>
      <div className="monitoring-actions">
        <button disabled={mutation.isPending} onClick={() => mutation.mutate({ kind: "VIEWED" })}>
          Record viewed
        </button>
        <button
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ kind: "ACKNOWLEDGED" })}
        >
          Acknowledge risk
        </button>
      </div>
      {canConfirm && (
        <div className="monitoring-actions">
          <label>
            Exact plan choice
            <select
              value={choice}
              onChange={(event) => setChoice(event.target.value as typeof choice)}
            >
              <option value="ACCEPT">Accept</option>
              <option value="DECLINE">Decline</option>
              <option value="DEFER">Defer</option>
            </select>
          </label>
          <button
            disabled={mutation.isPending}
            onClick={() => mutation.mutate({ kind: "CONFIRMED", choice })}
          >
            Record plan choice
          </button>
        </div>
      )}
      <div className="monitoring-actions">
        <label>
          Execution declaration
          <select
            value={declaration}
            onChange={(event) => setDeclaration(event.target.value as typeof declaration)}
          >
            <option value="PREPARING">Preparing</option>
            <option value="REPORTED_SUBMITTED">Reported submitted</option>
            <option value="REPORTED_PARTIAL">Reported partially filled</option>
            <option value="REPORTED_FILLED">Reported filled</option>
            <option value="REPORTED_CANCELLED">Reported cancelled</option>
            <option value="UNABLE">Unable to execute</option>
          </select>
        </label>
        <button
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ kind: "EXECUTION_DECLARED", declaration })}
        >
          Record execution declaration
        </button>
      </div>
      {mutation.isError && (
        <p role="alert">Fact not recorded. The saved plan or access may no longer be valid.</p>
      )}
      {mutation.isSuccess && (
        <p role="status">
          {mutation.data.kind} recorded.{" "}
          {mutation.data.reconciliation_status === "PENDING"
            ? "Authoritative reconciliation pending."
            : "Risk obligation unchanged."}
        </p>
      )}
    </section>
  );
}

export function MonitoringEvidence({ report }: { report: FormalReport }) {
  const outcome = report.result.monitoring;
  if (!outcome) return null;
  const freshness = outcome.freshness;
  return (
    <section className="report-section monitoring-evidence" aria-label="Monitoring assessment">
      <h2>{outcome.kind.replaceAll("_", " ")}</h2>
      <p>{outcome.disposition}</p>
      {outcome.reasons.length > 0 && <p className="error-message">{outcome.reasons.join(", ")}</p>}
      <dl className="record-list">
        <Fact label="Evidence cutoff" value={monitoringTime(report.knowledge_cutoff)} />
        <Fact label="Assessment" value={monitoringTime(freshness?.assessed_at)} />
        <Fact label="Report generated" value={monitoringTime(report.generated_at)} />
        <Fact
          label="Reliably saved"
          value={monitoringTime(report.monitoring_publication?.committed_at)}
        />
        <Fact
          label="Published"
          value={monitoringTime(report.monitoring_publication?.published_at)}
        />
        <Fact label="Market" value={freshness?.market_status ?? "Unknown"} />
        <Fact
          label="Next window"
          value={
            freshness
              ? `${monitoringTime(freshness.next_window_start)} to ${monitoringTime(freshness.next_window_end)}`
              : "Unknown"
          }
        />
      </dl>
      {freshness?.evidence_families?.map((family) => (
        <details className="report-section" key={family.family}>
          <summary>
            {family.family.replaceAll("_", " ")}: {family.status}
          </summary>
          <p>{family.reasons.join(", ")}</p>
          <dl className="record-list">
            <Fact label="Source" value={family.evidence?.source ?? "Unknown"} />
            <Fact label="Version" value={family.evidence?.source_version ?? "Unknown"} />
            <Fact
              label="Effective"
              value={monitoringTime(family.evidence?.business_effective_at)}
            />
            <Fact label="Observed" value={monitoringTime(family.evidence?.source_observed_at)} />
            <Fact label="Acquired" value={monitoringTime(family.evidence?.locally_acquired_at)} />
            <Fact label="Validated" value={monitoringTime(family.evidence?.validated_at)} />
            <Fact label="Cutoff" value={monitoringTime(family.evidence?.cutoff_at)} />
          </dl>
        </details>
      ))}
      {outcome.notification_due_at && (
        <p>Deferred notification due: {monitoringTime(outcome.notification_due_at)}</p>
      )}
      {outcome.cases.map((item) => (
        <CaseSummary key={item.case_id} item={item} reportId={report.report_version_id} />
      ))}
      {outcome.cases
        .filter(
          (item, index, items) =>
            item.plan &&
            items.findIndex((candidate) => candidate.source_event_id === item.source_event_id) ===
              index
        )
        .map(
          (item) =>
            item.plan && <ExecutionPlanEvidence key={item.source_event_id} plan={item.plan} />
        )}
      {outcome.cases.length > 0 && ["DAILY_CLOSE", "EVENT_REASSESS"].includes(outcome.kind) && (
        <MonitoringActions report={report} />
      )}
      {outcome.action_units.length > 0 && (
        <div className="table-scroll">
          <table>
            <caption>Account action units</caption>
            <thead>
              <tr>
                <th>Security / Account</th>
                <th>Quantity</th>
                <th>Sellable</th>
                <th>Evidence validated</th>
              </tr>
            </thead>
            <tbody>
              {outcome.action_units.map((unit) => (
                <tr key={`${unit.account_id}:${unit.position_id}`}>
                  <th>
                    {unit.security_id}
                    <small>{unit.account_id}</small>
                  </th>
                  <td>{unit.total_quantity ?? "Unknown"}</td>
                  <td>{unit.broker_sellable_quantity ?? "Unknown"}</td>
                  <td>{monitoringTime(unit.position_evidence.validated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {outcome.notifications.map((attempt) => (
        <div className="report-section" key={attempt.notification_id}>
          <h3>
            {attempt.role}: {attempt.result}
          </h3>
          <p>{monitoringTime(attempt.attempted_at)} | Delivery unknown</p>
          <p>{attempt.body}</p>
        </div>
      ))}
      {outcome.reconciliation && (
        <p>
          Authoritative reconciliation: {outcome.reconciliation.disposition}. Risk discharge
          requires its own decision.
        </p>
      )}
      {outcome.source_report_ids.map((id) => (
        <p key={id}>
          <Link to={`/reports/${id}`}>{id}</Link>
        </p>
      ))}
    </section>
  );
}

export function MonitoringPage() {
  const { pathname } = useLocation();
  const query = useQuery({ queryKey: ["monitoring"], queryFn: fetchMonitoringWorkspace });
  if (query.error instanceof ApiResponseError && query.error.status === 401) {
    return <Navigate replace to={`/sign-in?next=${encodeURIComponent(pathname)}`} />;
  }
  const archive = pathname.endsWith("/archive");
  const inbox = pathname.endsWith("/inbox");
  const current =
    query.data?.reports.filter((report) =>
      query.data?.current_report_ids.includes(report.report_version_id)
    ) ?? [];
  return (
    <section className="monitoring-layout" aria-label="Position monitoring">
      <div className="monitoring-toolbar">
        <h2>
          {archive ? "Monitoring audit archive" : inbox ? "Action inbox" : "Position overview"}
        </h2>
        <button
          className="icon-button"
          title="Refresh saved records"
          aria-label="Refresh saved records"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          <RefreshCw size={18} />
        </button>
      </div>
      <p className="monitoring-boundary">
        Daily-close and validated-event assessments. No automatic orders.
      </p>
      {query.isPending && <p role="status">Loading monitoring records</p>}
      {query.isError && <p role="alert">Monitoring unavailable</p>}
      {query.data && query.data.reports.length === 0 && <p>No published monitoring assessments.</p>}
      {archive ? (
        query.data?.reports.map((report) => (
          <article className="report-section" key={report.report_version_id}>
            <h3>{report.result.monitoring?.kind.replaceAll("_", " ")}</h3>
            <p>
              {monitoringTime(report.knowledge_cutoff)} | {report.result.monitoring?.disposition}
            </p>
            {report.corrects_event_id && <p>Corrects {report.corrects_event_id}</p>}
            <Link to={`/reports/${report.report_version_id}`}>{report.report_version_id}</Link>
            {query.data?.user_facts
              .filter((fact) => fact.report_version_id === report.report_version_id)
              .map((fact) => (
                <p key={fact.fact_id}>
                  {fact.kind} | {fact.choice ?? fact.declaration ?? "Recorded"} |{" "}
                  {monitoringTime(fact.recorded_at)}
                </p>
              ))}
          </article>
        ))
      ) : inbox ? (
        <>
          {query.data && query.data.inbox.length === 0 && <p>No outstanding monitoring cases.</p>}
          {query.data?.inbox.map((item) => {
            const report = current.find((candidate) =>
              candidate.result.monitoring?.cases.some((entry) => entry.case_id === item.case_id)
            );
            return (
              report && (
                <CaseSummary key={item.case_id} item={item} reportId={report.report_version_id} />
              )
            );
          })}
        </>
      ) : (
        current.map((report) => (
          <MonitoringEvidence key={report.report_version_id} report={report} />
        ))
      )}
    </section>
  );
}
