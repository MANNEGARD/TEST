"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, buildUrl } from "../api";
import { DEMO_EXPORT_FILE } from "../demo-hook";
import type { ExportChecks, ExportJob, ExportPreview, ExportRange, MonthClose, MonthStatus } from "../exports-types";
import { useVehicle } from "../vehicle";
import { qk } from "./keys";

/** Nycklarna ligger i keys.ts (qk.exports); invalidateJournalData() invaliderar även ["exports"]. */
const exportKeys = qk.exports;

/**
 * Urval av resor ("Exportera valda" i körjournalen) i stället för en period. Nycklarna ligger
 * under qk.exports.all så att invalidateJournalData() även träffar urvalet.
 */
function selectionKey(part: "preview" | "checks", vehicleId: string | undefined, tripIds: string[]) {
  return [...exportKeys.all, "selection", part, vehicleId, tripIds.join(",")] as const;
}

function scopeQuery(range: ExportRange | null, tripIds: string[] | null | undefined, vehicleId: string | undefined) {
  return tripIds ? { trip_ids: tripIds, vehicle_id: vehicleId } : { from: range?.from, to: range?.to, vehicle_id: vehicleId };
}

/** Exakt de rader och summor som Excel-filen kommer att innehålla – för en period eller ett urval. */
export function useExportPreview(range: ExportRange | null, tripIds?: string[] | null) {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: tripIds ? selectionKey("preview", vehicleId, tripIds) : exportKeys.preview(vehicleId, range ?? { from: "", to: "" }),
    queryFn: ({ signal }) => api.get<ExportPreview>("/exports/preview", { query: scopeQuery(range, tripIds, vehicleId), signal }),
    enabled: !!vehicleId && (tripIds ? tripIds.length > 0 : !!range),
    placeholderData: keepPreviousData,
  });
}

export function useExportChecks(range: ExportRange | null, tripIds?: string[] | null) {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: tripIds ? selectionKey("checks", vehicleId, tripIds) : exportKeys.checks(vehicleId, range ?? { from: "", to: "" }),
    queryFn: ({ signal }) => api.get<ExportChecks>("/exports/checks", { query: scopeQuery(range, tripIds, vehicleId), signal }),
    enabled: !!vehicleId && (tripIds ? tripIds.length > 0 : !!range),
  });
}

/** Tidigare skapade filer för det valda fordonet (nyast först). */
export function useExports() {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: exportKeys.list(vehicleId),
    queryFn: ({ signal }) => api.get<ExportJob[]>("/exports", { query: { vehicle_id: vehicleId }, signal }),
    enabled: !!vehicleId,
  });
}

/**
 * Skapar filen för en period eller för ett urval av resor (`{ tripIds }`). Layouten utelämnas
 * så att inställningen export.layout används.
 */
export function useCreateExport() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: (scope: ExportRange | { tripIds: string[] }) =>
      api.post<ExportJob>(
        "/exports",
        "tripIds" in scope ? { trip_ids: scope.tripIds, vehicle_id: vehicleId } : { from: scope.from, to: scope.to, vehicle_id: vehicleId },
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: exportKeys.list(vehicleId) }),
  });
}

export function useDeleteExport() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: (id: string) => api.del(`/exports/${id}`),
    onSettled: () => qc.invalidateQueries({ queryKey: exportKeys.list(vehicleId) }),
  });
}

// ----------------------------------------------------------------------------- månadsskiftet

/** Läget för innevarande och föregående månad för det valda fordonet (GET /month-close). */
export function useMonthClose() {
  const { vehicleId, vehicle } = useVehicle();
  return useQuery({
    queryKey: exportKeys.monthClose(vehicleId),
    queryFn: ({ signal }) => api.get<MonthClose>("/month-close", { query: { vehicle_id: vehicleId }, signal }),
    enabled: !!vehicleId && !vehicle?.is_demo,
  });
}

/** "Kontrollera nu": prövar månaden och skapar underlaget om den är klar (POST /month-close/{månad}/run). */
export function useRunMonthClose() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: (month: string) =>
      api.post<MonthStatus>(`/month-close/${encodeURIComponent(month)}/run`, undefined, { query: { vehicle_id: vehicleId } }),
    onSettled: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: exportKeys.monthClose(vehicleId) }),
        qc.invalidateQueries({ queryKey: exportKeys.list(vehicleId) }),
      ]),
  });
}

/** Stänger alla nedladdningslänkar som skickats i notiser (POST /month-close/links/revoke). */
export function useRevokeLinks() {
  return useMutation({
    mutationFn: () => api.post<null>("/month-close/links/revoke"),
  });
}

export function exportDownloadUrl(id: string): string {
  // Demot har ingen server: alla filer är samma riktiga exempelfil (statisk).
  if (DEMO_EXPORT_FILE) return DEMO_EXPORT_FILE;
  return buildUrl(`/exports/${id}/download`);
}

/**
 * Startar nedladdningen via en vanlig länk (sessionskakan följer med, GET kräver ingen
 * CSRF-token). Servern svarar med Content-Disposition: attachment, så sidan ligger kvar.
 */
export function startDownload(job: Pick<ExportJob, "id" | "filename">): void {
  const a = document.createElement("a");
  a.href = exportDownloadUrl(job.id);
  if (job.filename) a.download = job.filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
