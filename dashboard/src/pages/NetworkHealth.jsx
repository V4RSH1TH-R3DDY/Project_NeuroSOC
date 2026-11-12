import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import usePolling from "../hooks/usePolling";
import { applyFix, fetchDevice, fetchOverview, injectScenario, resetFleet } from "../services/nethealthApi";

const POLL_MS = 3000; // two requests per poll; the API allows 120 requests a minute per client

const STATUS = {
  healthy: { label: "Healthy", dot: "bg-soc-green", text: "text-soc-green", chip: "border-soc-green/40 bg-soc-green/10 text-soc-green" },
  watch: { label: "Watch", dot: "bg-soc-electric", text: "text-soc-electric", chip: "border-soc-electric/40 bg-soc-electric/10 text-soc-electric" },
  warning: { label: "Warning", dot: "bg-soc-amber", text: "text-soc-amber", chip: "border-soc-amber/40 bg-soc-amber/10 text-soc-amber" },
  critical: { label: "Critical", dot: "bg-soc-red", text: "text-soc-red", chip: "border-soc-red/40 bg-soc-red/10 text-soc-red" },
  down: { label: "Down", dot: "bg-soc-red", text: "text-soc-red", chip: "border-soc-red/60 bg-soc-red/20 text-soc-red" },
};
const PRIORITY = { 1: "Urgent", 2: "Soon", 3: "Routine" };
const PRIORITY_TONE = { 1: "text-soc-red", 2: "text-soc-amber", 3: "text-soc-muted" };
const AXIS = { fill: "#8b95a8", fontSize: 11 };

const statusOf = (key) => STATUS[key] || STATUS.watch;
const riskColor = (r) => (r >= 75 ? "bg-soc-red" : r >= 50 ? "bg-soc-amber" : r >= 25 ? "bg-soc-electric" : "bg-soc-green");
const kind = (d) => d.type.replace("_", " ");

function eta(min) {
  if (min == null) return "";
  if (min <= 0) return "now";
  return min < 90 ? `${Math.round(min)} min` : `${(min / 60).toFixed(1)} h`;
}

// Simulated devices run on a faster clock than the wall, so times are shown relative to the device's latest sample.
function ago(asOf, t) {
  const min = Math.round((asOf - t) / 60);
  return min <= 0 ? "now" : min < 90 ? `${min} min ago` : `${(min / 60).toFixed(1)} h ago`;
}

function Chip({ status }) {
  const s = statusOf(status);
  return <span className={`inline-flex shrink-0 items-center rounded-md border px-2 py-0.5 text-[11px] font-medium ${s.chip}`}>{s.label}</span>;
}

function Tag({ children, tone = "text-soc-muted" }) {
  return <span className={`rounded border border-soc-border px-1.5 py-px text-[10px] ${tone}`}>{children}</span>;
}

function SourceTag({ device }) {
  if (device.stale) return <Tag tone="text-soc-amber">No data</Tag>;
  return device.source === "live" ? <Tag tone="text-soc-green">Live</Tag> : <Tag>Simulated</Tag>;
}

function Tile({ label, value, hint, tone = "text-soc-text" }) {
  return (
    <div className="soc-glass px-4 py-3">
      <p className="text-xs font-medium text-soc-muted">{label}</p>
      <p className={`soc-tabular mt-1.5 text-2xl font-semibold leading-none ${tone}`}>{value}</p>
      <p className="mt-1.5 truncate text-xs text-soc-muted">{hint}</p>
    </div>
  );
}

