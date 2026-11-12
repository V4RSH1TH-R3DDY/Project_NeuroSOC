import { useMemo } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { describeWindow, formatClock } from "../../lib/timeline";

const SERIES = [
  { key: "HACKER", label: "Threat", color: "#f26b6b" },
  { key: "FORGETFUL_USER", label: "Review", color: "#f0b44c" },
  { key: "LEGITIMATE", label: "Normal", color: "#3fbf8c" },
];
const AXIS = { fill: "#8b95a8", fontSize: 11 };

export default function VerdictTimeline({ data }) {
  const rows = Array.isArray(data) ? data : [];
  const totals = useMemo(
    () => Object.fromEntries(SERIES.map((s) => [s.key, rows.reduce((sum, r) => sum + (r[s.key] || 0), 0)])),
    [rows]
  );
  const empty = SERIES.every((s) => totals[s.key] === 0);
  const win = describeWindow(rows);

  return (
    <section className="soc-glass p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="soc-section-title">Verdicts, last {win.label}</h2>
          <p className="mt-0.5 text-xs text-soc-muted">Per {win.bucket}</p>
        </div>
        <div className="flex gap-4">
          {SERIES.map((s) => (
            <div key={s.key}>
              <p className="flex items-center gap-1.5 text-xs text-soc-muted">
                <span className="h-2 w-2 rounded-sm" style={{ background: s.color }} />
                {s.label}
              </p>
              <p className="soc-tabular text-lg font-semibold text-soc-text">{totals[s.key].toLocaleString()}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="relative h-72">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={rows} margin={{ top: 8, right: 20, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#252f42" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="time" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={formatClock} tick={AXIS} tickLine={false} axisLine={false} minTickGap={48} />
            <YAxis allowDecimals={false} tick={AXIS} tickLine={false} axisLine={false} width={36} />
            <Tooltip
              cursor={{ stroke: "#33415c" }}
              contentStyle={{ background: "#111723", border: "1px solid #252f42", borderRadius: 8, color: "#e8edf5", fontSize: 12 }}
              labelStyle={{ color: "#8b95a8" }}
              labelFormatter={formatClock}
              formatter={(v, name) => [v, SERIES.find((s) => s.key === name)?.label || name]}
            />
            {SERIES.map((s) => (
              <Area key={s.key} type="monotone" dataKey={s.key} stroke={s.color} fill={s.color} fillOpacity={0.25} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            ))}
          </AreaChart>
        </ResponsiveContainer>
        {empty ? (
          <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-soc-muted">No verdicts in this window yet.</p>
        ) : null}
      </div>
    </section>
  );
}
