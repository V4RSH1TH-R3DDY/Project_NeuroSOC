import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { describeWindow, formatClock } from "../../lib/timeline";

const AXIS = { fill: "#8b95a8", fontSize: 11 };

export default function SeverityTrendChart({ data }) {
  const rows = Array.isArray(data) ? data : [];
  const empty = rows.every((r) => !r.severity);
  const win = describeWindow(rows);

  return (
    <section className="soc-glass p-4">
      <h2 className="soc-section-title">Severity trend</h2>
      <p className="mt-0.5 text-xs text-soc-muted">Last {win.label}, per {win.bucket} (high 3, medium 2, low 1)</p>
      <div className="relative mt-3 h-72">
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
              formatter={(v) => [v, "Severity"]}
            />
            <Area type="monotone" dataKey="severity" stroke="#f26b6b" fill="#f26b6b" fillOpacity={0.2} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
        {empty ? <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-soc-muted">No alerts in this window.</p> : null}
      </div>
    </section>
  );
}
