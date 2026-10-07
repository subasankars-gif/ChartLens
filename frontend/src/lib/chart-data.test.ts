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

describe("an explanation is shown only with the document it is bound to (ADR-0028 §6)", () => {
  const base = {
    meta_version: "meta-a",
    weekly: { meta_version: "meta-a" },
    analysis: { envelope: { meta_version: "meta-a", document_sha256: "d1" } },
  };
  it("accepts a bound explanation from the chart's snapshot", () => {
    const chart = { ...base, explanations: { envelope: { meta_version: "meta-a", document_sha256: "d1" } } };
    expect(() => checkChart(chart as unknown as ChartResponse)).not.toThrow();
  });
  it("refuses one bound to another document or from another snapshot", () => {
    const other = { ...base, explanations: { envelope: { meta_version: "meta-a", document_sha256: "d2" } } };
    expect(() => checkChart(other as unknown as ChartResponse)).toThrow(/not bound/);
    const stale = { ...base, explanations: { envelope: { meta_version: "meta-b", document_sha256: "d1" } } };
    expect(() => checkChart(stale as unknown as ChartResponse)).toThrow(SnapshotChanged);
  });
});

