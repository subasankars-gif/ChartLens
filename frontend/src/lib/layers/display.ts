/**
 * Display text for stored analytical numbers. The API serves each number's canonical
 * stored text (ADR-0026 §2.3); rounding to four decimals is a display rule only and never
 * feeds a coordinate.
 */

export function value4(n: number | null | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return n.toFixed(4);
}

/** "STRONG_DOWNTREND" → "strong downtrend". */
export function words(code: string | null | undefined): string {
  return (code ?? "").toLowerCase().replaceAll("_", " ");
}

export function sentence(code: string | null | undefined): string {
  const w = words(code);
  return w.charAt(0).toUpperCase() + w.slice(1);
}
