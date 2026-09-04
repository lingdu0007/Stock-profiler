import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

const client = createClient<paths>({
  baseUrl: "",
  fetch: (request) => globalThis.fetch(request)
});

export type VersionBundle = components["schemas"]["VersionDiagnosticDto"];

export async function fetchVersionBundle(): Promise<VersionBundle> {
  const { data, error } = await client.GET("/api/v1/diagnostics/version");
  if (error || !data) {
    throw new Error("Version diagnostics are unavailable.");
  }
  return data;
}
