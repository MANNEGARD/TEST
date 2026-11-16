"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { ExportLogoMeta, LogoOut, SettingsPayload } from "../settings-page-types";
import { qk } from "./keys";

/**
 * Företagsloggan i underlaget (1.3.0, Inställningar → Milersättning och export). Uppladdning och
 * borttagning gäller direkt – de går inte via inställningarnas Spara. Loggans uppgifter står i
 * GET /settings (`export.logo`), så inställningarna läses om efter varje ändring.
 */
const PATH = "/settings/logo";

/** Adressen till bilden; `v` byter adress när loggan byts så att webbläsaren inte visar den gamla. */
export function logoImageSrc(meta: ExportLogoMeta): string {
  return meta.demo_src ?? `/api/v1${PATH}?v=${encodeURIComponent(meta.uploaded_at ?? "")}`;
}

/** Loggans uppgifter ur inställningarna, eller null när ingen logga finns. */
export function logoMeta(data: SettingsPayload | undefined): ExportLogoMeta | null {
  const value = data?.settings.export?.logo;
  if (!value || typeof value !== "object") return null;
  const meta = value as ExportLogoMeta;
  return meta.uploaded_at || meta.content_type ? meta : null;
}

export function useUploadExportLogo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.upload<LogoOut>(PATH, [file], { fileField: "file", timeoutMs: 60_000 }),
    // Knappen snurrar tills inställningarna (med loggans nya uppgifter) är omlästa.
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.settings }),
  });
}

export function useRemoveExportLogo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.del<LogoOut>(PATH),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.settings }),
  });
}
