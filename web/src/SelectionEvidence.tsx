import type { FormalReport } from "./api/client";

type Selection = NonNullable<FormalReport["result"]["selection"]>;

export function SelectionEvidence({ selection }: { selection: Selection }) {
  const facts = [
    ["Use", "Non-actionable"],
    ["Order", "Research priority"],
    ["Knowledge cutoff", selection.cutoff_at],
    ["Universe event", selection.universe_event_id],
    ["Policy version", selection.policy.version_id],
    ["Qualification scope", selection.qualification_scope],
    ["Valid monthly cohort", selection.population.valid_monthly ? "Yes" : "No"],
    [
      "Recommendation coverage denominator",
      selection.population.recommendation_coverage_denominator ? "Included" : "Excluded"
    ],
    ["Availability failure", selection.population.availability_failure ?? "None"],
    ["Reasons", selection.reasons.join(", ") || "None"]
  ];
  return (
    <section className="report-section" aria-label="Monthly selection">
      <h2>Monthly selection</h2>
      <p className="outcome-code">{selection.disposition}</p>
      <dl className="record-list">
        {facts.map(([label, value]) => (
          <div className="record-row" key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <div className="portfolio-subsection">
        <h3>Frozen members</h3>
        {selection.members.length ? (
          <ol className="selection-ranking">
            {selection.members.map((member) => (
              <li key={member}>{member}</li>
            ))}
          </ol>
        ) : (
          <p>No frozen members</p>
        )}
      </div>
      <details className="portfolio-subsection">
        <summary>Full research ranking ({selection.ranking.length})</summary>
        <ol className="selection-ranking">
          {selection.ranking.map((row) => (
            <li key={row.security_id}>
              {row.security_id}: {row.composite_score}
            </li>
          ))}
        </ol>
      </details>
      <details className="portfolio-subsection">
        <summary>Diversification scan ({selection.scan.length})</summary>
        <dl className="record-list">
          {selection.scan.map((step) => (
            <div className="record-row" key={step.security_id}>
              <dt>{step.security_id}</dt>
              <dd>
                {step.included ? "Included during scan" : step.reasons.join(", ")}
                {step.correlated_with.length > 0 && `: ${step.correlated_with.join(", ")}`}
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </section>
  );
}
