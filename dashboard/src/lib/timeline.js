// Buckets alerts over time, zero-filled so a chart always has a real x-axis.
// The window is the last hour, widened (up to 24h, with coarser buckets) when the newest alerts are older than that,
// so the charts show recent history instead of an empty hour.
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const SEVERITY_WEIGHT = { high: 3, medium: 2, low: 1 };

export function buildTimeline(alerts, { now = Date.now() } = {}) {
  const times = alerts.map((a) => new Date(a.timestamp).getTime()).filter(Number.isFinite);
  const newest = times.length ? Math.max(...times) : now;
  const oldest = times.length ? Math.min(...times) : now;
  const end = Math.max(now, newest);
  const span = Math.min(24 * HOUR, Math.max(HOUR, end - oldest + 5 * MINUTE));
  const bucket = span <= 2 * HOUR ? MINUTE : span <= 12 * HOUR ? 5 * MINUTE : 15 * MINUTE;
  const last = Math.floor(end / bucket) * bucket;
  const count = Math.ceil(span / bucket);
  const start = last - (count - 1) * bucket;

  const rows = Array.from({ length: count }, (_, i) => ({ time: start + i * bucket, HACKER: 0, FORGETFUL_USER: 0, LEGITIMATE: 0, severity: 0 }));
  alerts.forEach((alert) => {
    const t = new Date(alert.timestamp).getTime();
    if (!Number.isFinite(t) || t < start || t >= last + bucket) return;
    const row = rows[Math.floor((t - start) / bucket)];
    if (alert.verdict in row) row[alert.verdict] += 1;
    row.severity += SEVERITY_WEIGHT[alert.severity] || 0;
  });
  rows.bucketMinutes = bucket / MINUTE;
  rows.spanMinutes = Math.round(span / MINUTE);
  return rows;
}

export const formatClock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function describeWindow(rows) {
  const span = rows?.spanMinutes || 60;
  const label = span >= 120 ? `${Math.round(span / 60)} h` : `${span} min`;
  return { label, bucket: rows?.bucketMinutes === 1 ? "minute" : `${rows?.bucketMinutes || 1} min` };
}
