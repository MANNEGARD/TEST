"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { ExportLogo } from "../settings-page-types";

/**
 * Företagsloggan i underlaget (1.3.0, Inställningar → Milersättning och export). Uppladdning och
 * borttagning gäller direkt – de går inte via inställningarnas Spara.
 */
export const exportLogoKeys = {
  logo: ["settings", "export-logo"] as const,
};

const PATH = "/settings/export-logo";

export function useExportLogo() {
  return useQuery({
    queryKey: exportLogoKeys.logo,
    queryFn: ({ signal }) => api.get<ExportLogo>(PATH, { signal }),
  });
}

export function useUploadExportLogo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.upload<ExportLogo>(PATH, [file], { fileField: "file", timeoutMs: 60_000 }),
    onSuccess: (logo) => qc.setQueryData(exportLogoKeys.logo, logo),
    onSettled: () => qc.invalidateQueries({ queryKey: exportLogoKeys.logo }),
  });
}

export function useRemoveExportLogo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.del(PATH),
    onSettled: () => qc.invalidateQueries({ queryKey: exportLogoKeys.logo }),
  });
}
