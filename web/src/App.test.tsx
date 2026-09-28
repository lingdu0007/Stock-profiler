import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import correctionFixture from "../../tests/fixtures/synthetic/frozen_correction_evidence.json";
import portfolioFixture from "../../tests/fixtures/synthetic/portfolio_authorization.json";
import { App } from "./App";
import type { FormalReport } from "./api/client";
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

  it("renders the frozen candidate batch and per-security evidence without personal allocation", async () => {
    const candidateReport: FormalReport = {
      ...report,
      result: {
        ...report.result,
        outcome_code: "CANDIDATE_RELEASE_CANDIDATES",
        candidate_release: {
          batch_id: "synthetic-candidate-batch-001",
          disposition: "CANDIDATES",
          knowledge_cutoff: "2046-07-01T08:00:00Z",
          published_at: "2046-07-01T09:00:00Z",
          qualification_scope: "D0_SYNTHETIC_CONTRACT_ONLY",
          capability_version: "candidate-v1",
          research_object_id: "synthetic-research-object-001",
          research_event_id: "synthetic-research-event-001",
          market_calendar_version: "synthetic-calendar-v1",
          market_state: "BULL",
          calibration: {
            calibrator_version: "monotone-firth-logistic-v1",
            intercept: "-1.2",
            slope: "2.7",
            training_window_months: ["2041-01", "2045-12"],
            label_watermark_at: "2046-07-01T08:00:00Z",
            training_record_count: 600,
            positive_record_count: 300,
            negative_record_count: 300
          },
          members: [
            {
              security_id: "SYNTH-ALPHA",
              research_id: "synthetic-research-alpha",
              raw_success_score: "0.95",
              calibrated_probability: "0.87",
              candidate: true,
              risk_status: "ACCEPTED",
              risk_gates: [{ gate_id: "RISK_REVIEW", status: "PASSED" }],
              risk_reasons: ["INDEPENDENT_RISK_ACCEPTED"],
              market_state_qualified: true,
              market_state_qualification_status: "AT_RISK",
              data_complete: true,
              thesis: "Original synthetic investment thesis.",
              principal_risks: ["Original synthetic risk."],
              evidence_freshness: "FRESH_AT_CUTOFF",
              valid_market_dates: [
                "2046-07-02",
                "2046-07-03",
                "2046-07-04",
                "2046-07-05",
                "2046-07-06"
              ],
              reasons: ["MARKET_STATE_QUALIFICATION_AT_RISK"]
            }
          ],
          population: {
            valid_monthly: true,
            recommendation_coverage_denominator: true,
            recommendation_coverage_pass: true,
            availability_failure: null
          },
          valid_market_dates: [
            "2046-07-02",
            "2046-07-03",
            "2046-07-04",
            "2046-07-05",
            "2046-07-06"
          ],
          reasons: [],
          availability_failure: null,
          actionable: false
        }
      }
    };
    window.history.pushState({}, "", `/reports/${report.report_version_id}`);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(candidateReport), {
          status: 200,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
        })
      )
    );

    render(<App />);

    const release = await screen.findByRole("region", { name: "Candidate batch release" });
    expect(release).toHaveTextContent("CANDIDATES");
    expect(release).toHaveTextContent("2046-07-02 through 2046-07-06");
    expect(release).toHaveTextContent("0.87");
    expect(release).toHaveTextContent("Independent risk status");
    expect(release).toHaveTextContent("RISK_REVIEW: PASSED");
    expect(release).toHaveTextContent("INDEPENDENT_RISK_ACCEPTED");
    expect(release).toHaveTextContent("At risk");
    expect(release).toHaveTextContent("Original synthetic investment thesis.");
    expect(release).not.toHaveTextContent("account route");
    expect(release).not.toHaveTextContent("position quantity");
  });

  it("renders the research rejection projection and its durable stages", async () => {
    const researchReport = {
      ...report,
      result: {
        ...report.result,
        outcome_code: "RESEARCH_REJECTED",
        summary: "Synthetic fixed-ten research was rejected by independent risk.",
        key_reasons: ["INDEPENDENT_RISK_VETO"],
        research: {
          actionable: false,
          disposition: "REJECTED",
          reasons: ["INDEPENDENT_RISK_VETO"],
          members: [
            {
              security_id: "synthetic-security-00",
              research_id: "research-00",
              evidence_refs: ["financial-statements-evidence-00"],
              thesis: "The fictional thesis is bounded by the frozen evidence.",
              bull_case: "The fictional upside case remains conditional.",
              bear_case: "The fictional downside case remains explicit.",
              knowledge_cutoff: "2042-05-31T23:59:59Z"
            }
          ],
          raw_scores: [
            {
              security_id: "synthetic-security-00",
              research_id: "research-00",
              target: "SIX_MONTH_TERMINAL_20_PERCENT",
              algorithm: "ELASTIC_NET_LOGISTIC",
              model_version: "elastic-net-logistic-z20-v1",
              training_window_id: "synthetic-training-window-expanding-60m",
              training_window_policy: "EXPANDING_60_TO_119_ROLLING_120",
              training_window_kind: "EXPANDING",
              training_window_month_count: 60,
              training_window_start_month: "2037-06",
              training_window_end_month: "2042-05",
              training_months: ["2037-06", "2042-05"],
              label_watermark_month: "2042-05",
              normalization_snapshot_id: "synthetic-normalization-v1",
              mature_months: 60,
              training_record_count: 500,
              positive_record_count: 250,
              negative_record_count: 250,
              intercept: "-0.4",
              structured_inputs: {},
              transformed_inputs: {},
              coefficients: {},
              feature_transformations: {},
              contributions: {
                screening_positive_prior: "0.15",
                single_quarter_revenue_acceleration: "0.08"
              },
              z20: "1.25",
              l1_ratio: "0.25",
              l2_ratio: "0.75",
              penalty_strength: "1"
            }
          ],
          tool_evidence: [],
          handoff: {
            contract_version: "1.0.0",
            scope: "D0_SYNTHETIC_RESEARCH_ONLY",
            selection_object_id: "selection-object-1616",
            selection_event_id: "selection-event-1616",
            security_ids: ["synthetic-security-00"],
            targets: ["SIX_MONTH_POSITIVE_RETURN", "SIX_MONTH_TERMINAL_20_PERCENT"],
            cutoff_at: "2042-05-31T23:59:59Z",
            knowledge_cutoff: "2042-05-31T23:59:59Z",
            evidence_ids: ["financial-statements-evidence-00"],
            research_run_id: "research-run-1616",
            risk_run_id: "risk-run-1616",
            research_definition_id: "synthetic-monthly-research",
            research_definition_version: "2.0.0",
            risk_definition_id: "synthetic-independent-risk-veto",
            risk_definition_version: "1.0.0",
            research_model_adapter_id: "m-agent-deterministic-research-adapter",
            risk_model_adapter_id: "m-agent-deterministic-risk-adapter",
            research_routing_policy_version: "monthly-research-risk-veto-v1",
            research_output_contract_id: "synthetic-monthly-research-draft",
            research_output_contract_version: "1.0.0",
            risk_output_contract_id: "synthetic-independent-risk-veto",
            risk_output_contract_version: "1.0.0",
            screening_strategy_version: "synthetic-dual-head-strategy-v1",
            screening_snapshot_id: "synthetic-dual-head-snapshot-v1",
            selection_fingerprint: "a".repeat(64),
            raw_scores: [],
            tool_evidence: [],
            member_handoffs: [],
            risk_veto: {
              run_id: "risk-run-1616",
              definition_id: "synthetic-independent-risk-veto",
              definition_version: "1.0.0",
              disposition: "REJECTED",
              gates: [{ gate_id: "SYNTHETIC_RISK_VETO", status: "FAILED" }],
              reasons: ["INDEPENDENT_RISK_VETO"],
              member_vetoes: []
            }
          },
          risk_veto: {
            run_id: "risk-run-1616",
            definition_id: "synthetic-independent-risk-veto",
            definition_version: "1.0.0",
            disposition: "REJECTED",
            gates: [{ gate_id: "SYNTHETIC_RISK_VETO", status: "FAILED" }],
            reasons: ["INDEPENDENT_RISK_VETO"],
            member_vetoes: [
              {
                security_id: "synthetic-security-00",
                research_id: "research-00",
                disposition: "REJECTED",
                gates: [{ gate_id: "SYNTHETIC_MEMBER_RISK", status: "FAILED" }],
                reasons: ["LIQUIDITY_WARNING"]
              }
            ]
          }
        }
      },
      stage_results: [
        { phase: "RESEARCH", status: "SUCCEEDED", reasons: [], gate_results: [] },
        {
          phase: "RISK_VETO",
          status: "REJECTED",
          reasons: ["INDEPENDENT_RISK_VETO"],
          gate_results: [{ gate_id: "SYNTHETIC_RISK_VETO", status: "FAILED" }]
        },
        { phase: "BUSINESS_DECISION", status: "REJECTED", reasons: [], gate_results: [] }
      ]
    };
    window.history.pushState({}, "", `/reports/${researchReport.report_version_id}`);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(researchReport), {
        status: 200,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("RESEARCH_REJECTED")).toBeVisible();
    expect(screen.getByRole("region", { name: "Key reasons" })).toHaveTextContent(
      "INDEPENDENT_RISK_VETO"
    );
    const research = screen.getByRole("region", { name: "Research evidence" });
    expect(research).toHaveTextContent("synthetic-security-00");
    expect(research).toHaveTextContent("The fictional thesis is bounded by the frozen evidence.");
    expect(research).toHaveTextContent("1.25");
    expect(research).toHaveTextContent("Uncalibrated raw success score");
    expect(research).toHaveTextContent("screening_positive_prior: 0.15");
    expect(research).toHaveTextContent("risk-run-1616");
    expect(research).toHaveTextContent("LIQUIDITY_WARNING");
    const stages = screen.getByRole("region", { name: "Decision stages" });
    expect(stages).toHaveTextContent("RESEARCH");
    expect(stages).toHaveTextContent("RISK_VETO");
    expect(stages).toHaveTextContent("BUSINESS_DECISION");
    expect(fetchMock).toHaveBeenCalledTimes(1);
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

  it("renders saved issuer obligations and unknown execution quantities without recalculation", async () => {
    const concentrationReport = {
      ...report,
      result: {
        ...report.result,
        concentration: {
          disposition: "BLOCKED",
          reasons: ["CONCENTRATION_POSITION_FACTS_UNRESOLVED"],
          portfolio_id: "synthetic-decision-portfolio-alpha",
          snapshot_id: "synthetic-position-snapshot-alpha",
          cutoff_at: "2042-05-17T16:00:00Z",
          valuation_currency: "XSP",
          portfolio_net_liquidation_equity: "9500",
          risk_budget_version_id: "synthetic-risk-budget-alpha",
          thresholds: { target_ratio: "0.13", hard_ratio: "0.19" },
          issuers: [
            {
              issuer_id: "FICTIONAL-ORBITAL-MOSAIC",
              current_market_exposure: "1900",
              position_weight: "0.2",
              state: "REMEDIATION_REQUIRED",
              new_exposure_blocked: true,
              direction: "REDUCE",
              obligation_id: "synthetic-concentration-obligation-alpha",
              targets: [
                {
                  security_id: "XQZ-4017",
                  target_quantity: "123.5",
                  required_reduction_quantity: null
                }
              ],
              exposure_gap: null,
              execution_blocked: true
            }
          ]
        }
      }
    };
    window.history.pushState({}, "", `/reports/${report.report_version_id}`);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(concentrationReport), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        })
      )
    );

    render(<App />);

    const region = await screen.findByRole("region", { name: "Issuer concentration" });
    for (const value of [
      "synthetic-risk-budget-alpha",
      "9500",
      "0.13",
      "0.19",
      "123.5",
      "synthetic-concentration-obligation-alpha",
      "REMEDIATION_REQUIRED",
      "REDUCE",
      "Execution blocked",
      "Unknown",
      "New exposure blocked"
    ]) {
      expect(region).toHaveTextContent(value);
    }
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
