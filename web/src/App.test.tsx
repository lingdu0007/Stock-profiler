import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import correctionFixture from "../../tests/fixtures/synthetic/frozen_correction_evidence.json";
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
            portfolio_id: "synthetic-decision-portfolio-alpha",
            snapshot_id: "synthetic-portfolio-snapshot-alpha",
            included_accounts: [
              {
                account_id: "synthetic-account-4017",
                account_type: "SIMULATED_CASH",
                scope: "FULL_ACCOUNT",
                captured_at: "2042-05-17T16:00:00Z",
                cash_fact_id: "synthetic-cash-fact-4017",
                positions_fact_id: "synthetic-positions-fact-4017",
                receivables_fact_id: "synthetic-receivables-fact-4017",
                payables_fact_id: "synthetic-payables-fact-4017",
                unfinished_trades_fact_id: "synthetic-trades-fact-4017"
              }
            ],
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
    expect(authorization).toHaveTextContent("synthetic-account-margin-2001");
    expect(authorization).toHaveTextContent("UNSUPPORTED_ACCOUNT_TYPE");
  });

  it("renders the confirmation and budget snapshot retained by a portfolio use", async () => {
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
        confirmed: true
      },
      proposal: {
        portfolio_id: "synthetic-decision-portfolio-alpha",
        snapshot: {
          snapshot_id: "synthetic-portfolio-snapshot-alpha",
          cutoff_at: "2042-05-17T16:00:00Z",
          accounts: [],
          selected_account_ids: ["synthetic-account-4017"]
        },
        risk_budget: {
          contract_version: "1.0.0",
          version_id: "synthetic-risk-budget-alpha",
          synthetic: true,
          generator_version: "portfolio-authorization/1",
          seed: 6101,
          effective_at: "2042-05-17T16:00:00Z",
          expires_at: "2042-11-17T16:00:00Z",
          concentration: { target_ratio: "0.13", hard_ratio: "0.19" },
          stress: { target_ratio: "0.14", hard_ratio: "0.18" },
          cash: { target_ratio: "0.39", hard_ratio: "0.23" },
          drawdown: {
            caution_ratio: "0.11",
            defensive_ratio: "0.16",
            preservation_ratio: "0.21"
          },
          downside_grid: ["0.04", "0.09"],
          protection_floor: {
            new_exposure_blocked: true,
            retained_directions: ["REDUCE", "EXIT"]
          }
        },
        cash_obligations: [
          {
            obligation_id: "synthetic-cash-obligation-alpha",
            amount: "125.50",
            purpose: "synthetic-near-term-liquidity",
            latest_usable_at: "2042-08-17T16:00:00Z",
            target_account_id: "synthetic-account-4017"
          }
        ]
      }
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
    expect(confirmation).toHaveTextContent("2042-11-17T16:00:00Z");
    expect(confirmation).toHaveTextContent("synthetic-cash-obligation-alpha");
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
