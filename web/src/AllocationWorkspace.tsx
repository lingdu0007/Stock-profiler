import { useEffect, useState } from "react";
import { Link } from "react-router";
import {
  ApiResponseError,
  reauthenticateCandidateSession,
  submitCandidateCommand,
  type CandidateWorkspace,
  type CandidateWorkspaceCommand,
  type FormalReport
} from "./api/client";
import { CandidateAllocationEvidence } from "./CandidateAllocationEvidence";
import { CandidateConfirmationEvidence } from "./CandidateConfirmationEvidence";
import { CandidateExecutionEvidence } from "./CandidateExecutionEvidence";
import { useCandidateSubmission } from "./candidate-submissions";

type View = NonNullable<CandidateWorkspace["allocations"]>[number];
type Choice = "ACCEPT" | "DECLINE" | "DEFER";

function SavedRecord({ report }: { report: FormalReport }) {
  return (
    <div className="portfolio-subsection">
      <p>
        <Link to={`/reports/${report.report_version_id}`}>
          Read saved report {report.report_version_id}
        </Link>{" "}
        · Event {report.event_id} · Cutoff {report.knowledge_cutoff}
      </p>
      {report.result.candidate_confirmation && (
        <CandidateConfirmationEvidence confirmation={report.result.candidate_confirmation} />
      )}
      {report.result.candidate_execution && (
        <CandidateExecutionEvidence execution={report.result.candidate_execution} />
      )}
    </div>
  );
}

