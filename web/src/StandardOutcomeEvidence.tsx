import { NavLink } from "react-router";
import type { FormalReport } from "./api/client";
import { ReportRecord as Record } from "./ReportRecord";

type Outcome = NonNullable<FormalReport["result"]["standard_outcomes"]>;

export function StandardOutcomeEvidence({ outcome }: { outcome: Outcome }) {
  return (
    <section className="report-section" aria-label="Standard outcomes">
      <h2>Standard outcomes</h2>
      <p>Standard results are independent of personal purchases and costs.</p>
      <p>
        Success requires at least 20% net total return at the six calendar month terminal close.
      </p>
      <p>Complete outcome evidence does not grant statistical qualification or activation.</p>
      <dl className="record-list">
        <Record label="Saved evaluation version" value={String(outcome.report_version)} />
        <Record label="Evaluation cutoff" value={outcome.cutoff_at} />
      </dl>
      {outcome.previous_report_id && (
        <NavLink to={`/reports/${outcome.previous_report_id}`}>
          Read previous evaluation report
        </NavLink>
      )}
      {Object.entries(outcome.populations).map(([population, counts]) => (
        <section
          className="portfolio-subsection"
          key={population}
          aria-label={`${population} outcomes`}
        >
          <h3>{population}</h3>
          <p>{counts.formal_adjudication}</p>
          <dl className="record-list">
            <Record label="Registered" value={String(counts.registered)} />
            <Record label="Due" value={String(counts.due)} />
            <Record label="Evaluable" value={String(counts.evaluable)} />
            <Record label="Due outcomes missing" value={String(counts.missing)} />
            <Record label="Immature" value={String(counts.immature)} />
            <Record label="Achieved" value={String(counts.achieved)} />
            <Record label="Not achieved" value={String(counts.not_achieved)} />
          </dl>
        </section>
      ))}
      <details className="portfolio-subsection" open>
        <summary>Retained standard evaluation members ({outcome.members.length})</summary>
        {outcome.members.map((member) => (
          <section className="portfolio-subsection" key={member.evaluation_id}>
            <h3>
              {member.security_id} · {member.population}
            </h3>
            <p>{member.state}</p>
            <dl className="record-list">
              <Record label="Evaluation identity" value={member.evaluation_id} />
              <Record label="Source event" value={member.source_event_id} />
              <Record
                label="Frozen probability"
                value={String(member.frozen_probability ?? "Unavailable")}
              />
              <Record label="Standard entry" value={member.entry_at ?? "Unavailable"} />
              <Record
                label="Entry price including costs"
                value={String(member.entry_price ?? "Unavailable")}
              />
              <Record label="Terminal evaluation" value={member.matures_at} />
              <Record label="Entry expired" value={member.entry_expired ? "Yes" : "No"} />
              <Record
                label="Terminal net total return"
                value={
                  member.net_total_return === null || member.net_total_return === undefined
                    ? "Unavailable"
                    : `${(Number(member.net_total_return) * 100).toFixed(2)}% (ratio ${member.net_total_return})`
                }
              />
              <Record
                label="Entry evidence"
                value={member.observation?.entry?.evidence_id ?? "Unavailable"}
              />
              <Record
                label="Terminal evidence"
                value={member.observation?.terminal?.evidence_id ?? "Unavailable"}
              />
              <Record
                label="Corrects terminal evidence"
                value={member.observation?.terminal?.corrects_evidence_id ?? "None"}
              />
            </dl>
          </section>
        ))}
      </details>
      <section className="portfolio-subsection" aria-label="Candidate batch delivery history">
        <h3>Candidate batch delivery history</h3>
        {outcome.delivery.map((batch) => (
          <section className="portfolio-subsection" key={batch.event_id}>
            <h4>
              {batch.plan_month} · {batch.disposition}
            </h4>
            <dl className="record-list">
              <Record label="Generated" value={batch.generated_at} />
              <Record label="Committed" value={batch.committed_at} />
              <Record label="Published" value={batch.published_at ?? "Unpublished"} />
              <Record label="Window start" value={batch.valid_from ?? "Unavailable"} />
              <Record label="Window end" value={batch.valid_through ?? "Unavailable"} />
              <Record
                label="Expiry at evaluation cutoff"
                value={
                  batch.expired === null
                    ? "Unavailable"
                    : batch.expired
                      ? "Expired"
                      : "Within window"
                }
              />
              <Record label="Corrects event" value={batch.corrects_event_id ?? "None"} />
              <Record label="Superseded by" value={batch.superseded_by_event_id ?? "None"} />
            </dl>
            {batch.report_version_id && (
              <NavLink to={`/reports/${batch.report_version_id}`}>
                Read saved candidate report
              </NavLink>
            )}
            <ul>
              {batch.reminders.map((reminder) => (
                <li key={reminder.attempt_id}>
                  {reminder.status} · {reminder.recorded_at}
                </li>
              ))}
            </ul>
          </section>
        ))}
      </section>
    </section>
  );
}
