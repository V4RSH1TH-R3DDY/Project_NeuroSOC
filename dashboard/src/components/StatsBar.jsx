import { useMemo } from "react";
import { DEMO_DATA_ENABLED } from "../lib/featureFlags";
import { useDashboardStore } from "../store/dashboardStore";

function StatCard({ label, value, hint, accent }) {
  return (
    <article className="soc-glass px-4 py-3">
      <p className="flex items-center gap-2 text-xs font-medium text-soc-muted">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${accent}`} />
        <span className="truncate">{label}</span>
      </p>
      <p className="soc-tabular mt-2 text-2xl font-semibold leading-none text-soc-text">{value}</p>
      <p className="mt-1.5 truncate text-xs text-soc-muted">{hint}</p>
    </article>
  );
}

export default function StatsBar() {
  const stats = useDashboardStore((state) => state.stats.data);
  const demoAlertCount = useDashboardStore((state) => state.alerts.items.length);

  const cards = useMemo(
    () => [
      {
        label: "Sessions",
        value: Number(stats.totalTransactions || 0).toLocaleString(),
        hint: "Scored by the engine",
        accent: "bg-soc-electric",
      },
      {
        label: "Threats",
        value: Number(stats.hackerDetections || 0).toLocaleString(),
        hint: "Sent to the sandbox",
        accent: "bg-soc-red",
      },
      {
        label: "Average risk",
        value: `${Number(stats.avgRiskScore || 0).toFixed(1)}%`,
        hint: "Mean across sessions",
        accent: "bg-soc-amber",
      },
      {
        label: "Events",
        value: Number(DEMO_DATA_ENABLED ? demoAlertCount : stats.liveAlerts || 0).toLocaleString(),
        hint: DEMO_DATA_ENABLED ? "Seeded examples plus generated stream" : "Available for investigation",
        accent: "bg-soc-green",
      },
    ],
    [stats, demoAlertCount]
  );

  return (
    <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {cards.map((card) => (
        <StatCard key={card.label} {...card} />
      ))}
    </section>
  );
}
