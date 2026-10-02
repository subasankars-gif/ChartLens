/**
 * ChartLens API client. Types are generated from the API's OpenAPI document
 * (docs/api/openapi.json → src/lib/api-schema.ts, `pnpm gen:api`), so the frontend and
 * the API cannot drift apart. The browser holds no secrets: every request carries the
 * signed-in user's Firebase ID token and the API decides what it may see (ADR-0016/17).
 */

import { config } from "./config";
import type { components } from "./api-schema";

export const API_PREFIX = "/api/v1";

type Schemas = components["schemas"];
export type Health = Schemas["HealthResponse"];
export type SearchResponse = Schemas["SearchResponse"];
export type SecuritySummary = Schemas["SecuritySummary"];
export type SecurityDetail = Schemas["SecurityDetail"];
export type WeeklyResponse = Schemas["WeeklyResponse"];
export type WeeklyBar = Schemas["WeeklyBarOut"];
export type DataQuality = Schemas["DataQualityResponse"];
export type ServingStatus = Schemas["ServingStatus"];
export type User = Schemas["User"];

export type ApiErrorKind = "signed_out" | "pending" | "forbidden" | "not_found" | "unavailable" | "other";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly kind: ApiErrorKind,
    readonly status?: number,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ApiError";
  }
}

export function apiBaseUrl(): string {
  return config.apiBaseUrl;
}

function kindOf(status: number, detail: string): ApiErrorKind {
  if (status === 401) return "signed_out";
  if (status === 403 && detail === "access pending approval") return "pending";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 503) return "unavailable";
  return "other";
}

export type TokenSource = () => Promise<string | null>;

export async function apiRequest<T>(
  path: string,
  token: TokenSource | null,
  init: { method?: string; body?: unknown; query?: Record<string, string> } = {},
  baseUrl: string = apiBaseUrl(),
  fetchImpl: typeof fetch = fetch,
): Promise<T> {
  const url = new URL(`${baseUrl}${API_PREFIX}${path}`);
  for (const [k, v] of Object.entries(init.query ?? {})) url.searchParams.set(k, v);
  const headers: Record<string, string> = { Accept: "application/json" };
  const bearer = token ? await token() : null;
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetchImpl(url.toString(), {
      method: init.method ?? "GET",
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
    });
  } catch (cause) {
    throw new ApiError(`The ChartLens API at ${baseUrl} could not be reached.`, "unavailable", undefined, {
      cause,
    });
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body) detail = String((body as { detail: unknown }).detail);
    } catch {
      /* no JSON body */
    }
    throw new ApiError(detail || `The API answered ${response.status}.`, kindOf(response.status, detail), response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: (fetchImpl?: typeof fetch) => apiRequest<Health>("/health", null, {}, apiBaseUrl(), fetchImpl),
  me: (t: TokenSource) => apiRequest<User>("/me", t),
  status: (t: TokenSource) => apiRequest<ServingStatus>("/system/status", t),
  search: (t: TokenSource, q: string, universe: "analytical" | "all" = "analytical") =>
    apiRequest<SearchResponse>("/securities", t, { query: { q, universe, limit: "20" } }),
  security: (t: TokenSource, id: string) => apiRequest<SecurityDetail>(`/securities/${encodeURIComponent(id)}`, t),
  weekly: (t: TokenSource, id: string, segments: "valid" | "all") =>
    apiRequest<WeeklyResponse>(`/securities/${encodeURIComponent(id)}/weekly`, t, { query: { segments } }),
  dataQuality: (t: TokenSource, id: string) =>
    apiRequest<DataQuality>(`/securities/${encodeURIComponent(id)}/data-quality`, t),
  users: (t: TokenSource) => apiRequest<User[]>("/admin/users", t),
  updateUser: (t: TokenSource, uid: string, change: { enabled?: boolean; role?: "admin" | "user" }) =>
    apiRequest<User>(`/admin/users/${encodeURIComponent(uid)}`, t, { method: "PATCH", body: change }),
};
