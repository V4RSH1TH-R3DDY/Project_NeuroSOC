import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import AlertFeed from "../components/AlertFeed";
import SeverityTrendChart from "../components/Charts/SeverityTrendChart";
import PageTabs from "../components/PageTabs";
import { buildTimeline } from "../lib/timeline";
import { useDashboardStore } from "../store/dashboardStore";

export default function IntelFeedPage() {
  const alerts = useDashboardStore((state) => state.alerts.items);
  const [filter, setFilter] = useState("all");
  const [searchParams, setSearchParams] = useSearchParams();
  const tabs = [
    { key: "alerts", label: "Alert queue" },
    { key: "trends", label: "Severity trend" },
    { key: "events", label: "Event log" },
  ];
  const requestedTab = searchParams.get("view");
  const activeTab = tabs.some((tab) => tab.key === requestedTab) ? requestedTab : "alerts";

  function setActiveTab(view) {
    const next = new URLSearchParams(searchParams);
    if (view === "alerts") next.delete("view");
    else next.set("view", view);
    setSearchParams(next);
  }

  const filteredAlerts = useMemo(() => {
    const sorted = [...alerts].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
    return filter === "all" ? sorted : sorted.filter((alert) => alert.severity === filter);
  }, [alerts, filter]);

  const trend = useMemo(() => buildTimeline(filteredAlerts), [filteredAlerts]);

  return (
    <div className="space-y-4">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="soc-title">Intel feed</h1>
          <p className="mt-1 text-sm text-soc-muted">Triage alerts by severity.</p>
        </div>
        <div className="inline-flex self-start rounded-lg border border-soc-border bg-soc-panel p-0.5" role="group" aria-label="Filter alerts by severity">
          {["all", "high", "medium", "low"].map((level) => (
            <button
              type="button"
              key={level}
              onClick={() => setFilter(level)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium capitalize transition-colors ${
                filter === level ? "bg-soc-panelSoft text-soc-text" : "text-soc-muted hover:text-soc-text"
              }`}
            >
              {level}
            </button>
          ))}
        </div>
      </header>

      <PageTabs tabs={tabs} activeTab={activeTab} onChange={setActiveTab} idPrefix="intel" label="Intel feed sections" />

      <section id="intel-panel" role="tabpanel" aria-labelledby={`intel-tab-${activeTab}`} tabIndex={0} className="focus:outline-none">
        {activeTab === "alerts" ? <AlertFeed items={filteredAlerts} tall /> : null}

        {activeTab === "trends" ? (
          <SeverityTrendChart data={trend} />
        ) : null}

        {activeTab === "events" ? (
          <section className="soc-glass overflow-hidden">
            <div className="border-b border-soc-border px-4 py-3">
              <h2 className="soc-section-title">Latest entries</h2>
            </div>
            <div className="soc-scroll max-h-[32rem] overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="sticky top-0 bg-soc-panelSoft text-xs text-soc-muted">
                  <tr>
                    <th className="px-4 py-2 font-medium">Time</th>
                    <th className="px-4 py-2 font-medium">Severity</th>
                    <th className="px-4 py-2 font-medium">Message</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-soc-border/60">
                  {filteredAlerts.length === 0 ? (
                    <tr>
                      <td colSpan={3} className="px-4 py-6 text-center text-soc-muted">No entries match this filter.</td>
                    </tr>
                  ) : null}
                  {filteredAlerts.slice(0, 50).map((alert) => (
                    <tr key={alert.id} className="hover:bg-soc-panelSoft/40">
                      <td className="soc-tabular whitespace-nowrap px-4 py-2 font-mono text-xs text-soc-muted">
                        {new Date(alert.timestamp).toLocaleTimeString()}
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center gap-1.5 text-xs capitalize ${
                            alert.severity === "high" ? "text-soc-red" : alert.severity === "medium" ? "text-soc-amber" : "text-soc-muted"
                          }`}
                        >
                          <span className="h-1.5 w-1.5 rounded-full bg-current" />
                          {alert.severity}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-soc-text">{alert.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ) : null}
      </section>
    </div>
  );
}
