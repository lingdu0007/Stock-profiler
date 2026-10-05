import { NavLink } from "react-router";
import type { FormalReport } from "./api/client";
import { ReportRecord as Record } from "./ReportRecord";

type Evidence = NonNullable<FormalReport["result"]["historical_selection"]>;
const value = (item: unknown) =>
  item === null || item === undefined ? "Unavailable" : String(item);

export function HistoricalSelectionEvidence({ evidence }: { evidence: Evidence }) {
  const inference = evidence.inference;
  return (
    <section className="report-section" aria-label="Historical selection evidence">
      <h2>Historical selection evidence</h2>
      <p>{evidence.disposition}</p>
      <p>D0 synthetic evidence. No recommendation or execution authorization.</p>
      <dl className="record-list">
        <Record label="Registered contract" value={evidence.registration.version_id} />
        <Record label="Saved report version" value={value(evidence.report_version)} />
        <Record label="Evaluation cutoff" value={evidence.cutoff_at} />
        <Record
          label="Registered monthly window"
          value={`${evidence.registration.start_month} — ${evidence.registration.end_month}`}
        />
        <Record
          label="Exploration maturity floor"
          value={value(evidence.registration.exploratory_months)}
        />
        <Record label="Formal maturity floor" value={value(evidence.registration.formal_months)} />
        <Record
          label="Required positive members"
          value={value(evidence.registration.positive_members_required)}
        />
        <Record
          label="Required terminal target members"
          value={value(evidence.registration.target_members_required)}
        />
        <Record
          label="Terminal return target"
          value={value(evidence.registration.terminal_target)}
        />
        <Record
          label="Maximum drawdown limit"
          value={value(evidence.registration.maximum_drawdown)}
        />
        <Record
          label="Random trials per month"
          value={value(evidence.registration.random_trials)}
        />
        <Record label="One-sided confidence" value={value(evidence.registration.confidence)} />
        {Object.entries(evidence.counts ?? {}).map(([key, count]) => (
          <Record key={key} label={key} value={value(count)} />
        ))}
      </dl>
      {evidence.previous_report_id && (
        <NavLink to={`/reports/${evidence.previous_report_id}`}>
          Read previous historical report
        </NavLink>
      )}
      {inference && (
        <>
          <section className="portfolio-subsection" aria-label="Historical conjunctive gates">
            <h3>All gates must pass</h3>
            {Object.entries(inference.gates).map(([key, gate]) => (
              <section className="portfolio-subsection" key={key}>
                <h4>
                  {key} · {gate.passed === null ? "Unavailable" : gate.passed ? "Passed" : "Failed"}
                </h4>
                <dl className="record-list">
                  <Record label="Estimate" value={value(gate.estimate)} />
                  <Record label="Worst block lower bound" value={value(gate.lower_bound)} />
                  <Record
                    label="Required bound"
                    value={`${gate.strict ? ">" : "≥"} ${gate.threshold}`}
                  />
                  {Object.entries(gate.block_bounds).map(([block, bound]) => (
                    <Record
                      key={block}
                      label={`${block} month block lower bound`}
                      value={value(bound)}
                    />
                  ))}
                  {Object.entries(gate.undefined_resamples).map(([block, count]) => (
                    <Record
                      key={block}
                      label={`${block} month block undefined samples`}
                      value={value(count)}
                    />
                  ))}
                </dl>
              </section>
            ))}
          </section>
          <section className="portfolio-subsection" aria-label="Market state sufficiency">
            <h3>Selection-visible market states</h3>
            {Object.entries(inference.regimes).map(([state, watermark]) => (
              <section className="portfolio-subsection" key={state}>
                <h4>
                  {state} · {watermark.sufficient ? "Sufficient" : "Insufficient"}
                </h4>
                <dl className="record-list">
                  {Object.entries(watermark).map(([key, item]) => (
                    <Record key={key} label={key} value={value(item)} />
                  ))}
                </dl>
              </section>
            ))}
          </section>
          <section className="portfolio-subsection" aria-label="Dependence and stress diagnostics">
            <h3>Dependence and stress diagnostics</h3>
            <p>Diagnostics do not replace conjunctive gates.</p>
            <dl className="record-list">
              {Object.entries(inference.diagnostics).map(([key, item]) => (
                <Record key={key} label={key} value={value(item)} />
              ))}
            </dl>
          </section>
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
      <details className="portfolio-subsection" open>
        <summary>Complete monthly timeline ({evidence.months?.length ?? 0})</summary>
        {evidence.months?.map((month) => (
          <section className="portfolio-subsection" key={month.plan_month}>
            <h3>
              {month.plan_month} · {month.disposition}
            </h3>
            <dl className="record-list">
              <Record label="Availability failure" value={value(month.availability_failure)} />
              <Record label="Selection-visible state" value={value(month.regime)} />
              <Record label="Maturity" value={value(month.matures_at)} />
              <Record label="Batch passed" value={value(month.batch_pass)} />
              <Record label="Drawdown passed" value={value(month.drawdown_pass)} />
              <Record label="Maximum drawdown" value={value(month.maximum_drawdown)} />
              <Record label="Selected members" value={month.members?.join(", ") ?? "None"} />
            </dl>
            {month.reasons?.map((reason) => (
              <p key={reason}>{reason}</p>
            ))}
            {Object.entries(month.baselines ?? {}).map(([baseline, metrics]) => (
              <section key={baseline}>
                <h4>{baseline}</h4>
                <dl className="record-list">
                  <Record label="Positive member rate" value={value(metrics.positive_rate)} />
                  <Record label="Target member rate" value={value(metrics.target_rate)} />
                  <Record label="Positive member count" value={value(metrics.counts?.positive)} />
                  <Record label="Target member count" value={value(metrics.counts?.target)} />
                  <Record
                    label="Member slot denominator"
                    value={value(metrics.counts?.member_slots)}
                  />
                  <Record label="Batch pass rate" value={value(metrics.batch_pass_rate)} />
                  <Record label="Passed trials" value={value(metrics.counts?.passed_trials)} />
                  <Record label="Trial count" value={value(metrics.trial_count)} />
                  <Record label="Failed trials" value={value(metrics.failed_trials)} />
                  <Record label="Membership digest" value={metrics.membership_digest} />
                </dl>
              </section>
            ))}
          </section>
        ))}
      </details>
    </section>
  );
}
