import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useDashboardStore } from "../store/dashboardStore";

function markerSize(score) {
  return 14 + Math.round((Number(score) || 0) * 14);
}

function bandClass(score) {
  return `soc-threat-${getRiskBand(score).toLowerCase()}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
}

function buildMarkerIcon(cluster, delay) {
  const size = markerSize(cluster.score);
  const count = cluster.events.length;

  return L.divIcon({
    className: "soc-threat-icon",
    html: `
      <div class="soc-threat-marker ${bandClass(cluster.score)}" style="--size:${size}px;--delay:${delay}s">
        <span class="soc-threat-halo"></span>
        <span class="soc-threat-ring"></span>
        <span class="soc-threat-ring soc-threat-ring-late"></span>
        <span class="soc-threat-core"></span>
        ${count > 1 ? `<span class="soc-threat-count">${count}</span>` : ""}
      </div>
    `,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

function buildTooltip(cluster) {
  const names = [...new Set(cluster.events.map((event) => event.userName).filter(Boolean))];
  const shown = names.slice(0, 3).map(escapeHtml).join(", ");
  const more = names.length > 3 ? ` +${names.length - 3}` : "";

  return `
    <div class="soc-threat-tip">
      <p class="soc-threat-tip-place">${escapeHtml(cluster.label)}</p>
      <p class="soc-threat-tip-meta">${getRiskBand(cluster.score)} · peak ${formatPercent(cluster.score)} · ${cluster.events.length} event${cluster.events.length === 1 ? "" : "s"}</p>
      ${shown ? `<p class="soc-threat-tip-users">${shown}${more}</p>` : ""}
    </div>
  `;
}

// Greedy on-screen clustering: nearby origins merge into one marker at the current zoom.
function clusterByScreenDistance(events, map, radius = 26) {
  const clusters = [];
  [...events]
    .sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0))
    .forEach((event) => {
      const point = map.latLngToLayerPoint([event.lat, event.lng]);
      const cluster = clusters.find((item) => item.point.distanceTo(point) < radius);
      if (cluster) {
        cluster.events.push(event);
        if (!cluster.labels.includes(event.label)) cluster.labels.push(event.label);
        return;
      }
      clusters.push({
        lat: event.lat,
        lng: event.lng,
        point,
        score: Number(event.score) || 0,
        labels: [event.label],
        events: [event],
      });
    });

  return clusters.map((cluster) => ({
    ...cluster,
    label: cluster.labels.length > 1 ? `${cluster.labels[0]} +${cluster.labels.length - 1} nearby` : cluster.labels[0],
  }));
}

function getRiskBand(score) {
  const value = Number(score) || 0;
  if (value >= 0.8) return "Critical";
  if (value >= 0.5) return "Elevated";
  return "Watch";
}

function formatPercent(score) {
  const value = Number(score);
  if (!Number.isFinite(value)) {
    return "0%";
  }

  return `${Math.round(value * 100)}%`;
}

async function geocodeIp(sourceIp) {
  const response = await fetch(
    `http://ip-api.com/json/${sourceIp}?fields=status,message,query,country,city,lat,lon`
  );
  const data = await response.json();

  if (data.status !== "success") {
    throw new Error(data.message || "Geocoding failed");
  }

  return {
    lat: data.lat,
    lng: data.lon,
    label: [data.city, data.country].filter(Boolean).join(", ") || sourceIp,
  };
}

