import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/synthetic/standard_outcomes.json";
import { App } from "./App";
import type { FormalReport } from "./api/client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it("shows independent denominators and immutable standard results from the saved report", async () => {
  const report = fixture.report as unknown as FormalReport;
  window.history.pushState({}, "", `/reports/${report.report_version_id}`);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    )
  );
  render(<App />);
  const section = await screen.findByRole("region", { name: "Standard outcomes" });
  expect(section).toHaveTextContent("Due outcomes missing");
  expect(section).toHaveTextContent("INDETERMINATE");
  expect(section).toHaveTextContent("SELECTION");
  expect(section).toHaveTextContent("PROBABILITY");
  expect(section).toHaveTextContent("CANDIDATE");
  expect(section).toHaveTextContent("20.00%");
  expect(section).toHaveTextContent("Terminal net total return");
  expect(
    within(section).getByRole("link", { name: "Read saved candidate report" })
  ).toHaveAttribute("href", expect.stringContaining("/reports/"));
  expect(section).toHaveTextContent("Expired");
  expect(section).toHaveTextContent(
    "Standard results are independent of personal purchases and costs."
  );
});
