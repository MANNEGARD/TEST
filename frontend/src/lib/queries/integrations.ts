"use client";

/**
 * Frågor och mutationer för integrationsvyn (Volvo Cars, Microsoft Outlook,
 * Home Assistant, AI). Nycklarna ligger här – allt börjar med "integrations" så
 * att en mutation kan invalidera hela området på en gång.
 */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type {
  AiProviderName,
  AiStatus,
  AiTestResult,
  ApiTokenCreated,
  ApiTokenOut,
  DemoPhoneNotification,
  HaConfigInput,
  HaDevices,
  HaStatus,
  HaTestNotification,
  HaTestResult,
  IcsStatus,
  IcsSyncResult,
  MicrosoftCalendar,
  MicrosoftStatus,
  MicrosoftSyncResult,
  RespondResult,
  VolvoAccountVehicles,
  VolvoSimulateResult,
  VolvoStatus,
  VolvoSyncResult,
} from "../integrations-types";
import { invalidateJournalData, qk } from "./keys";

export const integrationKeys = {
  all: ["integrations"] as const,
  volvo: ["integrations", "volvo", "status"] as const,
  volvoVehicles: ["integrations", "volvo", "vehicles"] as const,
  microsoft: ["integrations", "microsoft", "status"] as const,
  microsoftCalendars: ["integrations", "microsoft", "calendars"] as const,
  ics: ["integrations", "microsoft", "ics"] as const,
  ha: ["integrations", "home-assistant", "status"] as const,
  haDevices: ["integrations", "home-assistant", "devices"] as const,
  ai: ["integrations", "ai"] as const,
  tokens: ["integrations", "tokens"] as const,
  demoInbox: ["integrations", "demo-inbox"] as const,
} as const;

/** Vad en mutation påverkar utöver integrationernas egen status. */
interface Affects {
  /** Resor, granskning och översikt (t.ex. synk, simulerad resa, svar på en fråga). */
  journal?: boolean;
  /** Fordonslistan (demofordonet skapas/tas bort, en bil kopplas). */
  vehicles?: boolean;
  /** Inställningar som speglas på /settings (leveransläge, AI-leverantör, kalenderval). */
  settings?: boolean;
}

function invalidate(qc: QueryClient, affects: Affects): Promise<unknown> {
  return Promise.all([
    qc.invalidateQueries({ queryKey: integrationKeys.all }),
    affects.journal ? invalidateJournalData(qc) : null,
    affects.vehicles ? qc.invalidateQueries({ queryKey: qk.vehicles }) : null,
    affects.settings ? qc.invalidateQueries({ queryKey: qk.settings }) : null,
  ]);
}

/**
 * Mutation som alltid läser om integrationsstatusen efteråt – även vid fel, eftersom
 * backend kan ha sparat ett nytt läge (t.ex. "behöver anslutas på nytt").
 */
function useIntegrationMutation<TVars = void, TData = unknown>(fn: (vars: TVars) => Promise<TData>, affects: Affects = {}) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => invalidate(qc, affects),
  });
}

// ----------------------------------------------------------------------------- Volvo

export function useVolvoStatus() {
  return useQuery({
    queryKey: integrationKeys.volvo,
    queryFn: ({ signal }) => api.get<VolvoStatus>("/integrations/volvo/status", { signal }),
  });
}

export function useVolvoAccountVehicles(enabled: boolean) {
  return useQuery({
    queryKey: integrationKeys.volvoVehicles,
    queryFn: ({ signal }) => api.get<VolvoAccountVehicles>("/integrations/volvo/vehicles", { signal }),
    enabled,
  });
}

/** Hämtar fordonslistan på nytt från Volvo (GET med refresh=true gör ett riktigt anrop). */
export function useRefreshVolvoVehicles() {
  return useIntegrationMutation(() => api.get<VolvoAccountVehicles>("/integrations/volvo/vehicles", { query: { refresh: true } }));
}

export function useSaveVolvoCredentials() {
  return useIntegrationMutation(
    (body: { client_id: string; client_secret?: string; vcc_api_key?: string; auth_server?: "new" | "legacy" }) =>
      api.put<VolvoStatus>("/integrations/volvo/credentials", body),
  );
}

export function useVolvoConnect() {
  return useIntegrationMutation(() => api.post<{ authorize_url: string }>("/integrations/volvo/connect"));
}

