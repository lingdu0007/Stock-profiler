import { NavLink } from "react-router";
import type { FormalReport } from "./api/client";
import { ReportRecord as Record } from "./ReportRecord";

type Evidence = NonNullable<FormalReport["result"]["historical_probability"]>;
const value = (item: unknown) =>
  item === null || item === undefined ? "Unavailable" : String(item);

export function HistoricalProbabilityEvidence({ evidence }: { evidence: Evidence }) {
  const inference = evidence.inference;
  return (
    <section className="report-section" aria-label="Historical probability evidence">
      <h2>Historical probability evidence</h2>
      <p>{evidence.disposition}</p>
      <p>D0 synthetic evidence. No recommendation or execution authorization.</p>
      <dl className="record-list">
        <Record label="Registered contract" value={evidence.registration.version_id} />
        <Record label="Saved report version" value={value(evidence.report_version)} />
        <Record label="Evidence cutoff" value={evidence.cutoff_at} />
        <Record
          label="Registered monthly window"
          value={`${evidence.registration.start_month} — ${evidence.registration.end_month}`}
        />
        <Record label="Frozen calibrator family" value={evidence.registration.calibrator_version} />
        {Object.entries(evidence.counts ?? {}).map(([key, count]) => (
          <Record key={key} label={key} value={value(count)} />
        ))}
      </dl>
      {evidence.previous_report_id && (
        <NavLink to={`/reports/${evidence.previous_report_id}`}>
          Read previous probability report
        </NavLink>
      )}
      {inference && (
        <>
          <h3>Independent high-probability scopes</h3>
          {Object.entries({ overall: inference.overall, ...inference.regimes }).map(
            ([scope, watermark]) => (
              <section className="portfolio-subsection" key={scope}>
                <h4>
                  {scope} · {watermark.disposition}
                </h4>
                <dl className="record-list">
                  {Object.entries(watermark).map(([key, item]) => (
                    <Record key={key} label={key} value={value(item)} />
                  ))}
                </dl>
              </section>
            )
          )}
          <h3>Directional gates</h3>
          {Object.entries(inference.gates).map(([key, gate]) => (
            <section className="portfolio-subsection" key={key}>
              <h4>
                {key} · {gate.passed === null ? "Unavailable" : gate.passed ? "Passed" : "Failed"}
              </h4>
              <dl className="record-list">
                <Record label="Estimate" value={value(gate.estimate)} />
                <Record label="Bound direction" value={gate.direction} />
                <Record label="Worst block bound" value={value(gate.bound)} />
                <Record
                  label="Required bound"
                  value={`${gate.direction === "LOWER" ? "≥" : "≤"} ${gate.threshold}`}
                />
                {Object.entries(gate.block_bounds).map(([block, bound]) => (
                  <Record key={block} label={`${block} month block bound`} value={value(bound)} />
                ))}
              </dl>
            </section>
          ))}
          <h3>Calibration diagnostics</h3>
          <p>
            Diagnostics describe evaluable frozen probabilities and do not fit a production model.
          </p>
          <dl className="record-list">
            <Record label="Log Loss" value={value(inference.diagnostics?.log_loss)} />
            <Record label="Brier Score" value={value(inference.diagnostics?.brier_score)} />
            <Record
              label="Calibration intercept"
              value={value(inference.diagnostics?.calibration_intercept)}
            />
            <Record
              label="Calibration slope"
              value={value(inference.diagnostics?.calibration_slope)}
            />
            <Record
              label="Recent diagnostic months"
              value={inference.recent_diagnostic_months.join(", ")}
            />
            <Record
              label="Recent diagnostic records"
              value={value(inference.recent_diagnostic_sample_count)}
            />
            <Record label="Recent Log Loss" value={value(inference.recent_diagnostics?.log_loss)} />
          </dl>
          {inference.diagnostics?.reliability_curve.map((bin) => (
            <dl className="record-list" key={String(bin.lower_probability)}>
              <Record
                label="Probability bin"
                value={`${bin.lower_probability} — ${bin.upper_probability}`}
              />
              <Record label="Records" value={value(bin.sample_count)} />
              <Record label="Mean prediction" value={value(bin.mean_predicted_probability)} />
              <Record label="Observed success rate" value={value(bin.observed_success_rate)} />
            </dl>
          ))}
          <details className="portfolio-subsection">
            <summary>Common calendar resampling</summary>
            {Object.entries(inference.resampling).map(([block, metadata]) => (
              <section key={block}>
                <h4>{block} month blocks</h4>
                <dl className="record-list">
                  {Object.entries(metadata).map(([key, item]) => (
                    <Record key={key} label={key} value={value(item)} />
                  ))}
                </dl>
              </section>
            ))}
          </details>
        </>
      )}
      <details className="portfolio-subsection">
        <summary>Complete monthly timeline ({evidence.months?.length ?? 0})</summary>
        {evidence.months?.map((month) => (
          <section key={month.plan_month}>
            <h3>
              {month.plan_month} · {month.disposition}
            </h3>
            <dl className="record-list">
              <Record label="Availability failure" value={value(month.availability_failure)} />
              <Record label="Selection-visible state" value={value(month.regime)} />
              <Record label="Maturity" value={value(month.matures_at)} />
              <Record label="Frozen probability records" value={value(month.members.length)} />
              <Record label="Source event" value={value(month.source_event_id)} />
            </dl>
            {month.reasons?.map((reason) => (
              <p key={reason}>{reason}</p>
            ))}
          </section>
        ))}
      </details>
    </section>
  );
}
