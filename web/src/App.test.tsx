import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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

  it("shows correction lineage supplied by the committed report projection", async () => {
    const correctedReport = { ...report, corrects_event_id: "decision-event-original-4017" };
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
