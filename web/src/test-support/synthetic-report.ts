import fixture from "../../../tests/fixtures/synthetic/formal_report_projection.json";

import type { FormalReport } from "../api/client";

type SyntheticReportFixture = {
  synthetic: boolean;
  generator_version: string;
  seed: number;
  report: FormalReport;
};

const syntheticReportFixture = fixture as unknown as SyntheticReportFixture;

export const syntheticReport = syntheticReportFixture.report;
