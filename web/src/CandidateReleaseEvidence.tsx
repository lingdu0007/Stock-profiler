import type { FormalReport } from "./api/client";

type CandidateRelease = NonNullable<FormalReport["result"]["candidate_release"]>;

export function CandidateReleaseEvidence({ release }: { release: CandidateRelease }) {
  return (
    <section className="report-section" aria-label="Candidate batch release">
      <h2>Candidate batch</h2>
      <p className="outcome-code">{release.disposition}</p>
      <dl className="record-list">
        <Record label="Batch" value={release.batch_id} />
        <Record label="Knowledge cutoff" value={release.knowledge_cutoff} />
        <Record label="Published" value={release.published_at} />
        <Record label="Capability version" value={release.capability_version} />
        <Record label="Qualification scope" value={release.qualification_scope} />
        <Record label="Market state" value={release.market_state} />
        <Record label="Market calendar" value={release.market_calendar_version} />
        <Record
          label="Five-session window"
          value={
            release.valid_market_dates.length === 5
              ? `${release.valid_market_dates[0]} through ${release.valid_market_dates[4]}`
              : "Unavailable"
          }
        />
        <Record
          label="Monthly coverage denominator"
          value={release.population.recommendation_coverage_denominator ? "Included" : "Excluded"}
        />
        <Record
          label="Availability failure"
          value={release.population.availability_failure ?? "None"}
        />
        <Record label="Reasons" value={release.reasons.join(", ") || "None"} />
      </dl>

      {release.calibration && (
        <div className="portfolio-subsection">
          <h3>Frozen calibration</h3>
          <dl className="record-list">
            <Record label="Calibrator" value={release.calibration.calibrator_version} />
            <Record label="Intercept" value={release.calibration.intercept} />
            <Record label="Slope" value={release.calibration.slope} />
            <Record
              label="Training window"
              value={`${release.calibration.training_window_months[0]} through ${release.calibration.training_window_months.at(-1)}`}
            />
            <Record label="Label watermark" value={release.calibration.label_watermark_at} />
            <Record
              label="Mature labels"
              value={String(release.calibration.training_record_count)}
            />
          </dl>
        </div>
      )}

      <div className="portfolio-subsection">
        <h3>Per-security decisions</h3>
        {release.members.length === 0 ? (
          <p>No security decisions were published.</p>
        ) : (
          release.members.map((member) => (
            <article className="portfolio-subsection" key={member.security_id}>
              <h3>{member.security_id}</h3>
              <dl className="record-list">
                <Record
                  label="Decision"
                  value={member.candidate ? "Candidate buy" : "Not a candidate"}
                />
                <Record label="Raw success score" value={member.raw_success_score} />
                <Record
                  label="Calibrated six-month probability"
                  value={member.calibrated_probability ?? "Unavailable"}
                />
                <Record label="Independent risk veto" value={member.risk_status} />
                <Record
                  label="Market-state qualification"
                  value={member.market_state_qualified ? "Valid" : "Not qualified"}
                />
                <Record label="Data" value={member.data_complete ? "Complete" : "Incomplete"} />
                <Record label="Evidence freshness" value={member.evidence_freshness} />
                <Record
                  label="Validity window"
                  value={member.valid_market_dates.map(String).join(", ")}
                />
                <Record label="Reasons" value={member.reasons.join(", ")} />
              </dl>
              <p>{member.thesis}</p>
              <h4>Principal risks</h4>
              <ul>
                {member.principal_risks.map((risk) => (
                  <li key={risk}>{risk}</li>
                ))}
              </ul>
            </article>
          ))
        )}
      </div>
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
