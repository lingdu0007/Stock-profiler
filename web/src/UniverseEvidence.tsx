import type { FormalReport } from "./api/client";

type Universe = NonNullable<FormalReport["result"]["universe"]>;

function Fact({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="record-row">
      <dt>{label}</dt>
      <dd>{value ?? "Unknown"}</dd>
    </div>
  );
}

export function UniverseEvidence({ universe }: { universe: Universe }) {
  return (
    <section className="report-section" aria-label="Monthly universe">
      <h2>Monthly universe</h2>
      <p className="outcome-code">{universe.disposition}</p>
      <dl className="record-list">
        <Fact label="Use" value="Non-actionable" />
        <Fact label="Knowledge cutoff" value={universe.cutoff_at} />
        <Fact label="Policy version" value={universe.policy.version_id} />
        <Fact label="Manifest version" value={universe.manifest?.version_id} />
        <Fact label="Qualification scope" value={universe.qualification_scope} />
        <Fact label="Reasons" value={universe.reasons.join(", ") || "None"} />
      </dl>
      <div className="portfolio-subsection">
        <h3>Members</h3>
        {universe.members.length === 0 ? (
          <p>No eligible members</p>
        ) : (
          <ul>
            {universe.members.map((member) => (
              <li key={member}>{member}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="portfolio-subsection">
        <h3>Exclusions</h3>
        {universe.exclusions.length === 0 ? (
          <p>No exclusions</p>
        ) : (
          <dl className="record-list">
            {universe.exclusions.map((exclusion) => (
              <Fact
                key={exclusion.security_id}
                label={exclusion.security_id}
                value={exclusion.reasons.join(", ")}
              />
            ))}
          </dl>
        )}
      </div>
      {universe.board_qualifications?.map((qualification) => (
        <div className="portfolio-subsection" key={qualification.decision_id}>
          <h3>{qualification.scope.board}</h3>
          <dl className="record-list">
            <Fact label="Qualification" value={qualification.decision_id} />
            <Fact label="Status" value={qualification.status} />
            <Fact label="Purpose" value={qualification.scope.purpose} />
            <Fact label="Market state" value={qualification.scope.market_state} />
          </dl>
        </div>
      ))}
      {universe.manifest?.entries.map((entry, index) => (
        <details className="portfolio-subsection" key={`${entry.field_family}-${index}`}>
          <summary>
            {entry.field_family}: {entry.requirement}
          </summary>
          <dl className="record-list">
            <Fact label="Semantics" value={entry.semantics_version} />
            <Fact label="Source" value={entry.evidence?.source} />
            <Fact label="Source version" value={entry.evidence?.source_version} />
            <Fact label="License" value={entry.evidence?.license_id} />
            <Fact label="Licensed purposes" value={entry.evidence?.licensed_purposes.join(", ")} />
            <Fact label="License valid from" value={entry.evidence?.license_valid_from} />
            <Fact label="License valid until" value={entry.evidence?.license_valid_until} />
            <Fact label="Fact effective" value={entry.evidence?.fact_effective_at} />
            <Fact label="First published" value={entry.evidence?.source_published_at} />
            <Fact label="Source observed" value={entry.evidence?.source_observed_at} />
            <Fact label="Acquired" value={entry.evidence?.acquired_at} />
            <Fact label="Validated" value={entry.evidence?.validated_at} />
            <Fact label="Complete through" value={entry.evidence?.complete_through} />
            <Fact label="Content hash" value={entry.evidence?.content_sha256} />
            <Fact label="Substitution" value={entry.substitution?.certification_id ?? "None"} />
            <Fact label="Switch reason" value={entry.substitution?.reason ?? "None"} />
          </dl>
        </details>
      ))}
    </section>
  );
}
