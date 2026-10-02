import { describe, expect, it } from "vitest";
import type { WeeklyBar } from "./api";
import { buildChartModel, type Palette } from "./chart-model";

const P: Palette = { up: "U", down: "D", upMuted: "u", downMuted: "d", forming: "F" };

function bar(over: Partial<WeeklyBar>): WeeklyBar {
  return {
    continuity_segment_id: "S@2006-01-02",
    iso_year: 2024,
    iso_week: 1,
    week_start_date: "2024-01-01",
    week_end_date: "2024-01-07",
    first_session_date: "2024-01-01",
    last_session_date: "2024-01-05",
    open: "100.000000",
    high: "105.250000",
    low: "99.000000",
    close: "104.000000",
    volume: "1000.0000",
    raw_close: "104.0000",
    trading_days: 5,
    is_complete: true,
    partial_reason: null,
    special_sessions: 0,
    closes_on_special_session: false,
    closing_session_type: null,
    ...over,
  };
}

const causes = new Map([["S@2023-07-20", "Unquantified corporate action"]]);

describe("buildChartModel", () => {
  it("reconciles one candle per API bar with the API's own values", () => {
    const bars = [bar({}), bar({ last_session_date: "2024-01-12", open: "104.5", close: "101.125" })];
    const m = buildChartModel(bars, "S@2006-01-02", causes, P);
    expect(m.series).toHaveLength(1);
    expect(m.series[0]!.candles.map((c) => [c.time, c.open, c.high, c.low, c.close])).toEqual([
      ["2024-01-05", 100, 105.25, 99, 104],
      ["2024-01-12", 104.5, 105.25, 99, 101.125],
    ]);
    expect(m.barsByTime.get("2024-01-12")?.close).toBe("101.125"); // exact text kept for display
    expect(m.series[0]!.candles.map((c) => c.color)).toEqual(["U", "D"]);
  });

  it("never joins segments: each is its own series with a break band between", () => {
    const bars = [
      bar({ last_session_date: "2023-07-19", partial_reason: "CONTINUITY_BREAK" }),
      bar({
        continuity_segment_id: "S@2023-07-20",
        first_session_date: "2023-07-20",
        last_session_date: "2023-07-21",
        partial_reason: "CONTINUITY_BREAK",
      }),
    ];
    const m = buildChartModel(bars, "S@2023-07-20", causes, P);
    expect(m.series.map((s) => [s.segmentId, s.isCurrent, s.candles.length])).toEqual([
      ["S@2006-01-02", false, 1],
      ["S@2023-07-20", true, 1],
    ]);
    expect(m.series[0]!.candles[0]!.color).toBe("u"); // earlier segment muted
    expect(m.breaks).toEqual([
      { time: "2023-07-21", previousTime: "2023-07-19", cause: "Unquantified corporate action" },
    ]);
    expect(m.markers.filter((k) => k.kind === "partial")).toHaveLength(2);
  });

  it("draws the forming week hollow and marks special-session closes with their type", () => {
    const bars = [
      bar({ closes_on_special_session: true, closing_session_type: "MUHURAT", special_sessions: 1 }),
      bar({ last_session_date: "2024-01-10", is_complete: false }),
    ];
    const m = buildChartModel(bars, "S@2006-01-02", causes, P);
    const [, forming] = m.series[0]!.candles;
    expect([forming!.color, forming!.borderColor]).toEqual(["transparent", "F"]);
    expect(m.markers.map((k) => [k.time, k.kind, k.text])).toEqual([
      ["2024-01-05", "special", "M"],
      ["2024-01-10", "forming", "Forming"],
    ]);
  });

  it("refuses duplicate labels rather than drawing them over each other", () => {
    expect(() => buildChartModel([bar({}), bar({})], "S@2006-01-02", causes, P)).toThrow(/two weekly bars/);
  });
});
