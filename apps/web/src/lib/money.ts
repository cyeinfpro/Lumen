export function formatRmb(value?: string | number | null, fractionDigits = 2): string {
  // null / undefined / NaN / Infinity from the backend must be surfaced as an
  // explicit error marker so users can distinguish a real zero balance from a
  // data anomaly.  Silently returning "0.00" violates billing transparency.
  if (value === null || value === undefined) return "--";
  if (typeof value === "string" && value.trim() === "") return "--";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "--";
  return amount.toFixed(fractionDigits);
}

const COMPACT_RMB_UNITS = [
  { threshold: 1_000_000_000, suffix: "B" },
  { threshold: 1_000_000, suffix: "M" },
  { threshold: 1_000, suffix: "k" },
] as const;

function formatCompactNumber(value: number): string {
  const rounded = value.toFixed(1);
  return rounded.endsWith(".0") ? rounded.slice(0, -2) : rounded;
}

export function formatRmbCompact(value?: string | number | null): string {
  if (value === null || value === undefined) return "--";
  if (typeof value === "string" && value.trim() === "") return "--";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "--";
  const sign = amount < 0 ? "-" : "";
  const abs = Math.abs(amount);
  if (abs < 1000) return `${sign}${abs.toFixed(2)}`;
  const unit = COMPACT_RMB_UNITS.find((item) => abs >= item.threshold);
  if (!unit) return `${sign}${abs.toFixed(2)}`;
  return `${sign}${formatCompactNumber(abs / unit.threshold)}${unit.suffix}`;
}
