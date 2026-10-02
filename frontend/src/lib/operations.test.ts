import { describe, expect, it } from "vitest";
import {
  STAGE_ORDER,
  elapsedSeconds,
  formatDuration,
  formatInstant,
  isActive,
  outcomeLabel,
  requesterLabel,
  stageLabel,
} from "./operations";

describe("operations display", () => {
  it("names stages in pipeline order", () => {
    expect(STAGE_ORDER.map(stageLabel)).toEqual([
      "Ingest",
      "Corporate actions",
      "Adjustment",
      "Data quality",
      "Weekly bars",
      "Publish",
    ]);
    expect(stageLabel(null)).toBe("—");
  });

  it("says what a run did to the live snapshot", () => {
    expect(outcomeLabel({ status: "RUNNING", snapshot_outcome: null })).toMatch(/unchanged until/);
    expect(outcomeLabel({ status: "SUCCEEDED", snapshot_outcome: "PUBLISHED" })).toBe("Published a new snapshot");
    expect(outcomeLabel({ status: "SUCCEEDED", snapshot_outcome: "UNCHANGED" })).toMatch(/already current/);
    expect(outcomeLabel({ status: "FAILED", snapshot_outcome: "NOT_PUBLISHED" })).toMatch(/live snapshot is unchanged/);
  });

  it("names who asked", () => {
    expect(requesterLabel({ trigger: "schedule", requested_by: "schedule" })).toBe("Schedule");
    expect(requesterLabel({ trigger: "manual", requested_by: "github:octo" })).toBe("octo on GitHub");
    expect(requesterLabel({ trigger: "api", requested_by: "boss@example.com" })).toBe("boss@example.com");
  });

  it("formats durations and Indian time", () => {
    expect(formatDuration(42)).toBe("42 s");
    expect(formatDuration(200)).toBe("3 min 20 s");
    expect(formatDuration(120)).toBe("2 min");
    expect(formatDuration(3900)).toBe("1 h 5 min");
    expect(formatDuration(null)).toBe("—");
    expect(formatInstant("2026-10-02T14:45:00+00:00")).toBe("2 Oct, 20:15 IST");
    expect(formatInstant(null)).toBe("—");
  });

  it("knows which runs are active and how long they have run", () => {
    expect(["QUEUED", "RUNNING"].every((s) => isActive(s as "QUEUED"))).toBe(true);
    expect(isActive("FAILED")).toBe(false);
    const run = {
      started_at: "2026-10-02T14:45:00Z",
      requested_at: "2026-10-02T14:44:00Z",
    };
    expect(elapsedSeconds(run, Date.parse("2026-10-02T14:46:30Z"))).toBe(90);
  });
});
