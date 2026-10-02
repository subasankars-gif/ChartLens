import { describe, expect, it } from "vitest";
import { causeLabel, formatDate, formatDecimal, formatQuantity, sessionTypeLabel } from "./format";

describe("formatDecimal", () => {
  it("shows the API's exact digits, trimming only trailing zeros", () => {
    expect(formatDecimal("1269.375000")).toBe("1,269.375");
    expect(formatDecimal("1500.000000")).toBe("1,500.00");
    expect(formatDecimal("0.100000")).toBe("0.10");
    expect(formatDecimal("12345678.123456")).toBe("1,23,45,678.123456");
    // a value binary floats cannot hold exactly is still shown exactly
    expect(formatDecimal("0.300000000000000004")).toBe("0.300000000000000004");
  });

  it("formats volumes as whole numbers when they are", () => {
    expect(formatQuantity("33852768.0000")).toBe("3,38,52,768");
    expect(formatQuantity("1234.5000")).toBe("1,234.5");
  });

  it("formats dates without time-zone shifts", () => {
    expect(formatDate("2023-07-20")).toBe("20 Jul 2023");
    expect(formatDate(null)).toBe("—");
  });

  it("names session types and break causes in plain words", () => {
    expect(sessionTypeLabel("MUHURAT")).toBe("Muhurat session");
    expect(causeLabel("FACTOR_REJECTED_BY_PRICE+TRADING_GAP")).toBe(
      "Adjustment factor rejected by prices + Trading gap",
    );
  });
});