export function AllocationWorkspace({
  view,
  commandsPermitted,
  refresh
}: {
  view: View;
  commandsPermitted: boolean;
  refresh: () => Promise<unknown>;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const [draft, setDraft] = useState<Record<string, Choice>>({});
  const { pending, anyPending, setPending } = useCandidateSubmission(view.plan_event_id);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [review, setReview] = useState<{ identity: string; report: FormalReport } | null>(null);
  const [trigger, setTrigger] =
    useState<NonNullable<CandidateWorkspaceCommand["trigger_reason"]>>("FACTS_CHANGED");
  const [security, setSecurity] = useState("");
  const [status, setStatus] =
    useState<NonNullable<CandidateWorkspaceCommand["declaration"]>["status"]>("PREPARING");
  const [order, setOrder] = useState("");
  const [proof, setProof] = useState("");
  const confirmations =
    view.confirmations?.filter(
      (report) => report.result.candidate_confirmation?.disposition === "CONFIRMED"
    ) ?? [];
  const confirmation = confirmations.at(-1);
  const execution = view.executions?.at(-1);
  const identity = `${view.latest_input_report_version_id}:${confirmation?.event_id ?? ""}:${execution?.event_id ?? ""}`;
  const open = Boolean(
    view.valid_from &&
    view.valid_through &&
    Date.parse(view.valid_from) <= now &&
    now <= Date.parse(view.valid_through)
  );
  const allowed = commandsPermitted && view.new_actions_permitted && open && !busy && !anyPending;
  const canDeclare = commandsPermitted && Boolean(confirmation) && !busy && !anyPending;
  const replanningPermitted = view.stop_reasons.every((reason) =>
    ["PLAN_CHANGED", "PLAN_INVALIDATED", "PRICE_CAP_REQUIRED"].includes(reason)
  );
  const reviewed =
    review?.identity === identity &&
    review.report.result.candidate_confirmation?.disposition === "REVALIDATED";
  const rows = view.allocation.rows.filter((row) => Number(row.principal) > 0);

  async function send(request: CandidateWorkspaceCommand) {
    setBusy(true);
    setPending(request);
    setMessage("Submitting saved versions");
    try {
      const result = await submitCandidateCommand(request);
      if (!result.report) {
        setMessage(
          "Submission outcome unknown. Reconcile the original submission before any new action."
        );
        return;
      }
      const outcome =
        result.report.result.candidate_confirmation ??
        result.report.result.candidate_execution ??
        result.report.result.candidate_allocation;
      if (request.operation === "REVIEW") setReview({ identity, report: result.report });
      else setReview(null);
      setMessage(
        request.operation === "REVIEW"
          ? `Policy replay: ${outcome?.disposition} ${outcome?.reasons.join(", ")}`
          : `Saved result: ${outcome?.disposition} ${outcome?.reasons.join(", ")}`
      );
      setPending(null);
      await refresh();
    } catch (error) {
      setMessage(
        error instanceof ApiResponseError && error.status === 403
          ? "Recent passkey verification is required. Verify, then reconcile the original submission."
          : "Submission outcome unknown. Reconcile the original submission before any new action."
      );
    } finally {
      setBusy(false);
    }
  }
  function command(
    operation: CandidateWorkspaceCommand["operation"],
    extra: Partial<CandidateWorkspaceCommand> = {}
  ) {
    void send({
      operation,
      plan_report_version_id: view.report_version_id,
      input_report_version_id: view.latest_input_report_version_id,
      seen_confirmation_id: confirmation?.event_id ?? null,
      seen_execution_id: execution?.event_id ?? null,
      idempotency_key: crypto.randomUUID(),
      choices: [],
      ...extra
    });
  }
  const choices = rows.map((row) => ({
    security_id: row.candidate.security_id,
    choice: draft[row.candidate.security_id] ?? ("DEFER" as Choice)
  }));
  return (
    <section
      className="report-section allocation-workspace"
      aria-label="Personal allocation and execution"
    >
      <h2>Personal allocation and execution</h2>
      <p>
        Candidate batch {view.allocation.candidate_batch_id} · Plan event {view.plan_event_id}
      </p>
      <p>
        Original entry window: {view.valid_from ?? "Unknown"} to {view.valid_through ?? "Unknown"}.
        Inputs: {view.latest_input_report_version_id}
      </p>
      <p>
        Saved account scope:{" "}
        {view.plan_report.access_scope?.account_ids.join(", ") ?? "Unavailable"}
      </p>
      {(!open || view.stop_reasons.length > 0) && (
        <p className="stage-reasons">
          New actions stopped: {view.stop_reasons.join(", ") || "ENTRY_WINDOW_CLOSED"}
        </p>
      )}
      <Link to={`/reports/${view.report_version_id}`}>Read original allocation report</Link>
      <CandidateAllocationEvidence plan={view.allocation} />
      <h3>Policy review records</h3>
      {view.reviews?.map((report) => (
        <SavedRecord key={report.report_version_id} report={report} />
      ))}
      <p aria-live="polite">{message}</p>
      {review && <SavedRecord report={review.report} />}
      {commandsPermitted && (
        <>
          <button disabled={!allowed} onClick={() => command("REVIEW")}>
            Replay current policy
          </button>
          <h3>Complete batch choice draft</h3>
          <p>Choices take effect only after the host saves a complete batch confirmation.</p>
          {rows.map((row) => (
            <label key={row.candidate.security_id}>
              Choice for {row.candidate.security_id}
              <select
                disabled={!allowed}
                value={draft[row.candidate.security_id] ?? "DEFER"}
                onChange={(event) =>
                  setDraft({ ...draft, [row.candidate.security_id]: event.target.value as Choice })
                }
              >
                <option value="DEFER">Defer</option>
                <option value="ACCEPT">Accept</option>
                <option value="DECLINE">Decline</option>
              </select>
            </label>
          ))}
          <button
            disabled={!allowed || !reviewed || rows.length === 0}
            onClick={() => command("CONFIRM", { choices })}
          >
            Confirm complete batch
          </button>
          <label>
            Replanning trigger
            <select
              disabled={busy || anyPending}
              value={trigger}
              onChange={(event) => setTrigger(event.target.value as typeof trigger)}
            >
              <option value="FACTS_CHANGED">Facts changed</option>
              <option value="POLICY_CHANGED">Policy changed</option>
              <option value="USER_COUNTERPROPOSAL">User counterproposal</option>
            </select>
          </label>
          <button
            disabled={!commandsPermitted || !open || !replanningPermitted || busy || anyPending}
            onClick={() => command("REPLAN", { trigger_reason: trigger })}
          >
            Replan within original window
          </button>
          {confirmation && (
            <>
              <button disabled={!allowed} onClick={() => command("REVIEW_STEP")}>
                Review next execution step
              </button>
              <h3>Execution declaration awaiting reconciliation</h3>
              <label>
                Declared security
                <select
                  value={security}
                  disabled={!canDeclare}
                  onChange={(event) => setSecurity(event.target.value)}
                >
                  <option value="">Select an accepted security</option>
                  {confirmation.result.candidate_confirmation?.choices
                    .filter((row) => row.choice === "ACCEPT")
                    .map((row) => (
                      <option key={row.security_id}>{row.security_id}</option>
                    ))}
                </select>
              </label>
              <label>
                Declared status
                <select
                  value={status}
                  disabled={!canDeclare}
                  onChange={(event) => setStatus(event.target.value as typeof status)}
                >
                  {[
                    "PREPARING",
                    "SUBMITTED",
                    "PARTIALLY_FILLED",
                    "FILLED",
                    "CANCELLED",
                    "UNABLE"
                  ].map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
              <label>
                Broker order reference (optional)
                <input
                  value={order}
                  disabled={!canDeclare}
                  onChange={(event) => setOrder(event.target.value)}
                />
              </label>
              <button
                disabled={!canDeclare || !security}
                onClick={() =>
                  command("DECLARE", {
                    declaration: {
                      security_id: security,
                      status,
                      broker_order_id: order || null,
                      declared_at: new Date().toISOString()
                    }
                  })
                }
              >
                Save pending declaration
              </button>
              <label>
                Saved authoritative order exclusion report
                <input
                  value={proof}
                  disabled={busy || anyPending}
                  onChange={(event) => setProof(event.target.value)}
                />
              </label>
              <button
                disabled={busy || anyPending || !proof || !rows.length}
                onClick={() =>
                  command("WITHDRAW", { choices, withdrawal_position_report_version_id: proof })
                }
              >
                Submit batch withdrawal for reconciliation
              </button>
            </>
          )}
          {pending && (
            <button disabled={busy} onClick={() => void send(pending)}>
              Reconcile original submission
            </button>
          )}
          <button
            disabled={busy}
            onClick={() =>
              void reauthenticateCandidateSession()
                .then(() => setMessage("Passkey verified; reconcile the original submission."))
                .catch(() => setMessage("Passkey verification unavailable."))
            }
          >
            Verify with passkey
          </button>
        </>
      )}
      <h3>Confirmation and reservation records</h3>
      {view.confirmations?.map((report) => (
        <SavedRecord key={report.report_version_id} report={report} />
      ))}
      <h3>Authoritative execution and pending declarations</h3>
      {view.executions?.map((report) => (
        <SavedRecord key={report.report_version_id} report={report} />
      ))}
    </section>
  );
}
