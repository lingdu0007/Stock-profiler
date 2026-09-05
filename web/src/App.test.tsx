import "@testing-library/jest-dom/vitest";

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const report = {
  report_version_id: "report-version-4017",
  event_id: "decision-event-4017",
  business_object_id: "business-object-4017",
  framework_run_id: "framework-run-4017",
  case_id: "d0-orbital-mosaic-001",
  synthetic: true,
  qualification_scope: "D0_SYNTHETIC_CONTRACT_ONLY",
  generated_at: "2042-05-17T15:18:00Z",
  knowledge_cutoff: "2042-05-17T16:00:00Z",
  evidence_clock: {
    fact_effective_at: "2042-05-17T15:00:00Z",
    source_published_at: "2042-05-17T15:12:00Z",
    acquired_at: "2042-05-17T15:16:00Z",
    validated_at: "2042-05-17T15:18:00Z"
  },
  version_bundle: {
    case_contract_version: "1.0.0",
    host_contract_version: "1.0.0",
    agent_definition_id: "synthetic-frozen-decision-case",
    agent_definition_version: "1.0.0",
    output_contract_version: "1.0.0",
    m_agent_version: "0.5.0",
    m_agent_release_commit: "743651e5c74a4865f25a31dab68d188b5b0aed64"
  },
  result: {
    outcome_code: "SYNTHETIC_REVIEW_COMPLETE",
    summary: "Synthetic D0 decision case completed under the frozen contract.",
    key_reasons: [
      "All required fictional evidence records are present.",
      "The output is D0 synthetic evidence and is not a recommendation."
    ]
  }
};

describe("App", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
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
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect((fetchMock.mock.calls[0]?.[0] as Request).url).toContain(
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
});
