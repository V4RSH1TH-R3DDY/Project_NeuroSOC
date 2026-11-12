// Data access for the Network Health page (/api/v1/nethealth/*).
import { apiClient } from "../lib/apiClient";

const USE_MOCKS = import.meta.env.VITE_USE_MOCKS === "true";
const NO_MOCKS = "Network health needs the live API; demo-data mode has no simulated fleet.";

async function live(request) {
  if (USE_MOCKS) throw new Error(NO_MOCKS);
  const { data } = await request();
  return data;
}

export const fetchOverview = () => live(() => apiClient.get("/api/v1/nethealth/overview"));
export const fetchDevice = (id) => live(() => apiClient.get(`/api/v1/nethealth/devices/${encodeURIComponent(id)}`));
export const fetchEvents = (limit = 50) => live(() => apiClient.get("/api/v1/nethealth/events", { params: { limit } }));
export const injectScenario = (scenario, deviceId) =>
  live(() => apiClient.post("/api/v1/nethealth/simulate", { scenario, device_id: deviceId }));
export const applyFix = (deviceId, fix) =>
  live(() => apiClient.post(`/api/v1/nethealth/devices/${encodeURIComponent(deviceId)}/fix`, { fix }));
export const resetFleet = () => live(() => apiClient.post("/api/v1/nethealth/reset"));
