/**
 * ChartLens API client.
 *
 * Types are hand-written for Milestone 1. From Milestone 5 they are generated from
 * the API's OpenAPI schema so the frontend and backend cannot drift apart.
 */

export const API_PREFIX = "/api/v1";

export type Health = {
  status: "ok";
  environment: "local" | "ci" | "prod";
  exchange: string;
  methodology_hash: string;
  versions: { api: string; core: string; engine: string; pipeline: string };
  server_time: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ApiError";
  }
}

export function apiBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8080").replace(/\/+$/, "");
}

function isHealth(value: unknown): value is Health {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    v.status === "ok" &&
    typeof v.exchange === "string" &&
    typeof v.methodology_hash === "string" &&
    typeof v.versions === "object" &&
    v.versions !== null
  );
}

export async function fetchHealth(
  baseUrl: string = apiBaseUrl(),
  fetchImpl: typeof fetch = fetch,
): Promise<Health> {
  let response: Response;
  try {
    response = await fetchImpl(`${baseUrl}${API_PREFIX}/health`, { cache: "no-store" });
  } catch (cause) {
    throw new ApiError(`API unreachable at ${baseUrl}`, undefined, { cause });
  }
  if (!response.ok) {
    throw new ApiError(`API returned ${response.status}`, response.status);
  }
  const body: unknown = await response.json();
  if (!isHealth(body)) {
    throw new ApiError("API returned an unexpected health payload");
  }
  return body;
}
