import { describe, expect, it } from "vitest";
import type { ChartResponse } from "./api";
import { SnapshotChanged, acceptFrom, checkChart } from "./chart-data";

describe("one chart, one snapshot (ADR-0027 §3)", () => {
  it("accepts a component only from the chart's own snapshot", () => {
    expect(() => acceptFrom("meta-a", "meta-a")).not.toThrow();
    expect(() => acceptFrom("meta-a", "meta-b")).toThrow(SnapshotChanged);
  });

  it("refuses a /chart response whose components name different snapshots", () => {
    const chart = {
      meta_version: "meta-a",
      weekly: { meta_version: "meta-a" },
      analysis: { envelope: { meta_version: "meta-b" } },
    } as unknown as ChartResponse;
    expect(() => checkChart(chart)).toThrow(SnapshotChanged);
    expect(() => checkChart({ ...chart, analysis: null })).not.toThrow();
  });
});
