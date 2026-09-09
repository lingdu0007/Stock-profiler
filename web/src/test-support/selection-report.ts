import type { FormalReport } from "../api/client";

export function selectionReport(
  original: FormalReport,
  disposition: "FROZEN" | "ABSTAINED" | "DATA_FAILED"
): FormalReport {
  return {
    ...original,
    result: {
      ...original.result,
      selection: {
        disposition,
        cutoff_at: "2042-05-30T23:59:59+08:00",
        universe_event_id: "synthetic-universe-event",
        policy: {
          version_id: "synthetic-selection-policy-v1",
          cohort_size: 6,
          industry_limit: 2,
          capitalization_limit: 3,
          correlation_sessions: 8,
          maximum_correlation: "0.7",
          positive_weight: 3,
          terminal_weight: 2
        },
        qualification_scope: "D0_SYNTHETIC_CONTRACT_ONLY",
        actionable: false,
        members:
          disposition === "FROZEN"
            ? Array.from({ length: 6 }, (_, index) => `XQZ-SELECT-${index}`)
            : [],
        ranking:
          disposition === "DATA_FAILED"
            ? []
            : Array.from({ length: 12 }, (_, index) => ({
                security_id: `XQZ-SELECT-${index}`,
                rank: index + 1,
                positive_percentile: "51",
                terminal_percentile: "51",
                composite_score: "51"
              })),
        scan:
          disposition === "DATA_FAILED"
            ? []
            : [
                {
                  security_id: "XQZ-SELECT-0",
                  capitalization_group: 0,
                  included: true,
                  correlated_with: [],
                  reasons: []
                }
              ],
        population: {
          scheduled_monthly: true,
          valid_monthly: disposition !== "DATA_FAILED",
          recommendation_coverage_denominator: disposition !== "DATA_FAILED",
          selection_pass_denominator: disposition !== "DATA_FAILED",
          selection_pass: disposition === "ABSTAINED" ? false : null,
          availability_failure: disposition === "DATA_FAILED" ? "DATA" : null
        },
        reasons: disposition === "ABSTAINED" ? ["CONSTRAINTS_PREVENT_COMPLETE_COHORT"] : []
      }
    }
  };
}
