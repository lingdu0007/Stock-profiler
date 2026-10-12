import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it } from "vitest";
import type { FormalReport } from "./api/client";
import { ExitEvaluationEvidence } from "./ExitEvaluationEvidence";

afterEach(cleanup);

it("renders missing evidence and independent denominators without action controls", () => {
  const evidence: NonNullable<FormalReport["result"]["exit_evaluation"]> = {
    disposition: "INDETERMINATE",
    policy: {
      version_id: "fictional-policy",
      start_month: "2042-07",
      end_month: "2043-01",
      market_calendar_version: "synthetic-market-calendar-v1",
      model_version: "fictional-model",
      action_version: "fictional-action",
      cost_version: "fictional-cost",
      cost_curve: {
        commission_rate: "0",
        minimum_commission: "5",
        fee_rate: "0",
        tax_rate: "0",
        slippage_rate: "0"
      },
      baseline_version: "fictional-baseline",
      loss_grids: [".05", ".10", ".15", ".20"],
      selected_grid: ".10",
      states: ["BULL"],
      bootstrap_repetitions: 99,
      confidence: ".95",
      block_lengths: [6, 9, 12],
      continuous_quantile: ".5",
      fixed_reduce_loss: ".10",
      fixed_exit_loss: ".20",
      trailing_reduce_loss: ".10",
      trailing_exit_loss: ".20",
      safe_downside: ".20",
      risky_downside: ".40",
      established_value: ".70",
      denied_value: ".30"
    },
    registration_event_id: "fictional-registration",
    cutoff_at: "2043-04-02T16:00:00Z",
    previous_event_id: "fictional-event",
    previous_report_id: "fictional-report",
    report_version: 2,
    counts: { registered: 1, due: 1, evaluable: 0, due_missing: 1, immature: 0 },
    positions: [],
    forecast_freezes: [],
    theses: [],
    actionable: false,
    authorization_granted: false,
    inference: {
      disposition: "INDETERMINATE",
      inference_unit: "WHOLE_LIFECYCLE_COMMON_ENTRY_MONTH_BLOCKS",
      watermarks: {
        overall: {
          mature_months: 0,
          position_count: 0,
          nonoverlapping_windows: 0,
          periods: 0,
          directions: { HOLD: 0, REDUCE: 0, EXIT: 0 },
          target_positions: {},
          target_events: {},
          target_nonevents: {},
          sufficient: false,
          disposition: "INDETERMINATE"
        }
      },
      gates: {
        "overall:net:HOLD_END": {
          estimate: null,
          bound: null,
          direction: "LOWER",
          threshold: "0",
          strict: true,
          applicable: true,
          passed: null,
          block_bounds: { "6": null, "9": null, "12": null },
          undefined_resamples: { "6": 99, "9": 99, "12": 99 }
        }
      },
      diagnostics: {},
      pinball_loss: {},
      resampling: {},
      missing_evidence: ["EXIT_PATH_MISSING:fictional-position"]
    }
  };
  render(
    <MemoryRouter>
      <ExitEvaluationEvidence evidence={evidence} />
    </MemoryRouter>
  );
  const region = screen.getByRole("region", { name: "Standard exit evidence" });
  expect(region).toHaveTextContent("INDETERMINATE");
  expect(region).toHaveTextContent("Independent positions");
  expect(region).toHaveTextContent("Unresolved");
  expect(region).toHaveTextContent("EXIT_PATH_MISSING:fictional-position");
  expect(region).toHaveTextContent("Historical evidence does not authorize position actions");
  expect(screen.getByRole("link", { name: "Read previous exit evaluation" })).toHaveAttribute(
    "href",
    "/reports/fictional-report"
  );
  expect(region.querySelector("button")).toBeNull();
});
