import type { FormalReport } from "./api/client";
import { ReportRecord } from "./ReportRecord";

type Confirmation = NonNullable<FormalReport["result"]["candidate_confirmation"]>;
export function CandidateConfirmationEvidence({ confirmation }: { confirmation: Confirmation }) {
  return (
    <section className="report-section" aria-label="Saved choice and reservation">
      <h3>Saved choice and reservation</h3>
      <p>
        {confirmation.disposition}: {confirmation.reasons.join(", ")}
      </p>
      <dl className="record-list">
        <ReportRecord
          label="Confirmation identity"
          value={confirmation.confirmation_id ?? "No confirmation formed"}
        />
        <ReportRecord
          label="Superseded confirmation"
          value={confirmation.supersedes_confirmation_id ?? "None"}
        />
        <ReportRecord
          label="Causally released reservations"
          value={confirmation.released_reservation_ids?.join(", ") || "None"}
        />
      </dl>
      <ul>
        {confirmation.choices?.map((row) => (
          <li key={row.security_id}>
            {row.security_id}: {row.choice}
          </li>
        ))}
      </ul>
      {confirmation.reservations?.map((row) => (
        <dl className="record-list" key={row.commitment_id}>
          <ReportRecord label="Reservation" value={row.commitment_id} />
          <ReportRecord
            label="Account and security"
            value={`${row.account_id} / ${row.security_id}`}
          />
          <ReportRecord
            label="Reserved principal / costs"
            value={`${row.principal} / ${row.purchase_cost}`}
          />
          <ReportRecord
            label="Reserved quantity / price cap"
            value={`${row.quantity ?? "Unknown"} / ${row.price_cap ?? "Unknown"}`}
          />
        </dl>
      ))}
    </section>
  );
}
