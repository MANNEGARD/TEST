"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, buildUrl } from "../api";
import type { ExportChecks, ExportJob, ExportPreview, ExportRange } from "../exports-types";
import { useVehicle } from "../vehicle";

/**
 * Nycklar för exportsidan. (Ligger här i stället för i keys.ts – se shared_requests:
 * de bör flyttas dit och `invalidateJournalData` bör även invalidera ["exports"].)
 */
export const exportKeys = {
  all: ["exports"] as const,
  preview: (vehicleId: string | undefined, range: ExportRange) => ["exports", "preview", vehicleId, range.from, range.to] as const,
  checks: (vehicleId: string | undefined, range: ExportRange) => ["exports", "checks", vehicleId, range.from, range.to] as const,
  list: (vehicleId: string | undefined) => ["exports", "list", vehicleId] as const,
};

/** Exakt de rader och summor som Excel-filen kommer att innehålla. */
export function useExportPreview(range: ExportRange | null) {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: exportKeys.preview(vehicleId, range ?? { from: "", to: "" }),
    queryFn: ({ signal }) =>
      api.get<ExportPreview>("/exports/preview", { query: { from: range?.from, to: range?.to, vehicle_id: vehicleId }, signal }),
    enabled: !!vehicleId && !!range,
    placeholderData: keepPreviousData,
  });
}

export function useExportChecks(range: ExportRange | null) {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: exportKeys.checks(vehicleId, range ?? { from: "", to: "" }),
    queryFn: ({ signal }) =>
      api.get<ExportChecks>("/exports/checks", { query: { from: range?.from, to: range?.to, vehicle_id: vehicleId }, signal }),
    enabled: !!vehicleId && !!range,
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

/** Skapar filen. Layouten utelämnas så att inställningen export.layout används. */
export function useCreateExport() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: (range: ExportRange) => api.post<ExportJob>("/exports", { from: range.from, to: range.to, vehicle_id: vehicleId }),
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

export function exportDownloadUrl(id: string): string {
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
