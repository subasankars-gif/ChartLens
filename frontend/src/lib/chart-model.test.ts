import { describe, expect, it } from "vitest";
import type { WeeklyBar } from "./api";
import { buildChartModel, RANGES, visibleRange, type Palette } from "./chart-model";

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


describe("the view never removes history (Issue 1)", () => {
  // 1,084 weekly bars: a 20-year history like TCS in production.
  const long = Array.from({ length: 1084 }, (_, i) => {
    const d = new Date(Date.UTC(2006, 0, 2 + 7 * i)).toISOString().slice(0, 10);
    return bar({ week_start_date: d, first_session_date: d, last_session_date: d, week_end_date: d });
  });

  it("the chart holds every bar the API returned, whatever the view", () => {
    const model = buildChartModel(long, "S@2006-01-02", new Map(), P);
    expect(model.series.reduce((n, s) => n + s.candles.length, 0)).toBe(long.length);
    expect(model.barsByTime.size).toBe(long.length);
  });

  it("opens on the last three years, and every range stays inside the bars", () => {
    expect(visibleRange(1084, undefined)).toEqual({ from: 1084 - 156, to: 1088 });
    for (const r of RANGES) {
      const v = visibleRange(1084, r.weeks)!;
      expect(v.from).toBeGreaterThanOrEqual(0);
      expect(v.to).toBe(1088);
    }
    expect(visibleRange(1084, null)).toEqual({ from: 0, to: 1088 }); // "All" reaches the first bar
  });

  it("a younger security shows exactly the history it has", () => {
    expect(visibleRange(45, undefined)).toBeNull(); // fit: all 45 bars in view
    expect(visibleRange(45, 156)).toEqual({ from: 0, to: 49 });
    expect(visibleRange(0, null)).toBeNull();
  });

  it("with earlier segments shown, the view reaches back to a year before the last break", () => {
    expect(visibleRange(1085, undefined, 1040)).toEqual({ from: 929, to: 1089 }); // the 3-year view already shows it
    expect(visibleRange(1085, undefined, 500)).toEqual({ from: 448, to: 1089 }); // an older break pulls the view back
  });
});