function MetricChart({ detail, metric }) {
  const series = detail.series?.[metric] || [];
  const th = detail.thresholds?.[metric];
  if (series.length < 2 || !th) return <p className="flex h-56 items-center justify-center text-sm text-soc-muted">Not enough data yet.</p>;
  const asOf = detail.as_of ?? series[series.length - 1].t;
  const data = series.map((p) => ({ t: p.t, v: p.v }));
  const peak = Math.max(th.crit * 1.1, ...series.map((p) => p.v)) || 1;
  const top = th.unit === "%" ? Math.min(100, Math.ceil(peak)) : Math.ceil(peak);
  const fmt = (v) => `${v}${th.unit}`;
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#252f42" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(t) => ago(asOf, t)} tick={AXIS} tickLine={false} axisLine={false} minTickGap={56} />
          <YAxis domain={[0, top]} tick={AXIS} tickLine={false} axisLine={false} width={44} tickFormatter={(v) => (th.unit === "%" ? `${v}%` : v)} />
          <Tooltip
            contentStyle={{ background: "#111723", border: "1px solid #252f42", borderRadius: 8, fontSize: 12, color: "#e8edf5" }}
            labelStyle={{ color: "#8b95a8" }}
            labelFormatter={(t) => ago(asOf, t)}
            formatter={(v) => [fmt(v), th.label]}
          />
          <ReferenceLine y={th.warn} stroke="#f0b44c" strokeDasharray="4 4" />
          <ReferenceLine y={th.crit} stroke="#f26b6b" strokeDasharray="4 4" />
          <Line type="monotone" dataKey="v" stroke="#4f8cff" strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function ActionRow({ action, onFix, busy, showDevice = false, onOpen }) {
  return (
    <li className="soc-inset flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <p className="text-sm font-medium text-soc-text">
          <span className={`mr-2 text-[11px] font-semibold uppercase ${PRIORITY_TONE[action.priority]}`}>{PRIORITY[action.priority]}</span>
          {action.title}
        </p>
        <p className="mt-0.5 text-xs text-soc-muted">{action.detail}</p>
        {showDevice ? (
          <button type="button" onClick={() => onOpen(action.device_id)} className="mt-1 text-xs text-soc-electric hover:underline">
            {action.device_name}
          </button>
        ) : null}
      </div>
      {action.fix && onFix ? (
        <button type="button" disabled={busy} onClick={() => onFix(action.device_id, action.fix)} className="soc-btn shrink-0">
          Apply fix
        </button>
      ) : null}
    </li>
  );
}

function DeviceDetail({ deviceId, busy, onFix }) {
  const [detail, setDetail] = useState(null);
  const [metric, setMetric] = useState(null);
  const load = useCallback(() => {
    fetchDevice(deviceId).then(setDetail).catch(() => {});
  }, [deviceId]);
  usePolling(load, POLL_MS);
  useEffect(() => {
    setDetail(null);
    setMetric(null);
  }, [deviceId]);

  if (!detail) return <div className="soc-glass p-6 text-sm text-soc-muted">Loading…</div>;

  const metrics = Object.keys(detail.series || {});
  const active = metric && metrics.includes(metric) ? metric : detail.signals?.[0]?.metric || metrics[0];
  const p = detail.failure_probability_30m;
  const pct = p == null || detail.status === "down" ? null : Math.round(p * 100);
  const t = detail.threat && detail.threat.pressure >= 10 ? detail.threat : null;

  return (
    <div className="soc-glass space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-base font-semibold text-soc-text">{detail.name}</h2>
          <p className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-soc-muted">
            {kind(detail)} · {detail.site} <SourceTag device={detail} />
          </p>
        </div>
        <div className="flex items-center gap-4 text-right">
          <div>
            <p className="text-[11px] text-soc-muted">Risk</p>
            <p className="soc-tabular text-lg font-semibold text-soc-text">{detail.risk}</p>
          </div>
          {pct != null ? (
            <div title="Learned model: chance of failure within 30 minutes">
              <p className="text-[11px] text-soc-muted">AI, 30 min</p>
              <p className={`soc-tabular text-lg font-semibold ${pct >= 70 ? "text-soc-red" : pct >= 35 ? "text-soc-amber" : "text-soc-green"}`}>{pct}%</p>
            </div>
          ) : null}
          <Chip status={detail.status} />
        </div>
      </div>

      {t ? (
        <p className="rounded-lg border border-soc-red/40 bg-soc-red/10 px-3 py-2 text-sm text-soc-text">
          <span className="font-semibold text-soc-red">Attack traffic. </span>
          Classified as {(t.attack_class || "hostile").replace(/_/g, " ").toLowerCase()} ({Math.round((t.confidence || 0) * 100)}% confidence), pressure {Math.round(t.pressure)}/100.
        </p>
      ) : null}

      {metrics.length ? (
        <div>
          <div className="mb-2 flex flex-wrap gap-1" role="tablist" aria-label="Metric">
            {metrics.map((m) => (
              <button
                key={m}
                type="button"
                role="tab"
                aria-selected={m === active}
                onClick={() => setMetric(m)}
                className={`rounded-md border px-2.5 py-1 text-xs ${m === active ? "border-soc-electric bg-soc-electric/10 text-soc-text" : "border-soc-border text-soc-muted hover:text-soc-text"}`}
              >
                {detail.thresholds[m].label}
              </button>
            ))}
          </div>
          <MetricChart detail={detail} metric={active} />
          <p className="mt-1 flex gap-4 text-[11px] text-soc-muted">
            <span><span className="text-soc-amber">- -</span> warning</span>
            <span><span className="text-soc-red">- -</span> critical</span>
          </p>
        </div>
      ) : null}

      <div>
        <p className="soc-kicker mb-2">Why</p>
        {detail.signals?.length || detail.event_context?.length ? (
          <ul className="space-y-1.5 text-sm text-soc-text">
            {detail.signals.map((s) => (
              <li key={s.metric} className="soc-inset px-3 py-2">{s.message}</li>
            ))}
            {detail.event_context?.map((c) => (
              <li key={c.category} className="soc-inset px-3 py-2 text-soc-muted">
                {c.label} ×{c.count} in device logs{detail.corroborated_by_events?.includes(c.metric) ? ", supports the trend" : ""}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-soc-muted">All metrics are in their normal range.</p>
        )}
      </div>

      {detail.recommendations?.length ? (
        <div>
          <p className="soc-kicker mb-2">Actions</p>
          <ul className="space-y-2">
            {detail.recommendations.map((r) => (
              <ActionRow key={r.id} action={r} onFix={onFix} busy={busy} />
            ))}
          </ul>
        </div>
      ) : null}

      <div>
        <p className="soc-kicker mb-2">Recent events</p>
        {detail.events?.length ? (
          <ul className="soc-scroll max-h-36 space-y-1 text-xs">
            {detail.events.map((e) => (
              <li key={e.id} className="flex gap-2">
                <span className="soc-tabular w-16 shrink-0 text-soc-muted">{ago(detail.as_of ?? e.ts, e.ts)}</span>
                <span className={`w-14 shrink-0 ${e.severity === "critical" || e.severity === "error" ? "text-soc-red" : e.severity === "warning" ? "text-soc-amber" : "text-soc-muted"}`}>{e.severity}</span>
                <span className="min-w-0 text-soc-text">{e.message}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-soc-muted">None.</p>
        )}
      </div>
    </div>
  );
}

function FaultInjector({ data, busy, run, onInjected }) {
  const [scenario, setScenario] = useState("memory_leak");
  const [target, setTarget] = useState("");
  const info = data.scenarios.find((s) => s.key === scenario);
  const eligible = useMemo(
    () => data.devices.filter((d) => !info || info.types.includes(d.type)).sort((a, b) => a.name.localeCompare(b.name)),
    [data.devices, info]
  );
  const chosen = eligible.some((d) => d.id === target) ? target : eligible[0]?.id || "";
  const selectCls = "mt-1 block w-full rounded-lg border border-soc-border bg-soc-panelSoft px-2 py-1.5 text-sm text-soc-text";

  return (
    <div className="soc-glass flex flex-wrap items-end gap-2 p-3" aria-label="Fault injection">
      <label className="min-w-[10rem] flex-1 text-xs text-soc-muted">
        Fault
        <select value={scenario} onChange={(e) => setScenario(e.target.value)} className={selectCls}>
          {data.scenarios.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
      </label>
      <label className="min-w-[10rem] flex-1 text-xs text-soc-muted">
        Device
        <select value={chosen} onChange={(e) => setTarget(e.target.value)} className={selectCls}>
          {eligible.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </label>
      <button type="button" disabled={busy || !chosen} onClick={() => run(() => injectScenario(scenario, chosen).then(() => onInjected(chosen)), "Fault injected.")} className="soc-btn-danger">
        Inject
      </button>
      <button type="button" disabled={busy} onClick={() => run(resetFleet, "Fleet reset.")} className="soc-btn">
        Reset
      </button>
    </div>
  );
}

export default function NetworkHealthPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [view, setView] = useState("devices");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const load = useCallback(() => {
    fetchOverview()
      .then((d) => { if (mounted.current) { setData(d); setError(""); } })
      .catch((e) => { if (mounted.current) setError(e?.response?.data?.detail || e.message || "Could not reach the API."); });
  }, []);
  usePolling(load, POLL_MS);

  async function run(action, okMessage) {
    setBusy(true);
    setNotice("");
    try {
      await action();
      setNotice(okMessage);
      load();
    } catch (e) {
      setNotice(e?.response?.data?.detail || e.message || "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !data) {
    return (
      <div className="soc-glass p-6">
        <h1 className="soc-title">Network health</h1>
        <p className="mt-2 text-sm text-soc-muted">{error}</p>
        <p className="mt-1 text-xs text-soc-muted">The API must run with ENABLE_NETHEALTH=true.</p>
      </div>
    );
  }
  if (!data) return <p className="py-16 text-center text-sm text-soc-muted">Loading…</p>;

  const next = data.predictions[0];
  const atRisk = data.total - data.counts.healthy;
  const focus = selected && data.devices.some((d) => d.id === selected) ? selected : data.devices[0]?.id;
  const open = (id) => { setSelected(id); setView("devices"); };
  const fix = (id, f) => run(() => applyFix(id, f), "Fix applied. Metrics will recover.");

  return (
    <div className="space-y-4">
      <header className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="soc-title">Network health</h1>
          <p className="mt-1 text-sm text-soc-muted">
            Predicts device failures from trends, anomalies, logs and attack traffic.
            {data.learned_model ? ` Learned model AUROC ${data.learned_model.auroc ?? "n/a"}.` : ""}
          </p>
        </div>
        {data.simulated ? <FaultInjector data={data} busy={busy} run={run} onInjected={setSelected} /> : null}
      </header>
      {notice ? <p role="status" className="text-xs text-soc-muted">{notice}</p> : null}

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="Summary">
        <Tile label="Fleet risk" value={data.fleet_risk} hint="0 calm, 100 failing" tone={data.fleet_risk >= 50 ? "text-soc-red" : data.fleet_risk >= 25 ? "text-soc-amber" : "text-soc-green"} />
        <Tile label="At risk" value={`${atRisk} / ${data.total}`} hint={`${data.counts.down} down`} tone={atRisk ? "text-soc-amber" : "text-soc-green"} />
        <Tile label="Next failure" value={next ? eta(next.eta_min) : "None"} hint={next ? next.device_name : "Nothing predicted"} tone={next ? "text-soc-red" : "text-soc-green"} />
        <Tile label="Open actions" value={data.recommendations.length} hint="Recommended fixes" />
      </section>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <section className="soc-glass flex min-h-0 flex-col p-3" aria-label="Devices and actions">
          <div className="mb-3 inline-flex self-start rounded-lg border border-soc-border bg-soc-panelSoft p-0.5" role="tablist">
            {[["devices", `Devices (${data.total})`], ["actions", `Actions (${data.recommendations.length})`]].map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={view === key}
                onClick={() => setView(key)}
                className={`rounded-md px-3 py-1 text-xs font-medium ${view === key ? "bg-soc-panel text-soc-text" : "text-soc-muted hover:text-soc-text"}`}
              >
                {label}
              </button>
            ))}
          </div>

          {view === "devices" ? (
            <ul className="soc-scroll max-h-[28rem] space-y-1.5 lg:max-h-[calc(100vh-22rem)]">
              {data.devices.map((d) => (
                <li key={d.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(d.id)}
                    aria-pressed={d.id === focus}
                    className={`w-full rounded-lg border p-2.5 text-left transition-colors ${d.id === focus ? "border-soc-electric bg-soc-electric/5" : "border-soc-border bg-soc-panelSoft/40 hover:bg-soc-panelSoft"}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-2">
                        <span className={`h-2 w-2 shrink-0 rounded-full ${statusOf(d.status).dot}`} />
                        <span className="truncate text-sm font-medium text-soc-text">{d.name}</span>
                      </span>
                      <span className={`shrink-0 text-xs ${statusOf(d.status).text}`}>
                        {d.status === "down" ? "Down" : d.predicted_failure_in_min != null && d.status !== "healthy" ? `~${eta(d.predicted_failure_in_min)}` : statusOf(d.status).label}
                      </span>
                    </div>
                    <div className="mt-1.5 flex items-center gap-2">
                      <span className="text-[11px] text-soc-muted">{kind(d)} · {d.site}</span>
                      <SourceTag device={d} />
                    </div>
                    <div className="mt-2 h-1 overflow-hidden rounded-full bg-soc-border" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={d.risk} aria-label="Failure risk">
                      <div className={`h-full rounded-full ${riskColor(d.risk)}`} style={{ width: `${Math.max(3, d.risk)}%` }} />
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <ul className="soc-scroll max-h-[28rem] space-y-2 lg:max-h-[calc(100vh-22rem)]">
              {data.recommendations.length === 0 ? <li className="py-6 text-center text-sm text-soc-muted">Nothing to do.</li> : null}
              {data.recommendations.map((r) => (
                <ActionRow key={r.id} action={r} showDevice onOpen={open} />
              ))}
            </ul>
          )}
        </section>

        <div className="min-w-0">
          {focus ? <DeviceDetail deviceId={focus} busy={busy} onFix={fix} /> : null}
        </div>
      </div>
    </div>
  );
}
