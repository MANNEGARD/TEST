"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, ApiError, buildUrl, getCsrfToken, type ApiErrorBody } from "../api";
import { demoFetch } from "../demo-hook";
import type {
  ImportBatch,
  ImportBatchList,
  ImportCommitResult,
  ImportRowList,
  ImportRowStatusCode,
  ImportUndoResult,
} from "../imports-types";
import { useVehicle } from "../vehicle";
import { invalidateJournalData } from "./keys";

/*
 * Import av körjournaler (backend/app/api/routes/imports.py):
 *   POST   /imports/preview                      ladda upp filer → förhandsgranskning (inga resor skapas)
 *   GET    /imports, /imports/{id}               tidigare importer, en import
 *   GET    /imports/{id}/rows                    rader med originalvärden och meddelanden
 *   POST   /imports/{id}/commit                  bekräfta → resorna skapas
 *   POST   /imports/{id}/undo                    ångra (resorna makuleras) / släng en förhandsgranskning
 *   DELETE /imports/{id}/files                   radera lagrade originalfiler
 *   GET    /imports/{id}/files/{file}/download   hämta en originalfil
 *
 * Datatäckning (coverage) skickas aldrig – backend väljer själv utifrån underlaget.
 */

export const importKeys = {
  all: ["imports"] as const,
  list: (vehicleId: string | undefined) => ["imports", "list", vehicleId] as const,
  batch: (batchId: string) => ["imports", "batch", batchId] as const,
  rows: (batchId: string, params: Record<string, unknown>) => ["imports", "rows", batchId, params] as const,
};

/** Första ledet i de nycklar som invalidateJournalData() redan tar hand om. */
const JOURNAL_KEYS = new Set<unknown>(["trips", "review", "dashboard", "gaps"]);

/**
 * En bekräftad eller ångrad import ändrar i stort sett allt: resor, granskning och
 * översikt (invalidateJournalData) men också platser, inlärda mönster, förbrukning
 * och importlistan. Därför invalideras allt utom inloggning och inställningar.
 */
export function invalidateAfterImport(qc: QueryClient): Promise<unknown> {
  return Promise.all([
    invalidateJournalData(qc),
    qc.invalidateQueries({
      predicate: (q) => {
        const area = q.queryKey[0];
        return area !== "auth" && area !== "settings" && !JOURNAL_KEYS.has(area);
      },
    }),
  ]);
}

// ----------------------------------------------------------------------------- läsning

/** Importer för valt fordon, nyast först (även förhandsgranskningar som aldrig bekräftats). */
export function useImportBatches() {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: importKeys.list(vehicleId),
    queryFn: ({ signal }) => api.get<ImportBatchList>("/imports", { query: { vehicle_id: vehicleId, limit: 100 }, signal }),
    enabled: !!vehicleId,
  });
}

/** En import med sammanställning. Efter uppladdning ligger svaret redan i cachen. */
export function useImportBatch(batchId: string | undefined) {
  return useQuery({
    queryKey: importKeys.batch(batchId ?? ""),
    queryFn: ({ signal }) => api.get<ImportBatch>(`/imports/${batchId}`, { signal }),
    enabled: !!batchId,
    staleTime: 5 * 60_000,
  });
}

export type ImportRowFilter = "all" | "new" | "duplicates" | "errors";

const ROW_FILTER_STATUS: Record<ImportRowFilter, ImportRowStatusCode[] | undefined> = {
  all: undefined,
  new: ["ok", "imported"],
  duplicates: ["duplicate", "duplicate_differs"],
  errors: ["error"],
};

export function useImportRows(batchId: string, filter: ImportRowFilter, page: number, pageSize = 50) {
  return useQuery({
    queryKey: importKeys.rows(batchId, { filter, page, pageSize }),
    queryFn: ({ signal }) =>
      api.get<ImportRowList>(`/imports/${batchId}/rows`, {
        query: { status: ROW_FILTER_STATUS[filter], page, page_size: pageSize },
        signal,
      }),
    placeholderData: keepPreviousData,
  });
}

// ----------------------------------------------------------------------------- uppladdning

/**
 * Multipart-uppladdning med förlopp. `api.upload` bygger på fetch, som inte kan
 * rapportera hur mycket som skickats – därför används XMLHttpRequest här, med samma
 * CSRF-header och samma fel (`ApiError`) som resten av klienten.
 */