export function useVolvoTestToken() {
  return useIntegrationMutation((body: { access_token: string; vcc_api_key?: string }) =>
    api.post<VolvoStatus>("/integrations/volvo/test-token", body),
  );
}

export function useSelectVolvoVehicle() {
  return useIntegrationMutation(
    (body: { ref: string; vehicle_id?: string }) => api.post<VolvoStatus>("/integrations/volvo/select-vehicle", body),
    { vehicles: true },
  );
}

export function useVolvoSync() {
  return useIntegrationMutation(() => api.post<VolvoSyncResult>("/integrations/volvo/sync"), { journal: true });
}

export function useVolvoDisconnect() {
  return useIntegrationMutation(() => api.post<VolvoStatus>("/integrations/volvo/disconnect"), { vehicles: true });
}

export function useVolvoDemo() {
  return useIntegrationMutation(
    (enable: boolean) => api.post<VolvoStatus>(`/integrations/volvo/demo/${enable ? "enable" : "disable"}`),
    { journal: true, vehicles: true },
  );
}

export function useVolvoSimulate() {
  return useIntegrationMutation(
    (scenario: string | null) => api.post<VolvoSimulateResult>("/integrations/volvo/demo/simulate", scenario ? { scenario } : {}),
    { journal: true },
  );
}

/** Hur många dygn felsökningsfilen täcker (GET /volvo/diagnostics tar 1–31). */
export const VOLVO_DIAGNOSTICS_DAYS = 14;

/**
 * Felsökningsfilen (1.3.0): bilens avläsningar, hämtningarnas utfall och de resor som byggdes av
 * dem – utan chassinummer, registreringsnummer, positioner och adresser. Filnamnet kommer från servern.
 */
export function useDownloadVolvoDiagnostics() {
  return useMutation({
    mutationFn: (days: number = VOLVO_DIAGNOSTICS_DAYS) => api.download(`/volvo/diagnostics?days=${days}`, { timeoutMs: 60_000 }),
  });
}

// ----------------------------------------------------------------------------- Microsoft Outlook

export function useMicrosoftStatus() {
  return useQuery({
    queryKey: integrationKeys.microsoft,
    queryFn: ({ signal }) => api.get<MicrosoftStatus>("/integrations/microsoft/status", { signal }),
  });
}

export function useMicrosoftCalendars(enabled: boolean) {
  return useQuery({
    queryKey: integrationKeys.microsoftCalendars,
    queryFn: ({ signal }) => api.get<MicrosoftCalendar[]>("/integrations/microsoft/calendars", { signal }),
    enabled,
    retry: false,
  });
}

export function useSaveMicrosoftCredentials() {
  return useIntegrationMutation((body: { client_id: string; client_secret?: string; tenant: string }) =>
    api.put<unknown>("/integrations/microsoft/credentials", body),
  );
}

export function useMicrosoftConnect() {
  return useIntegrationMutation(() => api.post<{ authorize_url: string }>("/integrations/microsoft/connect"));
}

export function useSelectMicrosoftCalendars() {
  return useIntegrationMutation(
    (calendarIds: string[]) => api.put<{ calendar_ids: string[] }>("/integrations/microsoft/calendars", { calendar_ids: calendarIds }),
    { settings: true },
  );
}

export function useMicrosoftSync() {
  return useIntegrationMutation(() => api.post<MicrosoftSyncResult>("/integrations/microsoft/sync"), { journal: true });
}

export function useMicrosoftDisconnect() {
  // forget_app: radera även appregistreringens klient-ID och klienthemlighet.
  return useIntegrationMutation(
    (opts: { forgetApp?: boolean } | void) => api.post<unknown>("/integrations/microsoft/disconnect", opts?.forgetApp ? { forget_app: true } : undefined),
    { journal: true },
  );
}

export function useMicrosoftDemo() {
  return useIntegrationMutation(
    (enable: boolean) => api.post<{ status: string; events_created?: number; events_removed?: number }>(`/integrations/microsoft/demo/${enable ? "enable" : "disable"}`),
    { journal: true, vehicles: true },
  );
}

// ----------------------------------------------------------------------------- Kalenderlänk (ICS)

/** Status för kalenderlänken. Länken själv lämnas aldrig ut av servern – bara att den finns och värdnamnet. */
export function useIcsStatus() {
  return useQuery({
    queryKey: integrationKeys.ics,
    queryFn: ({ signal }) => api.get<IcsStatus>("/integrations/microsoft/ics", { signal }),
  });
}

