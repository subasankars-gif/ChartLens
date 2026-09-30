import { describe, expect, it } from "vitest";
import { ApiError, fetchHealth, type Health } from "./api";

const healthy: Health = {
  status: "ok",
  environment: "local",
  exchange: "NSE",
  methodology_hash: "1126f307b988",
  versions: { api: "0.1.0", core: "0.1.0", engine: "0.1.0", pipeline: "0.1.0" },
  server_time: "2026-09-30T13:30:00Z",
};

function fakeFetch(status: number, body: unknown): typeof fetch {
  return (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
}

describe("fetchHealth", () => {
  it("calls the versioned endpoint and returns the payload", async () => {
    let calledWith = "";
    const spy = (async (url: string | URL | Request) => {
      calledWith = String(url);
      return new Response(JSON.stringify(healthy), { status: 200 });
    }) as typeof fetch;
    await expect(fetchHealth("http://api.test", spy)).resolves.toEqual(healthy);
    expect(calledWith).toBe("http://api.test/api/v1/health");
  });

  it("raises ApiError with the status on HTTP errors", async () => {
    await expect(fetchHealth("http://api.test", fakeFetch(503, {}))).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
    });
  });

  it("rejects payloads that do not look like health", async () => {
    await expect(fetchHealth("http://api.test", fakeFetch(200, { status: "ok" }))).rejects.toBeInstanceOf(
      ApiError,
    );
  });

  it("reports an unreachable API distinctly", async () => {
    const failing = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    await expect(fetchHealth("http://api.test", failing)).rejects.toThrow(/unreachable/);
  });
});
