import { describe, expect, it } from "vitest";
import { ApiError, apiRequest } from "./api";

type Call = { url: string; init: RequestInit };

function fake(status: number, body: unknown, calls: Call[] = []): typeof fetch {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return new Response(status === 204 ? null : JSON.stringify(body), { status });
  }) as typeof fetch;
}

const token = async () => "id-token";

describe("apiRequest", () => {
  it("calls the versioned endpoint with the bearer token and query", async () => {
    const calls: Call[] = [];
    const body = await apiRequest("/securities", token, { query: { q: "RELIANCE" } }, "http://api.test", fake(200, { ok: 1 }, calls));
    expect(body).toEqual({ ok: 1 });
    expect(calls[0]!.url).toBe("http://api.test/api/v1/securities?q=RELIANCE");
    expect((calls[0]!.init.headers as Record<string, string>).Authorization).toBe("Bearer id-token");
  });

  it("sends no Authorization header without a token", async () => {
    const calls: Call[] = [];
    await apiRequest("/health", null, {}, "http://api.test", fake(200, {}, calls));
    expect((calls[0]!.init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it.each([
    [401, "sign in required", "signed_out"],
    [403, "access pending approval", "pending"],
    [403, "admin only", "forbidden"],
    [404, "unknown security", "not_found"],
    [503, "the lake is being republished; retry shortly", "unavailable"],
  ])("maps %i %s to %s", async (status, detail, kind) => {
    await expect(
      apiRequest("/me", token, {}, "http://api.test", fake(status, { detail })),
    ).rejects.toMatchObject({ name: "ApiError", kind, status, message: detail });
  });

  it("reports an unreachable API distinctly", async () => {
    const down = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    await expect(apiRequest("/health", null, {}, "http://api.test", down)).rejects.toMatchObject({
      kind: "unavailable",
    });
    await expect(apiRequest("/health", null, {}, "http://api.test", down)).rejects.toBeInstanceOf(ApiError);
  });

  it("sends JSON bodies and accepts empty replies", async () => {
    const calls: Call[] = [];
    await apiRequest("/admin/users/u1", token, { method: "PATCH", body: { enabled: true } }, "http://api.test", fake(204, null, calls));
    expect(calls[0]!.init.method).toBe("PATCH");
    expect(calls[0]!.init.body).toBe('{"enabled":true}');
  });
});
