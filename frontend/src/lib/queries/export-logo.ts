"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { ExportLogoMeta, LogoOut, SettingsPayload } from "../settings-page-types";
import { qk } from "./keys";

/**
 * Företagsloggan i underlaget (1.3.0, Inställningar → Milersättning och export). Uppladdning och
 * borttagning gäller direkt – de går inte via inställningarnas Spara. Loggans uppgifter står i
 * GET /settings (`export.logo`), så svaret skrivs in där och inställningarna läses om.
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

function useStoreLogo() {
  const qc = useQueryClient();
  return (out: LogoOut) => {
    const meta: ExportLogoMeta = out.has_logo
      ? {
          content_type: out.content_type as ExportLogoMeta["content_type"],
          width: out.width,
          height: out.height,
          size_bytes: out.size_bytes,
          uploaded_at: out.uploaded_at,
        }
      : {};
    qc.setQueryData<SettingsPayload>(qk.settings, (old) =>
      old ? { ...old, settings: { ...old.settings, export: { ...old.settings.export, logo: meta } } } : old,
    );
  };
}

export function useUploadExportLogo() {
  const qc = useQueryClient();
  const store = useStoreLogo();
  return useMutation({
    mutationFn: (file: File) => api.upload<LogoOut>(PATH, [file], { fileField: "file", timeoutMs: 60_000 }),
    onSuccess: store,
    onSettled: () => qc.invalidateQueries({ queryKey: qk.settings }),
  });
}

export function useRemoveExportLogo() {
  const qc = useQueryClient();
  const store = useStoreLogo();
  return useMutation({
    mutationFn: () => api.del<LogoOut>(PATH),
    onSuccess: (out) => store(out ?? { has_logo: false, content_type: null, width: null, height: null, size_bytes: null, uploaded_at: null, show_in_export: true }),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.settings }),
  });
}
