import type { FormalReport } from "./api/client";
import { ReportRecord as Record } from "./ReportRecord";

type Execution = NonNullable<FormalReport["result"]["candidate_execution"]>;

export function CandidateExecutionEvidence({ execution }: { execution: Execution }) {
  return (
    <section className="report-section" aria-label="Candidate execution">
      <h2>Candidate execution</h2>
      <p className="outcome-code">{execution.disposition}</p>
      <p className="stage-reasons">{execution.reasons.join(", ")}</p>
      <p>{execution.terminal_outcome ?? "Plan remains open"}</p>
      <dl className="record-list">
        <Record label="Plan" value={execution.plan_id ?? "Unavailable"} />
        <Record label="Confirmation" value={execution.confirmation_id ?? "Unavailable"} />
        <Record
          label="Position evidence"
          value={execution.position_event_id ?? "Awaiting reconciliation"}
        />
        <Record
          label="Released reservations"
          value={(execution.released_reservation_ids ?? []).join(", ") || "None"}
        />
      </dl>
      {(execution.declarations ?? []).length > 0 && (
        <div className="portfolio-subsection">
          <h3>User declarations awaiting reconciliation</h3>
          <ul>
            {(execution.declarations ?? []).map((row, index) => (
              <li key={index}>
                {row.security_id}: {row.status} at {row.declared_at}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(execution.rows ?? []).map((row) => (
        <div className="portfolio-subsection" key={row.reservation_id}>
          <h3>{row.security_id}</h3>
          <p className="outcome-code">{row.state}</p>
          <dl className="record-list">
            <Record label="Accepted quantity" value={String(row.accepted_quantity)} />
            <Record
              label="Broker filled quantity within intent"
              value={String(row.filled_quantity)}
            />
            <Record label="Unfilled quantity" value={String(row.remaining_quantity)} />
          </dl>
        </div>
      ))}
      {(execution.fills ?? []).map((fill) => (
        <dl className="record-list" key={`${fill.account_id}:${fill.entry_id}`}>
          <Record label="Broker fill" value={fill.entry_id} />
          <Record label="Execution classification" value={fill.classification} />
          <Record label="Account / security" value={`${fill.account_id} / ${fill.security_id}`} />
          <Record label="Broker quantity" value={String(fill.quantity)} />
          <Record
            label="Quantity within accepted intent"
            value={String(fill.intent_quantity ?? 0)}
          />
          <Record label="External quantity" value={String(fill.external_quantity ?? 0)} />
          <Record label="Cash used including fees" value={String(fill.cash_used)} />
          <Record label="Broker fees" value={String(fill.fees)} />
          <Record label="Corrects broker entry" value={fill.corrects_entry_id ?? "None"} />
          <Record label="Attribution reasons" value={fill.reasons.join(", ") || "None"} />
        </dl>
      ))}
      {(execution.order_attributions ?? []).map((order) => (
        <dl className="record-list" key={`${order.account_id}:${order.order_id}`}>
          <Record
            label="Broker account / order"
            value={`${order.account_id} / ${order.order_id}`}
          />
          <Record label="Order classification" value={order.classification} />
          <Record label="Linked intent" value={order.reservation_id ?? "Unassociated"} />
          <Record label="Order attribution reasons" value={order.reasons.join(", ") || "None"} />
        </dl>
      ))}
      {[...(execution.reservations ?? []), ...(execution.unassociated_commitments ?? [])].map(
        (claim) => (
          <dl className="record-list" key={claim.commitment_id}>
            <Record label="Retained capacity" value={claim.commitment_id} />
            <Record
              label="Account / security"
              value={`${claim.account_id} / ${claim.security_id}`}
            />
            <Record label="Committed principal" value={String(claim.principal)} />
            <Record
              label="Linked broker orders"
              value={
                (claim.broker_order_bindings ?? [])
                  .map(([account, order]) => `${account} / ${order}`)
                  .join(", ") ||
                (claim.broker_order_id
                  ? `${claim.account_id} / ${claim.broker_order_id}`
                  : (claim.broker_order_ids ?? [])
                      .map((order) => `${claim.account_id} / ${order}`)
                      .join(", ")) ||
                "None"
              }
            />
            <Record label="Purchase cost reserve" value={String(claim.purchase_cost)} />
            <Record
              label="Cash awaiting reconciliation"
              value={String(claim.reconciliation_cash_hold ?? 0)}
            />
            <Record label="Disposal friction" value={String(claim.disposal_friction)} />
          </dl>
        )
      )}
    </section>
  );
}
