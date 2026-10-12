import { NavLink } from "react-router";
import type { FormalReport } from "./api/client";
import { ReportRecord as Record } from "./ReportRecord";

type Evidence = NonNullable<FormalReport["result"]["exit_evaluation"]>;

export function ExitEvaluationEvidence({ evidence }: { evidence: Evidence }) {
  return (
    <section className="report-section" aria-label="Standard exit evidence">
      <h2>Standard exit evidence</h2>
      <p>Historical evidence does not authorize position actions.</p>
      <p>Standard exposure is independent of personal trades. Sold exposure stays sold.</p>
      <dl className="record-list">
        <Record label="Evaluation" value={evidence.disposition} />
        <Record label="Saved version" value={String(evidence.report_version)} />
        <Record label="Cutoff" value={evidence.cutoff_at} />
        <Record label="Registered policy" value={evidence.policy.version_id} />
      </dl>
      {evidence.previous_report_id && (
        <NavLink to={`/reports/${evidence.previous_report_id}`}>
          Read previous exit evaluation
        </NavLink>
      )}
      {evidence.forecast_freezes.map((item) => (
        <dl className="record-list" key={`${item.lifecycle_id}:${item.forecast.prediction_id}`}>
          <Record label="Frozen lifecycle" value={item.lifecycle_id} />
          <Record label="Frozen prediction" value={item.forecast.prediction_id} />
          <Record label="Prediction cutoff" value={item.forecast.cutoff_at} />
        </dl>
      ))}
      {evidence.counts && (
        <dl className="record-list">
          <Record label="Standard positions" value={String(evidence.counts.registered)} />
          <Record label="Due positions" value={String(evidence.counts.due)} />
          <Record label="Evaluable positions" value={String(evidence.counts.evaluable)} />
          <Record label="Due paths missing" value={String(evidence.counts.due_missing)} />
          <Record label="Immature positions" value={String(evidence.counts.immature)} />
        </dl>
      )}
      {evidence.inference && (
        <>
          {Object.entries(evidence.inference.watermarks).map(([scope, mark]) => (
            <section className="portfolio-subsection" key={scope}>
              <h3>
                {scope} · {mark.disposition}
              </h3>
              <dl className="record-list">
                <Record label="Mature entry months" value={String(mark.mature_months)} />
                <Record label="Independent positions" value={String(mark.position_count)} />
                <Record
                  label="Nonoverlapping windows"
                  value={String(mark.nonoverlapping_windows)}
                />
                <Record label="Separated state episodes" value={String(mark.periods)} />
                {Object.entries(mark.directions).map(([direction, count]) => (
                  <Record
                    key={direction}
                    label={`${direction} independent positions`}
                    value={String(count)}
                  />
                ))}
              </dl>
            </section>
          ))}
          <details>
            <summary>Historical gates</summary>
            <table>
              <thead>
                <tr>
                  <th>Gate</th>
                  <th>Estimate</th>
                  <th>Confidence bound</th>
                  <th>Required bound</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(evidence.inference.gates).map(([name, gate]) => (
                  <tr key={name}>
                    <td>{name}</td>
                    <td>{gate.estimate ?? "Unavailable"}</td>
                    <td>{gate.bound ?? "Unavailable"}</td>
                    <td>
                      {gate.direction} {gate.strict ? "strict" : "inclusive"} {gate.threshold}
                    </td>
                    <td>
                      {!gate.applicable
                        ? "No applicable sample"
                        : gate.passed === null
                          ? "Unresolved"
                          : gate.passed
                            ? "Passed"
                            : "Failed"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          {evidence.inference.missing_evidence.length > 0 && (
            <details open>
              <summary>Missing due evidence</summary>
              <ul>
                {evidence.inference.missing_evidence.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </details>
          )}
          <details>
            <summary>Calibration diagnostics and resampling</summary>
            {Object.entries(evidence.inference.diagnostics).map(([target, diagnostic]) => (
              <dl className="record-list" key={target}>
                <Record label={`${target} Brier score`} value={String(diagnostic.brier_score)} />
                <Record label={`${target} Log Loss`} value={String(diagnostic.log_loss)} />
                <Record
                  label={`${target} quantile loss`}
                  value={String(evidence.inference?.pinball_loss[target] ?? "Unavailable")}
                />
              </dl>
            ))}
            {Object.entries(evidence.inference.resampling).map(([block, sample]) => (
              <dl className="record-list" key={block}>
                <Record label={`${block} month blocks`} value={String(sample.calendar_months)} />
                <Record label="Repetitions" value={String(sample.repetitions)} />
                <Record label="Sampling identity" value={String(sample.sampling_digest)} />
              </dl>
            ))}
          </details>
        </>
      )}
      {evidence.positions.map((position) => (
        <section className="portfolio-subsection" key={position.lifecycle_id}>
          <h3>
            {position.security_id} · {position.state}
          </h3>
          <dl className="record-list">
            <Record label="Standard lifecycle" value={position.lifecycle_id} />
            <Record
              label="Remaining initial exposure"
              value={`${Number(position.current_exposure) * 100}%`}
            />
            <Record label="First lifecycle ends" value={position.matures_at} />
          </dl>
          <p>
            Residual exposure belongs to this lifecycle. Any renewal requires a separate evaluation.
          </p>
          {Object.entries(position.policies).map(([name, path]) => (
            <details key={name}>
              <summary>{name} policy path</summary>
              <dl className="record-list">
                <Record
                  label="Six month net return"
                  value={String(path.net_total_return ?? "Unavailable")}
                />
                <Record label="Maximum drawdown" value={String(path.maximum_drawdown)} />
                <Record label="Exit costs" value={String(path.exit_cost)} />
                <Record label="Pending exposure target" value={String(path.pending_target)} />
              </dl>
              <ul>
                {path.trades.map((trade) => (
                  <li key={trade.executed_at}>
                    {trade.executed_at}: sold {Number(trade.exposure_sold) * 100}% of initial
                    exposure at {trade.price}; net proceeds {trade.net_proceeds}
                  </li>
                ))}
              </ul>
            </details>
          ))}
        </section>
      ))}
    </section>
  );
}
