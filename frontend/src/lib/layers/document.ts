/**
 * The stored shapes the layers read, as the engine publishes them (ADR-0024 document
 * schema 1). Only the fields used for drawing and labelling are named; every other
 * field passes through untouched. Integer positions the engine stores (`bar_index`,
 * `anchor_index`, …) are deliberately absent: nothing is placed by array position.
 */

export type Context = { as_of: string; continuity_segment_id: string; security_id: string };

export type Swing = {
  swing_id: string;
  bar_date: string;
  price: number;
  known_at: string | null;
  method: string;
  sensitivity: string;
  type: "HIGH" | "LOW";
  provisional: boolean;
  continuity_segment_id: string;
};

export type SwingsSection = {
  context: Context;
  primary_method: string;
  primary_sensitivity: string;
  swings: Swing[];
};

export type StructureLabel = {
  swing_id: string;
  bar_date: string;
  price: number;
  known_at: string;
  label: string;
  type: "HIGH" | "LOW";
};

export type StructureEvent = {
  event_id: string;
  kind: "BOS" | "CHoCH";
  direction: "UP" | "DOWN";
  bar_date: string;
  level: number;
  known_at: string;
  provisional: boolean;
  continuity_segment_id: string;
};

export type TrendState = { since: string; state: string; provisional: boolean; last_event_id: string | null };

export type StructureSection = {
  context: Context;
  swing_method: string;
  swing_sensitivity: string;
  labels: StructureLabel[];
  events: StructureEvent[];
  trend_history: TrendState[];
};

export type StatusEntry = { date: string; status: string; provisional: boolean };

export type Zone = {
  zone_id: string;
  type: "SUPPORT" | "RESISTANCE";
  price_low: number;
  price_high: number;
  first_seen: string;
  known_at: string;
  last_tested: string | null;
  touches: string[];
  role_reversed: boolean;
  continuity_segment_id: string;
};

export type TrendlineTouch = { swing_id: string; bar_date: string; line_value: number; price: number; known_at: string };

export type Trendline = {
  trendline_id: string;
  type: "SUPPORT" | "RESISTANCE";
  touches: TrendlineTouch[];
  known_at: string;
  status_history: StatusEntry[];
  continuity_segment_id: string;
};

export type ActiveTrendline = { trendline_id: string; value: number };

export type LevelsSection = {
  context: Context;
  state_date: string;
  zones: Zone[];
  trendlines: Trendline[];
  active_trendlines: ActiveTrendline[];
};

export type FibLevel = { ratio: number; price: number; kind: "RETRACEMENT" | "EXTENSION" };

export type FibStructure = {
  fib_id: string;
  direction: "UP" | "DOWN";
  method: string;
  sensitivity: string;
  anchor_bar_date: string;
  anchor_price: number;
  counter_bar_date: string;
  counter_price: number;
  known_at: string;
  levels: FibLevel[];
  status_history: StatusEntry[];
  continuity_segment_id: string;
};

export type FibonacciSection = { context: Context; state_date: string; structures: FibStructure[] };

export type Divergence = {
  divergence_id: string;
  type: string;
  indicator: string;
  date_start: string;
  date_end: string;
  price_1: number;
  price_2: number;
  indicator_1: number;
  indicator_2: number;
  known_at: string;
  status_history: StatusEntry[];
  continuity_segment_id: string;
};

export type EvidenceEvent = {
  event_id: string;
  bar_date: string;
  known_at: string;
  provisional: boolean;
  continuity_segment_id: string;
  kind?: string;
  type?: string;
  description?: string;
};

export type EvidenceSection = {
  divergence: { context: Context; divergences: Divergence[] };
  volume: { context: Context; events: EvidenceEvent[] };
  volatility: { context: Context; events: EvidenceEvent[] };
  candles: { context: Context; events: EvidenceEvent[] };
};

export type KeyPoint = { bar_date: string; price: number; label: string; known_at: string };

export type PatternLine = { label: string; start_date: string; start_value: number; end_date: string; end_value: number };

export type MeasuredMove = { target_low: number; target_high: number; target_calculated_at: string; target_method: string };

export type PatternStatusEntry = {
  status: string;
  effective_date: string;
  known_at: string;
  provisional: boolean;
  reason: string;
  measured_move: MeasuredMove | null;
  evidence_refs: string[];
};

export type FitComponent = { component: string; aspect: string; status: string; score: number | null };

export type Pattern = {
  pattern_id: string;
  pattern_type: string;
  family: string;
  direction: string;
  start_date: string;
  end_date: string;
  known_at: string;
  continuity_segment_id: string;
  geometry: {
    key_points: KeyPoint[];
    lines: PatternLine[];
    confirmation_level: number | null;
    invalidation_level: number | null;
  };
  status_history: PatternStatusEntry[];
  definition_fit: { value: number; components: FitComponent[] };
  context: { evidence_refs?: string[] };
};

export type PatternsSection = { context: Context; patterns: Pattern[] };

export type IndicatorSeries = { name: string; family: string; data: (number | string | null)[] };

export type IndicatorsSection = {
  context: Context;
  bar_dates: string[];
  provisional: boolean[];
  series: IndicatorSeries[];
};

export type CurrentSection = {
  state_date: string;
  zone_ids: string[];
  active_trendline_ids: string[];
  fibonacci_ids: string[];
  included_pattern_ids: string[];
  trend_since: string | null;
};

export type BreakoutFollowUp = { kind: string; effective_date: string; known_at: string; provisional: boolean };

export type BreakoutRow = {
  event_id: string;
  source_id: string;
  source_type: string;
  direction: "BREAKOUT" | "BREAKDOWN";
  bar_date: string;
  known_at: string;
  level_at_break: number;
  provisional: boolean;
  history: BreakoutFollowUp[];
  continuity_segment_id: string;
  pattern_type?: string;
  level_source_type?: string;
};

/** The sections a chart may hold, by name (each optional until requested). */
export type ChartDocument = Partial<{
  identity: { as_of: string; state_date: string; continuity_segment_id: string };
  versions: Record<string, unknown>;
  provenance: unknown[];
  current: CurrentSection;
  indicators: IndicatorsSection;
  swings: SwingsSection;
  structure: StructureSection;
  levels: LevelsSection;
  fibonacci: FibonacciSection;
  evidence: EvidenceSection;
  patterns: PatternsSection;
}>;
