/**
 * Display helpers. Prices and volumes arrive as exact decimal strings and are shown from
 * those strings: no binary-float round trip, no rounding (ADR-0017).
 */

/** "1269.375000" → "1,269.375"; "1500.000000" → "1,500.00". Grouping in the Indian style. */
export function formatDecimal(text: string, minFraction = 2): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text.trim());
  if (!match) return text;
  const [, sign, whole = "0", fraction = ""] = match;
  let frac = fraction.replace(/0+$/, "");
  if (frac.length < minFraction) frac = frac.padEnd(minFraction, "0");
  return `${sign}${groupIndian(whole)}${frac ? `.${frac}` : ""}`;
}

/** Volumes: "33852768.0000" → "3,38,52,768"; fractional volumes keep their digits. */
export function formatQuantity(text: string): string {
  return formatDecimal(text, 0);
}

function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${rest},${last3}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2023-07-20" → "20 Jul 2023" (dates are calendar dates, never shifted by time zones). */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
}

const SESSION_TYPE_LABEL: Record<string, string> = {
  MUHURAT: "Muhurat session",
  BUDGET: "Budget-day session",
  DR_DRILL: "DR drill session",
  OTHER: "Special session",
};

export function sessionTypeLabel(type: string | null | undefined): string {
  return type ? (SESSION_TYPE_LABEL[type] ?? type) : "Regular session";
}

export function causeLabel(cause: string): string {
  const labels: Record<string, string> = {
    FIRST_SESSION: "First session",
    UNQUANTIFIED_ACTION: "Unquantified corporate action",
    TRADING_GAP: "Trading gap",
    UNEXPLAINED_PRICE_DISCONTINUITY: "Unexplained price discontinuity",
    FACTOR_REJECTED_BY_PRICE: "Adjustment factor rejected by prices",
    REVIEWED_LINK_PRICE_BREAK: "Reviewed identity link",
    CONFLICTING_ACTION_RECORDS: "Conflicting corporate-action records",
  };
  return cause
    .split("+")
    .map((c) => labels[c] ?? c)
    .join(" + ");
}
