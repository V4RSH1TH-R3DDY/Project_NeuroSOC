import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import AlertFeed from "../components/AlertFeed";
import VerdictTimeline from "../components/Charts/VerdictTimeline";
import IngestionWorkbench from "../components/IngestionWorkbench";
import ModelCandidatesPanel from "../components/ModelCandidatesPanel";
import ModelStatusCard from "../components/ModelStatusCard";
import PageTabs from "../components/PageTabs";
import StatsBar from "../components/StatsBar";
import ThreatMap from "../components/ThreatMap";
import UserProfileModal from "../components/UserProfileModal";
import { MODEL_INTEGRATION_ENABLED } from "../lib/featureFlags";
import { buildTimeline } from "../lib/timeline";
import { useDashboardStore } from "../store/dashboardStore";

export default function DashboardPage() {
  const alerts = useDashboardStore((s) => s.alerts.items);
  const statsError = useDashboardStore((s) => s.stats.error);
  const modelError = useDashboardStore((s) => s.modelStatus.error);
  const [searchParams, setSearchParams] = useSearchParams();

  const tabs = [
    { key: "overview", label: "Activity" },
    { key: "threat-map", label: "Threat map" },
    ...(MODEL_INTEGRATION_ENABLED ? [{ key: "model-health", label: "Model health" }] : []),
    { key: "pipeline", label: "Pipeline" },
  ];
  const requested = searchParams.get("view");
  const activeTab = tabs.some((t) => t.key === requested) ? requested : "overview";

  function setActiveTab(view) {
    const next = new URLSearchParams(searchParams);
    if (view === "overview") next.delete("view");
    else next.set("view", view);
    setSearchParams(next);
  }

  // The chart window slides forward with the clock, not only when a new alert arrives.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 30_000);
    return () => clearInterval(id);
  }, []);
  const timeline = useMemo(() => buildTimeline(alerts), [alerts, tick]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-4">
      <header>
        <h1 className="soc-title">Overview</h1>
        <p className="mt-1 text-sm text-soc-muted">Live verdicts, threat origins and model status.</p>
      </header>

      {statsError || modelError ? (
        <p role="alert" className="rounded-lg border border-soc-red/40 bg-soc-red/10 px-4 py-2 text-sm text-soc-red">{statsError || modelError}</p>
      ) : null}

      <StatsBar />

      <PageTabs tabs={tabs} activeTab={activeTab} onChange={setActiveTab} idPrefix="overview" label="Overview sections" />

      <section id="overview-panel" role="tabpanel" aria-labelledby={`overview-tab-${activeTab}`} className="space-y-4">
        {activeTab === "overview" ? (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
            <VerdictTimeline data={timeline} />
            <AlertFeed />
          </div>
        ) : null}
        {activeTab === "threat-map" ? <ThreatMap /> : null}
        {activeTab === "model-health" && MODEL_INTEGRATION_ENABLED ? (
          <div className="grid gap-4 lg:grid-cols-2">
            <ModelStatusCard />
            <ModelCandidatesPanel />
          </div>
        ) : null}
        {activeTab === "pipeline" ? (
          <div className="overflow-x-auto">
            <div className="min-w-[1180px]">
              <IngestionWorkbench />
            </div>
          </div>
        ) : null}
      </section>

      <UserProfileModal />
    </div>
  );
}
