import type { FormalReport } from "./api/client";

type Plan = NonNullable<FormalReport["result"]["execution_plan"]>;
const labels: Record<Plan["disposition"], string> = {
  PLANNED: "Plan awaiting confirmation",
  BLOCKED: "Execution allocation blocked",
  RECONFIRMATION_REQUIRED: "Reconfirmation required"
};

function Fact({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="record-row">
      <dt>{label}</dt>
      <dd>{value ?? "Unknown"}</dd>
    </div>
  );
}

export function ExecutionPlanEvidence({ plan }: { plan: Plan }) {
  return (
    <section className="report-section" aria-label="Execution plan">
      <h2>Execution plan</h2>
      <p className="outcome-code">{labels[plan.disposition]}</p>
      <p className="stage-reasons">{plan.reasons.join(", ")}</p>
      <dl className="record-list">
        <Fact label="Risk obligations" value="Not restored by this plan" />
        <Fact
          label="New exposure"
          value={plan.new_exposure_blocked ? "Blocked" : "No additional restriction"}
        />
        <Fact
          label="Confirmation"
          value={
            plan.requires_confirmation
              ? "Required for this exact version"
              : "No executable allocation"
          }
        />
        <Fact label="Cost routing" value={plan.cost_routing_basis} />
        <Fact label="Existing-target sale value" value={plan.initial_target_sale_value} />
        <Fact label="Projected stress gap" value={plan.projected_stress_gap} />
        <Fact label="Projected cash gap" value={plan.projected_cash_gap} />
        <Fact label="Projected exposure gap" value={plan.projected_exposure_gap} />
      </dl>
      {plan.targets.map((target) => (
        <div className="portfolio-subsection" key={target.security_id}>
          <h3>{target.security_id}</h3>
          {target.rounding_induced_full_sale && (
            <p className="outcome-code">Trading-unit full sale: reconfirmation required</p>
          )}
          <dl className="record-list">
            <Fact label="Action direction" value={target.direction} />
            <Fact label="Quantity ceiling" value={target.target_quantity} />
            <Fact label="Required sale quantity" value={target.required_sale_quantity} />
            <Fact label="Projected remaining quantity" value={target.remaining_quantity} />
            <Fact label="Uncompleted target gap" value={target.remaining_gap} />
            <Fact label="Source obligations" value={target.source_obligation_ids.join(", ")} />
          </dl>
        </div>
      ))}
      {plan.legs.map((leg) => (
        <div className="portfolio-subsection" key={`${leg.account_id}:${leg.security_id}`}>
          <h3>
            {leg.security_id}: {leg.account_id}
          </h3>
          <dl className="record-list">
            <Fact label="Legal sale quantity" value={leg.quantity} />
            <Fact label="Gross proceeds" value={leg.gross_proceeds} />
            <Fact label="Disposal cost" value={leg.disposal_cost} />
            <Fact label="Net proceeds" value={leg.net_proceeds} />
            <Fact label="First sellable window" value={leg.route.first_sellable_at} />
            <Fact label="Transferable at" value={leg.route.transferable_at} />
            <Fact label="Rule source" value={leg.route.rules_evidence.source} />
            <Fact label="Cost curve" value={leg.route.cost_curve?.version_id} />
            <Fact
              label="Conservative bound"
              value={leg.route.conservative_cost_curve?.version_id}
            />
          </dl>
        </div>
      ))}
    </section>
  );
}
