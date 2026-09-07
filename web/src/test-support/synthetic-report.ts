import fixture from "../../../tests/fixtures/synthetic/formal_report_projection.json";

import type { FormalReport } from "../api/client";

type SyntheticReportFixture = {
  synthetic: boolean;
  generator_version: string;
  seed: number;
  report: FormalReport;
};

const stagePhases = new Set([
  "FRAMEWORK_RUN",
  "HOST_VALIDATION",
  "BUSINESS_DECISION",
  "QUALIFICATION",
  "PORTFOLIO_AUTHORIZATION",
  "POSITION_RECONCILIATION",
  "DRAWDOWN_PROTECTION",
  "ADJUDICATION_LIFECYCLE",
  "VALIDITY_LIFECYCLE",
  "EXECUTION_LIFECYCLE",
  "COMMIT_RECONCILIATION",
  "BUSINESS_COMMIT",
  "PUBLICATION",
  "NOTIFICATION",
  "CORRECTION"
]);
const stageStatuses = new Set([
  "CREATED",
  "RUNNING",
  "WAITING",
  "SUCCEEDED",
  "REJECTED",
  "ABSTAINED",
  "FAILED",
  "PENDING",
  "EXPIRED",
  "EXECUTION_BLOCKED",
  "UNKNOWN",
  "CANCELLED"
]);
const gateStatuses = new Set(["PASSED", "FAILED", "UNKNOWN"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function hasStringFields(value: unknown, fields: string[]): value is Record<string, string> {
  return isRecord(value) && fields.every((field) => typeof value[field] === "string");
}

function isFormalReport(value: unknown): value is FormalReport {
  if (
    !hasStringFields(value, [
      "report_version_id",
      "event_id",
      "business_object_id",
      "framework_run_id",
      "case_id",
      "qualification_scope",
      "generated_at",
      "knowledge_cutoff"
    ]) ||
    typeof value.synthetic !== "boolean" ||
    !isRecord(value.result) ||
    !hasStringFields(value.result, ["outcome_code", "summary"]) ||
    !Array.isArray(value.result.key_reasons) ||
    !value.result.key_reasons.every((reason) => typeof reason === "string") ||
    !hasStringFields(value.evidence_clock, [
      "fact_effective_at",
      "source_published_at",
      "acquired_at",
      "validated_at"
    ]) ||
    !hasStringFields(value.version_bundle, [
      "case_contract_version",
      "host_contract_version",
      "host_application_version",
      "host_source_sha",
      "agent_definition_id",
      "agent_definition_version",
      "model_adapter_id",
      "routing_policy_version",
      "output_contract_version",
      "report_projection_contract_version",
      "m_agent_version",
      "m_agent_wheel_url",
      "m_agent_wheel_sha256",
      "m_agent_release_commit"
    ]) ||
    !Array.isArray(value.stage_results)
  ) {
    return false;
  }
  if (
    value.corrects_event_id !== undefined &&
    value.corrects_event_id !== null &&
    typeof value.corrects_event_id !== "string"
  ) {
    return false;
  }
  return value.stage_results.every(
    (stage) =>
      isRecord(stage) &&
      typeof stage.phase === "string" &&
      stagePhases.has(stage.phase) &&
      typeof stage.status === "string" &&
      stageStatuses.has(stage.status) &&
      Array.isArray(stage.reasons) &&
      stage.reasons.every((reason) => typeof reason === "string") &&
      Array.isArray(stage.gate_results) &&
      stage.gate_results.every(
        (gate) =>
          isRecord(gate) &&
          typeof gate.gate_id === "string" &&
          typeof gate.status === "string" &&
          gateStatuses.has(gate.status)
      )
  );
}

function parseSyntheticReportFixture(value: unknown): SyntheticReportFixture {
  if (
    !isRecord(value) ||
    typeof value.synthetic !== "boolean" ||
    typeof value.generator_version !== "string" ||
    typeof value.seed !== "number" ||
    !isFormalReport(value.report)
  ) {
    throw new Error("synthetic report fixture does not satisfy the generated report contract");
  }
  return {
    synthetic: value.synthetic,
    generator_version: value.generator_version,
    seed: value.seed,
    report: value.report
  };
}

const syntheticReportFixture = parseSyntheticReportFixture(fixture);

export const syntheticReport = syntheticReportFixture.report;
