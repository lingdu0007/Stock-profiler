import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import correctionFixture from "../../tests/fixtures/synthetic/frozen_correction_evidence.json";
import portfolioFixture from "../../tests/fixtures/synthetic/portfolio_authorization.json";
import { App } from "./App";
import { syntheticReport as report } from "./test-support/synthetic-report";

describe("App", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    document.cookie = "__Host-stock_profiler_csrf=; Max-Age=0; Path=/; Secure";
    window.history.pushState({}, "", "/");
  });

  it("renders only the committed report projection returned by the generated API client", async () => {
    window.history.pushState({}, "", `/reports/${report.report_version_id}`);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText(report.result.outcome_code)).toBeVisible();
    expect(screen.getByText(report.result.summary)).toBeVisible();
    expect(screen.getByText(report.event_id)).toBeVisible();
    expect(screen.getByText(report.framework_run_id)).toBeVisible();
    expect(screen.getByText("D0 synthetic")).toBeVisible();
    expect(screen.getByRole("region", { name: "Decision stages" })).toHaveTextContent(
      "BUSINESS_DECISION"
    );
    expect(screen.getByRole("region", { name: "Decision stages" })).toHaveTextContent(
      "OUTPUT_CONTRACT"
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect((fetchMock.mock.calls[0]?.[0] as Request).url).toContain(
      `/api/v1/reports/${report.report_version_id}`
    );
  });

  it("renders the frozen portfolio authorization evidence from the committed report", async () => {
    const portfolioReport = {
      ...report,
      result: {
        ...report.result,
        portfolio: {
          disposition: "PREVIEWED",
          reasons: ["PORTFOLIO_SCOPE_PREVIEWED"],
          preview: {
            portfolio_id: portfolioFixture.proposal.portfolio_id,
            snapshot_id: portfolioFixture.proposal.snapshot.snapshot_id,
            included_accounts: [portfolioFixture.proposal.snapshot.accounts[0]],
            included_account_ids: ["synthetic-account-4017"],
            excluded_accounts: [
              {
                account_id: "synthetic-account-margin-2001",
                account_type: "SIMULATED_MARGIN",
                reason: "UNSUPPORTED_ACCOUNT_TYPE"
              }
            ],
            blocking_account_ids: [],
            blocking_accounts: []
          },
          authorization: null,
          usage: null
        }
      }
    };
    window.history.pushState({}, "", `/reports/${portfolioReport.report_version_id}`);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(portfolioReport), {
          status: 200,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        })
      )
    );

    render(<App />);

    const authorization = await screen.findByRole("region", {
      name: "Portfolio authorization"
    });
    expect(authorization).toHaveTextContent("PORTFOLIO_SCOPE_PREVIEWED");
    expect(authorization).toHaveTextContent("synthetic-account-4017");
    expect(authorization).toHaveTextContent("FULL_ACCOUNT");
    expect(authorization).toHaveTextContent("XSP");
    expect(authorization).toHaveTextContent("SIMULATED_STATISTICAL_ACTION");
    expect(authorization).toHaveTextContent("synthetic-account-margin-2001");
    expect(authorization).toHaveTextContent("UNSUPPORTED_ACCOUNT_TYPE");
  });

  it("renders saved authoritative position facts, source clocks, and conflicts", async () => {
    const positionEvidence = {
      source: "synthetic-broker-4017-position",
      source_version: "synthetic-broker-schema-v1",
      business_effective_at: "2042-05-17T15:00:00Z",
      source_observed_at: "2042-05-17T15:10:00Z",
      locally_acquired_at: "2042-05-17T15:14:00Z",
      validated_at: "2042-05-17T15:18:00Z",
      cutoff_at: "2042-05-17T16:00:00Z",
      complete_through_at: "2042-05-17T16:00:00Z",
      expires_at: null
    };
    const openingCashEvidence = {
      ...positionEvidence,
      source: "synthetic-broker-4017-opening-cash"
    };
    const positionReport = {
      ...report,
      result: {
        ...report.result,
        position: {
          disposition: "CONFLICTED",
          reasons: ["LEDGER_QUANTITY_MISMATCH", "FACT_EXPIRED"],
          snapshot: {
            snapshot_id: "synthetic-position-snapshot-alpha",
            cutoff_at: "2042-05-17T16:00:00Z",
            valuation_currency: "XSP",
            snapshot_source: "synthetic-position-snapshot-manifest",
            evidence_clock: {
              business_effective_at: "2042-05-17T15:00:00Z",
              source_observed_at: "2042-05-17T15:10:00Z",
              locally_acquired_at: "2042-05-17T15:14:00Z",
              validated_at: "2042-05-17T15:18:00Z"
            },
            snapshot_evidence: positionEvidence,
            total_account_equity: "1800",
            action_units: [
              {
                account_id: "synthetic-account-4017",
                account_type: "SIMULATED_CASH",
                currency: "XSP",
                position_id: "synthetic-position-4017-xqz",
                origin: "EXTERNAL",
                lifecycle_id: "synthetic-lifecycle-4017-xqz",
                issuer_id: "FICTIONAL-ORBITAL-MOSAIC",
                security_id: "XQZ-4017",
                total_quantity: "101",
                broker_sellable_quantity: null,
                unsettled_quantity: "10",
                frozen_quantity: "5",
                restricted_quantity: "0",
                open_sell_order_quantity: "15",
                exact_statistical_action_quantity: null,
                exact_quantity_status: "BLOCKED",
                reasons: ["LEDGER_QUANTITY_MISMATCH", "FACT_EXPIRED"],
                position_evidence: positionEvidence
              }
            ],
            issuer_exposures: [
              {
                issuer_id: "FICTIONAL-ORBITAL-MOSAIC",
                valuation_currency: "XSP",
                account_ids: ["synthetic-account-4017", "synthetic-account-8029"],
                current_market_exposure: "1800"
              }
            ],
            cash_states: [
              {
                account_id: "synthetic-account-4017",
                account_type: "SIMULATED_CASH",
                currency: "XSP",
                account_evidence: positionEvidence,
                account_equity: "1000",
                account_equity_evidence: positionEvidence,
                ledger_cash_semantics: "OPENING_BALANCE_PLUS_AUTHORITATIVE_LEDGER",
                opening_ledger_cash: "902",
                opening_ledger_cash_evidence: openingCashEvidence,
                ledger_cash: "100",
                trading_cash: "95",
                transferable_cash: "90",
                frozen_cash: "5",
                receivable_cash: "10",
                payable_cash: "0",
                cash_state_evidence: positionEvidence
              }
            ],
            unfinished_orders: [
              {
                account_id: "synthetic-account-4017",
                order_id: "synthetic-open-sell-4017",
                security_id: "XQZ-4017",
                side: "SELL",
                remaining_quantity: "15",
                evidence: {
                  source: "synthetic-broker-4017-order",
                  business_effective_at: "2042-05-17T15:00:00Z",
                  source_observed_at: "2042-05-17T15:10:00Z",
                  locally_acquired_at: "2042-05-17T15:14:00Z",
                  validated_at: "2042-05-17T15:18:00Z",
                  cutoff_at: "2042-05-17T16:00:00Z",
                  expires_at: null
                }
              }
            ],
            execution_restrictions: [
              {
                account_id: "synthetic-account-4017",
                restriction_id: "synthetic-account-wide-sell-block",
                security_id: null,
                kind: "BROKER_SELL_BLOCK",
                reason: "Synthetic account-wide broker restriction.",
                active: true,
                evidence: {
                  source: "synthetic-broker-4017-restriction",
                  business_effective_at: "2042-05-17T15:00:00Z",
                  source_observed_at: "2042-05-17T15:10:00Z",
                  locally_acquired_at: "2042-05-17T15:14:00Z",
                  validated_at: "2042-05-17T15:18:00Z",
                  cutoff_at: "2042-05-17T16:00:00Z",
                  expires_at: null
                }
              }
            ],
            authoritative_ledger: [
              {
                account_id: "synthetic-account-4017",
                entry_id: "synthetic-fill-4017-xqz",
                entry_type: "FILL",
                security_id: "XQZ-4017",
                quantity_delta: "100",
                cost_basis_delta: "890",
                cash_delta: "-890",
                occurred_at: "2042-05-16T15:00:00Z",
                corrects_entry_id: "synthetic-prior-ledger-entry",
                correction_reason: "Synthetic broker fee correction.",
                evidence: {
                  source: "synthetic-broker-4017-fill",
                  business_effective_at: "2042-05-17T15:00:00Z",
                  source_observed_at: "2042-05-17T15:10:00Z",
                  locally_acquired_at: "2042-05-17T15:14:00Z",
                  validated_at: "2042-05-17T15:18:00Z",
                  cutoff_at: "2042-05-17T16:00:00Z",
                  expires_at: null
                }
              }
            ],
            user_annotations: [
              {
                annotation_id: "synthetic-user-position-note-4017",
                account_id: "synthetic-account-4017",
                security_id: "XQZ-4017",
                note: "Synthetic user note retains a different claimed quantity.",
                claimed_total_quantity: "999",
                claimed_cost_basis: null,
                created_at: "2042-05-17T15:30:00Z"
              }
            ]
          },
          conflicts: [
            {
              conflict_id:
                "position-conflict:synthetic-account-4017:XQZ-4017:LEDGER_QUANTITY_MISMATCH",
              code: "LEDGER_QUANTITY_MISMATCH",
              affected_scope: {
                account_id: "synthetic-account-4017",
                security_id: "XQZ-4017",
                fields: ["positions.total_quantity", "ledger_entries.quantity_delta"]
              },
              blocks_exact_statistical_quantity: true
            }
          ]
        }
      }
    };
    window.history.pushState({}, "", `/reports/${positionReport.report_version_id}`);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(positionReport), {
          status: 200,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        })
      )
    );

    render(<App />);

    const position = await screen.findByRole("region", { name: "Position state snapshot" });
    expect(position).toHaveTextContent("synthetic-position-snapshot-manifest");
    expect(position).toHaveTextContent("Business effective");
    expect(position).toHaveTextContent("2042-05-17T15:00:00Z");
    expect(position).toHaveTextContent("Source observed");
    expect(position).toHaveTextContent("Snapshot evidence");
    expect(position).toHaveTextContent("synthetic-broker-schema-v1");
    expect(position).toHaveTextContent("complete through");
    expect(position).toHaveTextContent("Locally acquired");
    expect(position).toHaveTextContent("Validated");
    expect(position).toHaveTextContent("BLOCKED");
    expect(position).toHaveTextContent("LEDGER_QUANTITY_MISMATCH");
    expect(position).toHaveTextContent("synthetic-broker-4017-fill");
    expect(position).toHaveTextContent("synthetic-open-sell-4017");
    expect(position).toHaveTextContent("synthetic-account-wide-sell-block");
    expect(position).toHaveTextContent("Synthetic user note retains a different claimed quantity.");
    expect(position).toHaveTextContent("opening ledger evidence");
    expect(position).toHaveTextContent("synthetic-broker-4017-opening-cash");
    expect(position).toHaveTextContent("corrects synthetic-prior-ledger-entry");
    expect(position).toHaveTextContent("correction reason Synthetic broker fee correction.");
  });

  it("renders the confirmation and budget snapshot retained by a portfolio use", async () => {
    const proposal = {
      ...portfolioFixture.proposal,
      activation_snapshot: {
        ...portfolioFixture.proposal.snapshot,
        snapshot_id: "synthetic-portfolio-snapshot-alpha-activation",
        cutoff_at: "2042-05-18T16:00:00Z",
        accounts: portfolioFixture.proposal.snapshot.accounts.map((account) => ({
          ...account,
          captured_at: "2042-05-18T16:00:00Z"
        }))
      },
      risk_budget: {
        ...portfolioFixture.proposal.risk_budget,
        effective_at: "2042-05-18T16:00:00Z",
        expires_at: "2042-11-18T16:00:00Z"
      }
    };
    const authorizationSnapshot = {
      authorization_id: "decision-event-portfolio-alpha",
      previous_authorization_id: null,
      recorded_at: "2042-05-17T16:01:00Z",
      confirmation: {
        confirmation_id: "synthetic-risk-confirmation-alpha",
        user_id: "stock-profiler-single-user",
        portfolio_id: "synthetic-decision-portfolio-alpha",
        snapshot_id: "synthetic-portfolio-snapshot-alpha",
        risk_budget_version_id: "synthetic-risk-budget-alpha",
        confirmed_at: "2042-05-17T16:00:00Z",
        confirmed: true,
        relaxation_evidence: portfolioFixture.risk_relaxation_evidence,
        downside_grid_requalification: {
          evidence_id: "synthetic-grid-requalification-alpha",
          predecessor_authorization_id: "decision-event-portfolio-alpha",
          predecessor_risk_budget_version_id: "synthetic-risk-budget-alpha",
          action_policy_version_id: "synthetic-action-policy-grid-beta",
          historical_out_of_sample_evidence_id: "synthetic-grid-history-grid-beta",
          historical_completed_at: "2042-06-10T15:00:00Z",
          locked_forward_confirmation_id: "synthetic-grid-forward-grid-beta",
          locked_forward_confirmed_at: "2042-06-16T14:59:00Z",
          available_at: "2042-06-16T15:00:00Z"
        }
      },
      proposal
    };
    const usageReport = {
      ...report,
      result: {
        ...report.result,
        portfolio: {
          disposition: "DENIED",
          reasons: ["RISK_BUDGET_EXPIRED"],
          preview: null,
          authorization: null,
          usage: {
            requested_action: "NEW_EXPOSURE",
            allowed: false,
            authorization_snapshot: authorizationSnapshot,
            retained_protection_floor: {
              new_exposure_blocked: true,
              retained_directions: ["REDUCE", "EXIT"]
            },
            unfinished_cash_obligations: authorizationSnapshot.proposal.cash_obligations,
            reasons: ["RISK_BUDGET_EXPIRED"],
            checked_at: "2042-11-17T16:01:00Z"
          }
        }
      }
    };
    window.history.pushState({}, "", `/reports/${usageReport.report_version_id}`);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(usageReport), {
          status: 200,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        })
      )
    );

    render(<App />);

    const confirmation = await screen.findByRole("region", { name: "Portfolio confirmation" });
    expect(confirmation).toHaveTextContent("synthetic-risk-confirmation-alpha");
    expect(confirmation).toHaveTextContent("synthetic-risk-budget-alpha");
    expect(confirmation).toHaveTextContent("2042-11-18T16:00:00Z");
    expect(confirmation).toHaveTextContent("synthetic-cash-obligation-alpha");
    expect(confirmation).toHaveTextContent("SIMULATED_STATISTICAL_ACTION");
    expect(confirmation).toHaveTextContent("synthetic-risk-relaxation-evidence-alpha");
    expect(confirmation).toHaveTextContent("Normal market sessions");
    expect(confirmation).toHaveTextContent("20");
    expect(confirmation).toHaveTextContent("synthetic-portfolio-snapshot-alpha-activation");
    expect(confirmation).toHaveTextContent("Activation cutoff");
    expect(confirmation).toHaveTextContent("synthetic-market-calendar-v1");
    expect(confirmation).toHaveTextContent("Action policy");
    expect(confirmation).toHaveTextContent("synthetic-action-policy-alpha");
    expect(confirmation).toHaveTextContent("synthetic-grid-requalification-alpha");
    expect(confirmation).toHaveTextContent("Historical out-of-sample evidence");
    expect(confirmation).toHaveTextContent("Locked forward confirmation");
  });

  it("shows correction lineage supplied by the committed report projection", async () => {
    const correctedReport = {
      ...report,
      corrects_event_id: "decision-event-original-4017",
      result: { ...report.result, correction_evidence: correctionFixture.correction }
    };
    window.history.pushState({}, "", `/reports/${correctedReport.report_version_id}`);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(correctedReport), {
          status: 200,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        })
      )
    );

    render(<App />);

    expect(await screen.findByText("decision-event-original-4017")).toBeVisible();
    expect(screen.getByText("Corrects event")).toBeVisible();
    const correction = screen.getByRole("region", { name: "Correction evidence" });
    expect(correction).toHaveTextContent(correctionFixture.correction.corrected_statement);
    expect(correction).toHaveTextContent("2042-05-17T16:01:50Z");
    expect(correction).toHaveTextContent("2042-05-17T16:01:10Z");
  });

  it("refreshes a CSRF-protected session before reading the committed report", async () => {
    window.history.pushState({}, "", `/reports/${report.report_version_id}`);
    document.cookie = "__Host-stock_profiler_csrf=synthetic-csrf-token; Path=/; Secure";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "authenticated" }), {
          status: 200,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(report), {
          status: 200,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText(report.result.outcome_code)).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect((fetchMock.mock.calls[0]?.[0] as Request).url).toContain("/api/v1/auth/session/refresh");
    expect((fetchMock.mock.calls[1]?.[0] as Request).url).toContain(
      `/api/v1/reports/${report.report_version_id}`
    );
  });

  it("redirects an unauthenticated report request to the passkey sign-in route", async () => {
    window.history.pushState({}, "", `/reports/${report.report_version_id}`);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));

    render(<App />);

    expect(await screen.findByRole("button", { name: "Continue with Passkey" })).toBeVisible();
    expect(screen.queryByText(report.result.outcome_code)).not.toBeInTheDocument();
  });

  it("does not render a closed report when the delivery API returns not found", async () => {
    window.history.pushState({}, "", `/reports/${report.report_version_id}`);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));

    render(<App />);

    expect(await screen.findByText("Report unavailable")).toBeVisible();
    expect(screen.queryByText(report.result.outcome_code)).not.toBeInTheDocument();
  });

  it("does not expose a public enrollment action without a host-console grant fragment", async () => {
    window.history.pushState({}, "", "/enroll");

    render(<App />);

    expect(await screen.findByText("Enrollment authorization is unavailable.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Enroll Passkey" })).not.toBeInTheDocument();
  });
});