export default function ThreatMap({ compact = false }) {
  const threatEvents = useDashboardStore((state) => state.threatMap.items);
  const mapRef = useRef(null);
  const mapNodeRef = useRef(null);
  const markerLayerRef = useRef(null);
  const mapInitializedRef = useRef(false);
  const fittedRef = useRef(false);
  const geoCacheRef = useRef(new Map());
  const [resolvedEvents, setResolvedEvents] = useState([]);
  const [isResolving, setIsResolving] = useState(false);

  const threatSummary = useMemo(() => {
    if (!threatEvents.length) {
      return "No confirmed threats in the last 24 hours.";
    }

    return `${threatEvents.length} confirmed threat${threatEvents.length === 1 ? "" : "s"} located by source IP.`;
  }, [threatEvents]);

  const legend = useMemo(
    () => [
      { label: "Watch", detail: "Score below 50%", score: 0.3 },
      { label: "Elevated", detail: "Score 50% to 79%", score: 0.65 },
      { label: "Critical", detail: "Score 80% and above", score: 0.92 },
    ],
    []
  );

  useEffect(() => {
    if (!mapNodeRef.current || mapInitializedRef.current) {
      return;
    }

    const map = L.map(mapNodeRef.current, {
      zoomControl: false,
      attributionControl: true,
      scrollWheelZoom: false,
      worldCopyJump: true,
      preferCanvas: true,
      minZoom: 2,
    }).setView([20, 10], 2);

    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
      attribution: "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ",
      className: "soc-map-tiles",
      maxZoom: 16,
    }).addTo(map);

    markerLayerRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;
    mapInitializedRef.current = true;

    const timeoutId = window.setTimeout(() => {
      map.invalidateSize();
    }, 0);

    return () => {
      window.clearTimeout(timeoutId);
      map.remove();
      mapRef.current = null;
      markerLayerRef.current = null;
      mapInitializedRef.current = false;
      fittedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function resolveThreats() {
      if (!threatEvents.length) {
        setResolvedEvents([]);
        setIsResolving(false);
        return;
      }

      setIsResolving(true);

      const nextEvents = await Promise.all(
        threatEvents.map(async (event) => {
          if (!event.sourceIp) {
            return null;
          }

          if (!geoCacheRef.current.has(event.sourceIp)) {
            try {
              geoCacheRef.current.set(event.sourceIp, await geocodeIp(event.sourceIp));
            } catch {
              geoCacheRef.current.set(event.sourceIp, null);
            }
          }

          const geo = geoCacheRef.current.get(event.sourceIp);
          if (!geo) {
            return null;
          }

          return {
            ...event,
            ...geo,
          };
        })
      );

      if (!cancelled) {
        setResolvedEvents(nextEvents.filter(Boolean));
        setIsResolving(false);
      }
    }

    resolveThreats();

    return () => {
      cancelled = true;
    };
  }, [threatEvents]);

  useEffect(() => {
    const map = mapRef.current;
    const layer = markerLayerRef.current;
    if (!map || !layer) {
      return undefined;
    }

    function drawMarkers() {
      layer.clearLayers();
      clusterByScreenDistance(resolvedEvents, map)
        .sort((a, b) => a.score - b.score)
        .forEach((cluster, index) => {
          L.marker([cluster.lat, cluster.lng], {
            icon: buildMarkerIcon(cluster, -((index * 0.7) % 2.8).toFixed(2)),
            keyboard: false,
            zIndexOffset: Math.round(cluster.score * 1000),
          })
            .bindTooltip(buildTooltip(cluster), {
              direction: "top",
              offset: [0, -markerSize(cluster.score) / 2],
              opacity: 1,
              className: "soc-tooltip",
            })
            .addTo(layer);
        });
    }

    if (resolvedEvents.length && !fittedRef.current) {
      fittedRef.current = true;
      const bounds = L.latLngBounds(resolvedEvents.map((event) => [event.lat, event.lng]));
      map.fitBounds(bounds.pad(0.35), { maxZoom: 4, animate: false });
    }

    drawMarkers();
    map.on("zoomend", drawMarkers);

    return () => {
      map.off("zoomend", drawMarkers);
    };
  }, [resolvedEvents]);

  return (
    <section
      className={`soc-glass pointer-events-auto overflow-hidden p-4 ${
        compact ? "h-[390px] w-full" : ""
      }`}
    >
      {compact ? (
        <>
          <div className="mb-3 flex items-start justify-between gap-3">
            <div>
              <p className="soc-kicker">Threat Map</p>
              <p className="mt-2 text-xs text-soc-muted">{threatSummary}</p>
            </div>
            {isResolving ? <span className="text-xs text-soc-muted">Geocoding...</span> : null}
          </div>

          <div className="relative h-[228px] overflow-hidden rounded-xl border border-soc-border/80">
            <div ref={mapNodeRef} className="h-full w-full" />
            {isResolving && resolvedEvents.length === 0 ? (
              <div className="absolute inset-0 flex items-center justify-center bg-soc-panel/60 text-sm text-soc-muted">
                Resolving threat origins...
              </div>
            ) : null}
          </div>
        </>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0">
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <h2 className="soc-section-title">Threat origins</h2>
                <p className="mt-0.5 text-xs text-soc-muted">{threatSummary}</p>
              </div>
              <span className="soc-tabular shrink-0 rounded-md border border-soc-border bg-soc-panelSoft px-2 py-1 text-xs text-soc-muted">
                {isResolving ? "Locating…" : `${resolvedEvents.length} markers`}
              </span>
            </div>
            <div className="relative h-[28rem] overflow-hidden rounded-lg border border-soc-border xl:h-[34rem]">
              <div ref={mapNodeRef} className="h-full w-full" />
              {isResolving && resolvedEvents.length === 0 ? (
                <div className="absolute inset-0 flex items-center justify-center bg-soc-panel/70 text-sm text-soc-muted">Locating threat origins…</div>
              ) : null}
            </div>
          </div>

          <aside className="flex min-h-0 flex-col gap-3 xl:h-[38.5rem]">
            <div className="soc-inset p-3">
              <p className="soc-kicker">Legend</p>
              <ul className="mt-2 space-y-1.5">
                {legend.map((item) => (
                  <li key={item.label} className="flex items-center gap-3">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center">
                      <span className={`soc-threat-marker ${bandClass(item.score)}`} style={{ "--size": `${Math.min(markerSize(item.score), 18)}px` }}>
                        <span className="soc-threat-ring" />
                        <span className="soc-threat-core" />
                      </span>
                    </span>
                    <span className="text-sm text-soc-text">{item.label} <span className="text-xs text-soc-muted">· {item.detail}</span></span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="soc-inset flex min-h-0 flex-1 flex-col p-3">
              <p className="soc-kicker">Recent signals</p>
              <ul className="soc-scroll mt-2 max-h-72 flex-1 space-y-2 xl:max-h-none">
                {resolvedEvents.length === 0 ? <li className="text-sm text-soc-muted">No located markers yet.</li> : null}
                {resolvedEvents.slice(0, 20).map((event) => (
                  <li key={`${event.id}-${event.timestamp}`} className="rounded-md border border-soc-border bg-soc-panel p-2.5">
                    <p className="flex items-center justify-between gap-2 text-xs text-soc-muted">
                      <span className="truncate">{event.userName}</span>
                      <span className="soc-tabular shrink-0">{formatPercent(event.score)}</span>
                    </p>
                    <p className="mt-1 text-sm text-soc-text">{event.label}</p>
                  </li>
                ))}
              </ul>
            </div>
          </aside>
        </div>
      )}
    </section>
  );
}