function uploadWithProgress<T>(
  path: string,
  files: File[],
  fields: Record<string, string | undefined>,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  if (demoFetch) {
    // Demot: förhandsgranskningen är ett inspelat exempel (filen lämnar aldrig webbläsaren).
    onProgress?.(1);
    return demoFetch(buildUrl(path), { method: "POST" }).then(async (res) => {
      const body = (await res.json().catch(() => ({}))) as unknown;
      if (res.ok) return body as T;
      throw new ApiError(res.status, body && typeof body === "object" ? (body as ApiErrorBody) : {});
    });
  }
  return new Promise<T>((resolve, reject) => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    for (const [k, v] of Object.entries(fields)) if (v !== undefined) form.append(k, v);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", buildUrl(path));
    xhr.setRequestHeader("Accept", "application/json");
    const csrf = getCsrfToken();
    if (csrf) xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress?.(e.loaded / e.total);
    };
    xhr.upload.onload = () => onProgress?.(1);
    xhr.onload = () => {
      let body: unknown;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = undefined; // inte JSON (t.ex. fel från en mellanliggande proxy)
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T);
      else reject(new ApiError(xhr.status, body && typeof body === "object" ? (body as ApiErrorBody) : {}));
    };
    xhr.onerror = () => reject(new ApiError(0, {}));
    xhr.onabort = () => reject(new ApiError(0, {}));
    xhr.send(form);
  });
}

export interface PreviewUploadInput {
  files: File[];
  vehicleId: string | undefined;
  /** Andel av filerna som skickats, 0–1. Vid 1 återstår serverns tolkning. */
  onProgress?: (fraction: number) => void;
}

/** POST /imports/preview – tolkar filerna och skapar en förhandsgranskning. */
export function usePreviewUpload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ files, vehicleId, onProgress }: PreviewUploadInput) => {
      const fields = { vehicle_id: vehicleId };
      try {
        return await uploadWithProgress<ImportBatch>("/imports/preview", files, fields, onProgress);
      } catch (err) {
        // Utgången session eller CSRF-token: api.upload förnyar token och gör om
        // anropet, respektive skickar användaren till inloggningen.
        if (err instanceof ApiError && (err.status === 401 || (err.status === 403 && err.code === "csrf_failed"))) {
          return api.upload<ImportBatch>("/imports/preview", files, { fields });
        }
        throw err;
      }
    },
    onSuccess: (batch) => {
      qc.setQueryData(importKeys.batch(batch.id), batch);
    },
  });
}

// ----------------------------------------------------------------------------- mutationer

/** Bekräftar importen. `applyMergeRules` utelämnas för att följa inställningen. */
export function useCommitImport() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, applyMergeRules }: { batchId: string; applyMergeRules?: boolean }) =>
      api.post<ImportCommitResult>(`/imports/${batchId}/commit`, applyMergeRules === undefined ? {} : { apply_merge_rules: applyMergeRules }),
    // Väntar inte in omladdningen – kvittot ska visas direkt.
    onSuccess: () => {
      void invalidateAfterImport(qc);
    },
  });
}

/** Ångrar en bekräftad import: resorna makuleras och berörda sammanslagningar löses upp. */
export function useUndoImport() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) => api.post<ImportUndoResult>(`/imports/${batchId}/undo`),
    onSuccess: () => invalidateAfterImport(qc),
  });
}

/**
 * Slänger en förhandsgranskning som inte ska bekräftas: markerar den som ångrad och
 * raderar de uppladdade filerna (filer som delas med en annan import ligger kvar).
 */
export function useDiscardPreview() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (batchId: string) => {
      await api.post<ImportUndoResult>(`/imports/${batchId}/undo`);
      await api.del<{ removed: number }>(`/imports/${batchId}/files`);
    },
    onSettled: (_data, _err, batchId) => {
      qc.removeQueries({ queryKey: importKeys.batch(batchId) });
      qc.removeQueries({ queryKey: ["imports", "rows", batchId] });
      return qc.invalidateQueries({ queryKey: ["imports", "list"] });
    },
  });
}

/** Raderar de lagrade originalfilerna för en import. Resorna påverkas inte. */
export function useDeleteImportFiles() {
  return useMutation({
    mutationFn: (batchId: string) => api.del<{ removed: number }>(`/imports/${batchId}/files`),
  });
}

/** Hämtar en lagrad originalfil (404 om den har raderats). */
export function downloadImportFile(batchId: string, fileId: string, filename: string): Promise<void> {
  return api.download(`/imports/${batchId}/files/${fileId}/download`, { filename });
}
