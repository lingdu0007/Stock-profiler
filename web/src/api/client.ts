import {
  startRegistration,
  startAuthentication,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON
} from "@simplewebauthn/browser";
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

const client = createClient<paths>({
  baseUrl: "",
  fetch: (request) =>
    globalThis.fetch(request, {
      cache: "no-store",
      credentials: "same-origin"
    })
});

export type FormalReport = components["schemas"]["FormalReport"];
export type VersionBundle = components["schemas"]["VersionDiagnosticDto"];

export class ApiResponseError extends Error {
  constructor(readonly status: number) {
    super(`API request failed with status ${status}.`);
  }
}

function csrfToken(): string | null {
  const cookie = document.cookie
    .split("; ")
    .find((value) => value.startsWith("__Host-stock_profiler_csrf="));
  return cookie ? decodeURIComponent(cookie.split("=", 2)[1] ?? "") : null;
}

export async function fetchFormalReport(reportVersionId: string): Promise<FormalReport> {
  const csrf = csrfToken();
  if (csrf) {
    const refreshResponse = await client.POST("/api/v1/auth/session/refresh", {
      params: { header: { "X-CSRF-Token": csrf } }
    });
    if (!refreshResponse.data) {
      throw new ApiResponseError(refreshResponse.response.status);
    }
  }
  const { data, response } = await client.GET("/api/v1/reports/{report_version_id}", {
    params: { path: { report_version_id: reportVersionId } }
  });
  if (!data) {
    throw new ApiResponseError(response.status);
  }
  return data;
}

export async function fetchVersionBundle(): Promise<VersionBundle> {
  const { data, response } = await client.GET("/api/v1/diagnostics/version");
  if (!data) {
    throw new ApiResponseError(response.status);
  }
  return data;
}

export async function completePasskeyRegistration(grantId: string): Promise<void> {
  const optionsResponse = await client.POST("/api/v1/auth/passkeys/registration/options", {
    params: { header: { "X-Host-Console-Grant": grantId } }
  });
  if (!optionsResponse.data) {
    throw new ApiResponseError(optionsResponse.response.status);
  }
  const credential = await startRegistration({
    optionsJSON: optionsResponse.data.options as unknown as PublicKeyCredentialCreationOptionsJSON
  });
  const verifyResponse = await client.POST("/api/v1/auth/passkeys/registration/verify", {
    body: {
      challenge_id: optionsResponse.data.challenge_id,
      credential: credential as unknown as Record<string, unknown>
    }
  });
  if (!verifyResponse.response.ok) {
    throw new ApiResponseError(verifyResponse.response.status);
  }
}

export async function completePasskeyAuthentication(): Promise<void> {
  const optionsResponse = await client.POST("/api/v1/auth/passkeys/authentication/options");
  if (!optionsResponse.data) {
    throw new ApiResponseError(optionsResponse.response.status);
  }
  const credential = await startAuthentication({
    optionsJSON: optionsResponse.data.options as unknown as PublicKeyCredentialRequestOptionsJSON
  });
  const verifyResponse = await client.POST("/api/v1/auth/passkeys/authentication/verify", {
    body: {
      challenge_id: optionsResponse.data.challenge_id,
      credential: credential as unknown as Record<string, unknown>
    }
  });
  if (!verifyResponse.data) {
    throw new ApiResponseError(verifyResponse.response.status);
  }
}
