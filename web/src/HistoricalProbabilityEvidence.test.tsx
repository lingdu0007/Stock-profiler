import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/synthetic/historical_probability.json";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it("reads saved directional probability evidence without action permission", async () => {
  const report = fixture.report;
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
  const evidence = await screen.findByRole("region", { name: "Historical probability evidence" });
  expect(evidence).toHaveTextContent("INSUFFICIENT");
  expect(evidence).toHaveTextContent("overall:overconfidence");
  expect(evidence).toHaveTextContent("UPPER");
  expect(evidence).toHaveTextContent("Log Loss");
  expect(evidence).toHaveTextContent("BULL");
  expect(evidence).toHaveTextContent("due_missing");
  expect(evidence).toHaveTextContent("No recommendation or execution authorization");
  expect(evidence.querySelector("button")).toBeNull();
});