/** Sparar eller byter länk. Servern hämtar och tolkar länken först – går det inte sparas ingenting. */
export function useSaveIcsLink() {
  return useIntegrationMutation((url: string) => api.put<IcsStatus>("/integrations/microsoft/ics", { url }), { journal: true });
}

export function useIcsSync() {
  return useIntegrationMutation(() => api.post<IcsSyncResult>("/integrations/microsoft/ics/sync"), { journal: true });
}

/** Tar bort länken och alla händelser som har hämtats från den. */
export function useRemoveIcsLink() {
  return useIntegrationMutation(() => api.del<IcsStatus>("/integrations/microsoft/ics"), { journal: true });
}

// ----------------------------------------------------------------------------- Home Assistant

export function useHaStatus(options: { poll?: boolean; intervalMs?: number } = {}) {
  return useQuery({
    queryKey: integrationKeys.ha,
    queryFn: ({ signal }) => api.get<HaStatus>("/integrations/home-assistant", { signal }),
    // Medan panelen är öppen: fånga livstecken och bekräftad testnotis utan omladdning.
    // Banderollen i app-skalet frågar glesare (intervalMs).
    refetchInterval: options.poll ? 15_000 : (options.intervalMs ?? false),
  });
}

export function useHaDevices(enabled: boolean) {
  return useQuery({
    queryKey: integrationKeys.haDevices,
    queryFn: ({ signal }) => api.get<HaDevices>("/integrations/home-assistant/devices", { signal }),
    enabled,
    retry: false,
  });
}

export function useConfigureHa() {
  return useIntegrationMutation((body: HaConfigInput) => api.put<HaStatus>("/integrations/home-assistant", body), { settings: true });
}

export function useTestHa() {
  return useIntegrationMutation(() => api.post<HaTestResult>("/integrations/home-assistant/test"));
}

export function useHaTestNotification() {
  return useIntegrationMutation(() => api.post<HaTestNotification>("/integrations/home-assistant/test-notification"));
}

export function useHaDisconnect() {
  return useIntegrationMutation(() => api.post<HaStatus>("/integrations/home-assistant/disconnect"));
}

export function useHaDemo() {
  return useIntegrationMutation(
    (enable: boolean) => api.post<HaStatus>(`/integrations/home-assistant/demo/${enable ? "enable" : "disable"}`),
    { journal: true },
  );
}

/** Notiser på den simulerade telefonen (demoläge). */
export function useDemoInbox(enabled: boolean) {
  return useQuery({
    queryKey: integrationKeys.demoInbox,
    queryFn: ({ signal }) => api.get<DemoPhoneNotification[]>("/notifications/demo-inbox", { signal }),
    enabled,
    refetchInterval: enabled ? 5_000 : false,
  });
}

/** Knapptryck eller textsvar på demotelefonen. */
export function useDemoRespond() {
  return useIntegrationMutation(
    ({ publicId, action, replyText }: { publicId: string; action: string; replyText?: string }) =>
      api.post<RespondResult>(`/notifications/${encodeURIComponent(publicId)}/respond`, { action, reply_text: replyText ?? null, as_phone: true }),
    { journal: true },
  );
}

export function useApiTokens(enabled = true) {
  return useQuery({
    queryKey: integrationKeys.tokens,
    queryFn: ({ signal }) => api.get<ApiTokenOut[]>("/auth/tokens", { signal }),
    enabled,
  });
}

export function useCreateHaToken() {
  return useIntegrationMutation((name: string) => api.post<ApiTokenCreated>("/auth/tokens", { name, scopes: ["ha"] }));
}

export function useRevokeApiToken() {
  return useIntegrationMutation((id: string) => api.del(`/auth/tokens/${id}`));
}

// ----------------------------------------------------------------------------- AI

export function useAiStatus() {
  return useQuery({
    queryKey: integrationKeys.ai,
    queryFn: ({ signal }) => api.get<AiStatus>("/integrations/ai", { signal }),
  });
}

export function useConfigureAi() {
  return useIntegrationMutation(
    (body: { provider: AiProviderName; model: string; api_key?: string }) => api.put<AiStatus>("/integrations/ai", body),
    { settings: true },
  );
}

export function useTestAi() {
  return useIntegrationMutation(() => api.post<AiTestResult>("/integrations/ai/test"));
}

export function useRemoveAi() {
  return useIntegrationMutation(() => api.del<AiStatus>("/integrations/ai"), { settings: true });
}
