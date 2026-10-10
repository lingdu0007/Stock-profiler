import type { FormalReport } from "./api/client";
import { ReportRecord as Record } from "./ReportRecord";

type Allocation = NonNullable<FormalReport["result"]["candidate_allocation"]>;
const outcomes: Record<Allocation["rows"][number]["outcome"], string> = {
  FULLY_ALLOCATED: "Fully allocated",
  PARTIALLY_ALLOCATED: "Partially allocated",
  UNALLOCATED: "Unallocated"
};
const states: Record<Allocation["disposition"], string> = {
  PLANNED: "Allocation awaiting confirmation",
  BLOCKED: "Allocation blocked",
  AWAITING_PRICE_CAP: "Continuous allocation awaiting a confirmed price cap"
};

export function CandidateAllocationEvidence({ plan }: { plan: Allocation }) {
  return (
    <section className="report-section" aria-label="Candidate allocation">
      <h2>Candidate allocation</h2>
      <p className="outcome-code">{states[plan.disposition]}</p>
      <p>
        Original candidate conclusions retained. This saved plan does not reserve cash or submit
        orders.
      </p>
      <p className="stage-reasons">{plan.reasons.join(", ")}</p>
      <dl className="record-list">
        <Record label="Plan version" value={plan.plan_id ?? "Unavailable"} />
        <Record label="Formed at" value={plan.formed_at ?? "Unknown"} />
        <Record label="Candidate batch" value={plan.candidate_batch_id ?? "Unknown"} />
        <Record label="Source conclusion" value={plan.candidate_conclusion_version ?? "Unknown"} />
        <Record
          label="Complete portfolio snapshot"
          value={plan.position_snapshot_id ?? "Unknown"}
        />
        <Record label="Risk budget" value={plan.risk_budget_version_id ?? "Unknown"} />
        <Record label="Allocated principal" value={String(plan.total_principal)} />
        <Record
          label="Remaining deployable cash"
          value={String(plan.remaining_cash ?? "Unknown")}
        />
        <Record label="Suggested sequence" value={plan.purchase_sequence.join(", ")} />
      </dl>
      {plan.beta && (
        <section aria-label="Saved coverage envelope">
          <h3>Saved coverage envelope</h3>
          <p>Synthetic contract evidence; real activation is unavailable.</p>
          <dl className="record-list">
            <Record label="Envelope identity" value={plan.beta.activation_id} />
            <Record label="Permission envelope" value={plan.beta.permission_envelope} />
            <Record label="Coverage ratio" value={String(plan.beta.ratio)} />
            <Record
              label="Portfolio net liquidation equity"
              value={String(plan.beta.net_liquidation_equity)}
            />
            <Record
              label="Normal feasible capacity"
              value={String(plan.beta.normal_feasible_capacity)}
            />
            <Record
              label="Coverage already committed"
              value={String(plan.beta.committed_exposure)}
            />
            <Record label="Available coverage" value={String(plan.beta.capacity)} />
            <Record
              label="Qualification decisions"
              value={plan.beta.qualification_decision_ids.join(", ")}
            />
          </dl>
          <p className="stage-reasons">{plan.beta.reasons.join(", ")}</p>
          {plan.beta.operations && (
            <div>
              <p>Planned months: {plan.beta.operations.plan_months.join(", ")}</p>
              <p>Missing months: {plan.beta.operations.missing_months.join(", ") || "None"}</p>
              <p>
                Consecutive core failure: {String(plan.beta.operations.consecutive_core_failure)}
              </p>
              <p>Safety failures: {plan.beta.operations.safety_failures.join(", ") || "None"}</p>
              {Object.entries(plan.beta.operations.metrics).map(([name, metric]) => (
                <p key={name}>
                  {name}: {metric.status}; {metric.completed}/{metric.required}; rate{" "}
                  {metric.rate ?? "Not applicable"}
                </p>
              ))}
            </div>
          )}
        </section>
      )}
      <p>
        Replanning source: {plan.replaces_plan_event_id ?? "Original plan"} · Trigger:{" "}
        {plan.replanning_reason ?? "None"}
      </p>
      {(plan.capacity_checks ?? []).length > 0 && (
        <div className="table-scroll">
          <table aria-label="Saved capacity margins">
            <thead>
              <tr>
                <th>Capacity gate</th>
                <th>Account / securities</th>
                <th>Already committed</th>
                <th>Available before</th>
                <th>Remaining after plan</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {plan.capacity_checks?.map((check) => (
                <tr key={check.gate_id}>
                  <td>{check.gate_id}</td>
                  <td>
                    {check.account_id ?? "Portfolio"} / {check.security_ids.join(", ")}
                  </td>
                  <td>{check.committed_margin}</td>
                  <td>{check.available_before}</td>
                  <td>{check.remaining_after_plan}</td>
                  <td>{check.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p>
        Position facts cutoff: {plan.position_snapshot?.cutoff_at ?? "Unknown"} · Price / route
        freshness:{" "}
        {plan.routes
          ?.map(
            (route) =>
              `${route.account_id}: ${route.evidence.cutoff_at} to ${route.evidence.expires_at ?? "Unknown"}`
          )
          .join("; ") || "Unknown"}
      </p>
      {plan.rows.map((row) => (
        <div
          className="portfolio-subsection"
          key={`${row.candidate.security_id}:${row.candidate.research_id}`}
        >
          <h3>{row.candidate.security_id}</h3>
          <p className="outcome-code">{outcomes[row.outcome]}</p>
          <p className="stage-reasons">{row.reasons.join(", ")}</p>
          <dl className="record-list">
            <Record label="Candidate identity" value={row.candidate.research_id} />
            <Record
              label="Frozen probability"
              value={String(row.candidate.calibrated_probability ?? "Unknown")}
            />
            <Record label="Issuer" value={row.issuer_id} />
            <Record
              label="Existing and committed exposure"
              value={String(row.committed_exposure)}
            />
            <Record label="Issuer target gap" value={String(row.target_gap)} />
            <Record label="Continuous principal" value={String(row.continuous_principal)} />
            <Record label="Legal allocated principal" value={String(row.principal)} />
            <Record label="Primary allocation reason" value={row.primary_reason ?? "None"} />
          </dl>
          {(row.route_failures ?? []).map((failure) => (
            <p className="stage-reasons" key={failure.route.account_id}>
              {failure.route.account_id}: {failure.reasons.join(", ")}
            </p>
          ))}
          {(row.comparisons ?? []).length > 0 && (
            <div className="table-scroll">
              <table aria-label={`Allocation priority for ${row.candidate.security_id}`}>
                <thead>
                  <tr>
                    <th>Criterion</th>
                    <th>Selected plan</th>
                    <th>Plan covering this candidate</th>
                    <th>Comparison</th>
                  </tr>
                </thead>
                <tbody>
                  {(row.comparisons ?? []).map((entry) => (
                    <tr key={entry.criterion}>
                      <td>{entry.criterion}</td>
                      <td>{entry.selected_value.join(", ")}</td>
                      <td>{entry.alternative_value?.join(", ") ?? "Infeasible"}</td>
                      <td>{entry.relation}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {row.legs.map((leg) => (
            <dl className="record-list" key={leg.route.account_id}>
              <Record label="Account" value={leg.route.account_id} />
              <Record label="Legal buy quantity" value={String(leg.quantity)} />
              <Record label="Confirmed price cap" value={String(leg.route.price_cap)} />
              <Record label="Purchase cost" value={String(leg.purchase_cost)} />
              <Record
                label="Cost and rule versions"
                value={`${leg.route.version_id}; ${leg.route.rule_version}`}
              />
            </dl>
          ))}
        </div>
      ))}
    </section>
  );
}
