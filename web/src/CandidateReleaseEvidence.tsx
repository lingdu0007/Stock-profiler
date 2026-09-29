import type { FormalReport } from "./api/client";
import { ReportRecord as Record } from "./ReportRecord";

type CandidateRelease = NonNullable<FormalReport["result"]["candidate_release"]>;
type ReportPublication = FormalReport["report_publication"];

export function CandidateReleaseEvidence({
  release,
  publication
}: {
  release: CandidateRelease;
  publication: ReportPublication;
}) {
  return (
    <section className="report-section" aria-label="Candidate batch release">
      <h2>Candidate batch</h2>
      <p className="outcome-code">{release.disposition}</p>
      <dl className="record-list">
        <Record label="Batch" value={release.batch_id} />
        <Record label="Knowledge cutoff" value={release.knowledge_cutoff} />
        <Record
          label="Published"
          value={
            publication?.status === "FAILED"
              ? "Not published"
              : publication?.published_at ?? "Unavailable"
          }
        />
        {publication?.status === "FAILED" && (
          <>
            <Record
              label="Publication failed"
              value={publication.failure_recorded_at ?? "Unavailable"}
            />
            <Record
              label="Publication failure reason"
              value={publication.failure_reason ?? "Unavailable"}
            />
          </>
        )}
        <Record label="Capability version" value={release.capability_version} />
        <Record label="Qualification scope" value={release.qualification_scope} />
        <Record
          label="Qualification decision"
          value={release.qualification?.qualification_id ?? "No matching qualification"}
        />
        <Record
          label="Qualification status"
          value={release.qualification?.status ?? "NOT_OBTAINED"}
        />
        <Record
          label="Qualification valid through"
          value={release.qualification?.valid_through ?? "Unavailable"}
        />
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
            <Record
              label="Out-of-sample log loss"
              value={release.calibration.out_of_sample_diagnostics.log_loss}
            />
            <Record
              label="Out-of-sample Brier score"
              value={release.calibration.out_of_sample_diagnostics.brier_score}
            />
            <Record
              label="Out-of-sample calibration intercept / slope"
              value={`${release.calibration.out_of_sample_diagnostics.calibration_intercept} / ${release.calibration.out_of_sample_diagnostics.calibration_slope}`}
            />
            <Record
              label="Out-of-sample reliability"
              value={reliabilitySummary(
                release.calibration.out_of_sample_diagnostics.reliability_curve
              )}
            />
            <Record
              label="Recent diagnostic window"
              value={`${release.calibration.recent_diagnostic_months[0]} through ${release.calibration.recent_diagnostic_months.at(-1)} (${release.calibration.recent_diagnostic_sample_count} records)`}
            />
            <Record
              label="Recent diagnostic log loss"
              value={
                release.calibration.recent_diagnostics?.log_loss ?? "Unavailable (<200 records)"
              }
            />
            <Record
              label="Recent diagnostic Brier score"
              value={
                release.calibration.recent_diagnostics?.brier_score ?? "Unavailable (<200 records)"
              }
            />
            <Record
              label="Recent calibration intercept / slope"
              value={
                release.calibration.recent_diagnostics
                  ? `${release.calibration.recent_diagnostics.calibration_intercept} / ${release.calibration.recent_diagnostics.calibration_slope}`
                  : "Unavailable (<200 records)"
              }
            />
            <Record
              label="Recent reliability"
              value={
                release.calibration.recent_diagnostics
                  ? reliabilitySummary(release.calibration.recent_diagnostics.reliability_curve)
                  : "Unavailable (<200 records)"
              }
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
                <Record label="Independent risk status" value={member.risk_status} />
                <Record
                  label="Independent risk gates"
                  value={member.risk_gates
                    .map((gate) => `${gate.gate_id}: ${gate.status}`)
                    .join(", ")}
                />
                <Record label="Independent risk reasons" value={member.risk_reasons.join(", ")} />
                <Record
                  label="Market-state qualification"
                  value={
                    member.market_state_qualification_status === "AT_RISK"
                      ? "At risk"
                      : member.market_state_qualification_status === "VALID"
                        ? "Valid"
                        : "Not qualified"
                  }
                />
                <Record label="Data" value={member.data_complete ? "Complete" : "Incomplete"} />
                <Record label="Evidence freshness" value={member.evidence_freshness} />
                <Record
                  label="Evidence clocks"
                  value={
                    member.evidence_clocks.length === 0
                      ? "Unavailable"
                      : member.evidence_clocks
                          .map(
                            (clock) =>
                              `${clock.evidence_id}: published ${clock.source_published_at ?? "unknown"}, acquired ${clock.acquired_at ?? "unknown"}, validated ${clock.validated_at ?? "unknown"}, cutoff ${clock.knowledge_cutoff}`
                          )
                          .join("; ")
                  }
                />
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

function reliabilitySummary(
  bins: NonNullable<
    CandidateRelease["calibration"]
  >["out_of_sample_diagnostics"]["reliability_curve"]
): string {
  return bins
    .map(
      (bin) =>
        `${bin.lower_probability}–${bin.upper_probability}: ${bin.mean_predicted_probability} → ${bin.observed_success_rate} (n=${bin.sample_count})`
    )
    .join("; ");
}
