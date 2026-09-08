import type { FormalReport } from "./api/client";

type Liquidity = NonNullable<FormalReport["result"]["liquidity"]>;

const dispositionLabels: Record<Liquidity["disposition"], string> = {
  AVAILABLE: "Deployable cash available",
  ZERO_DEPLOYABLE_CASH: "Zero deployable cash",
  REMEDIATION_REQUIRED: "Liquidity restoration required",
  FUNDING_INFEASIBLE: "Funding infeasible",
  EVIDENCE_FAILED: "Liquidity evidence failed"
};

function Fact({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="record-row">
      <dt>{label}</dt>
      <dd>{value ?? "Unknown"}</dd>
    </div>
  );
}

export function LiquidityEvidence({ liquidity }: { liquidity: Liquidity }) {
  const policy =
    liquidity.protection_authorization?.usage?.authorization_snapshot.proposal.risk_budget;
  return (
    <section className="report-section liquidity-evidence" aria-label="Liquidity protection">
      <h2>Liquidity protection</h2>
      <p className="outcome-code">{dispositionLabels[liquidity.disposition]}</p>
      <p className="stage-reasons">{liquidity.reasons.join(", ")}</p>
      <dl className="record-list">
        <Fact label="Authorization" value={liquidity.authorization_id} />
        <Fact
          label="New exposure"
          value={liquidity.new_exposure_blocked ? "Blocked" : "Cash gate passed"}
        />
        <Fact label="Net liquidation equity" value={liquidity.net_liquidation_equity} />
        <Fact label="Qualified trading cash" value={liquidity.qualified_cash} />
        <Fact label="Reserved unfinished buys" value={liquidity.reserved_buy_cash} />
        <Fact label="Six-month cash obligations" value={liquidity.six_month_obligations} />
        <Fact label="Normal cash target" value={liquidity.normal_cash_target} />
        <Fact label="Hard cash floor" value={liquidity.hard_cash_floor} />
        <Fact label="Deployable purchase cash" value={liquidity.deployable_purchase_cash} />
        <Fact label="Maximum deadline-fundable cash" value={liquidity.maximum_fundable_cash} />
        <Fact label="Uncovered obligation gap" value={liquidity.uncovered_obligation_gap} />
        {policy && (
          <>
            <Fact label="Normal reserve ratio" value={policy.cash.target_ratio} />
            <Fact label="Hard reserve ratio" value={policy.cash.hard_ratio} />
            <Fact label="Budget effective" value={policy.effective_at} />
            <Fact label="Budget expires" value={policy.expires_at} />
          </>
        )}
      </dl>
      {liquidity.remediation_id && (
        <div className="portfolio-subsection">
          <h3>Retained restoration obligation</h3>
          <dl className="record-list">
            <Fact label="Original event" value={liquidity.remediation_id} />
            <Fact label="Current restoration shortfall" value={liquidity.remediation_shortfall} />
            <Fact
              label="Last confirmed shortfall"
              value={liquidity.retained_remediation_shortfall}
            />
          </dl>
        </div>
      )}
      {liquidity.obligation_funding.map((obligation) => (
        <div className="portfolio-subsection" key={obligation.obligation_id}>
          <h3>{obligation.obligation_id}</h3>
          <dl className="record-list">
            <Fact label="Target account" value={obligation.target_account_id} />
            <Fact label="Latest usable" value={obligation.latest_usable_at} />
            <Fact label="Required cash" value={obligation.required_cash} />
            <Fact label="Maximum covered cash" value={obligation.maximum_covered_cash} />
            <Fact label="Uncovered gap" value={obligation.uncovered_gap} />
          </dl>
        </div>
      ))}
      {liquidity.maximum_funding_plan.map((leg) => (
        <div className="portfolio-subsection" key={`${leg.account_id}:${leg.security_id}`}>
          <h3>Hypothetical disposal: {leg.security_id}</h3>
          <dl className="record-list">
            <Fact label="Source account" value={leg.account_id} />
            <Fact label="Legal quantity" value={leg.quantity} />
            <Fact label="Gross proceeds" value={leg.gross_proceeds} />
            <Fact label="Disposal costs" value={leg.disposal_cost} />
            <Fact label="Net proceeds" value={leg.net_proceeds} />
            <Fact label="Transferable at" value={leg.transferable_at} />
            <Fact label="Deadline funding contribution" value={leg.deadline_funding_contribution} />
            <Fact label="Terms evidence" value={leg.terms.evidence.source} />
          </dl>
        </div>
      ))}
      {liquidity.settled_coverage.map((receipt) => (
        <div className="portfolio-subsection" key={receipt.receipt_id}>
          <h3>Settled external payment</h3>
          <dl className="record-list">
            <Fact label="Receipt" value={receipt.receipt_id} />
            <Fact label="Obligation" value={receipt.obligation_id} />
            <Fact label="Paid amount" value={receipt.amount} />
          </dl>
        </div>
      ))}
    </section>
  );
}
