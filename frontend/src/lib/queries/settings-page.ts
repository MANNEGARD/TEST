"use client";

/**
 * Frågor och mutationer för inställningssidan (/settings).
 *
 * Inställningarna delar cache med `useSettings()` (qk.settings) så att tidszon, karta m.m.
 * i resten av appen följer med direkt när något sparas. Övriga nycklar är sidans egna och
 * ligger under "settings-page".
 */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type {
  ApiToken,
  ApiTokenCreated,
  BackupFile,
  DeleteHistoryInput,
  DeleteHistoryResult,
  Driver,
  LocationOption,
  OdometerInput,
  OdometerSnapshot,
  SettingsChanges,
  SettingsPayload,
  VehiclePatch,
  Workplace,
  WorkplaceInput,
} from "../settings-page-types";
import type { Vehicle } from "../types";
import { invalidateJournalData, qk } from "./keys";

export const settingsPageKeys = {
  all: ["settings-page"] as const,
  drivers: ["settings-page", "drivers"] as const,
  odometer: (vehicleId: string | undefined) => ["settings-page", "odometer", vehicleId] as const,
  workplaces: ["settings-page", "workplaces"] as const,
  locations: ["settings-page", "locations"] as const,
  tokens: ["settings-page", "tokens"] as const,
  backups: ["settings-page", "backups"] as const,
};

/** Integrationsvyns nycklar (src/lib/queries/integrations.ts) – invalideras när samma data ändras här. */
const INTEGRATIONS_KEY = ["integrations"] as const;
const INTEGRATIONS_TOKENS_KEY = ["integrations", "tokens"] as const;

/** Sektioner där backend räknar om förslagen för oavgjorda resor när något ändras. */
const REEVALUATING_SECTIONS = new Set(["tax", "ai", "calendar", "abbreviations", "lunch", "automation"]);
/** Sektioner som speglas i integrationsvyn (AI-sekretess, notiser, kalender). */
const MIRRORED_IN_INTEGRATIONS = new Set(["ai", "notifications", "calendar"]);

// ----------------------------------------------------------------------------- inställningar

/** Alla inställningar med standardvärden. Hämtas om varje gång sidan öppnas. */
export function useSettingsPage() {
  return useQuery({
    queryKey: qk.settings,
    queryFn: ({ signal }) => api.get<SettingsPayload>("/settings", { signal }),
    staleTime: 0,
  });
}

function invalidateAfterSettings(qc: QueryClient, changes: SettingsChanges): void {
  const sections = Object.keys(changes);
  const scopeChanged = "journal_scope" in (changes.general ?? {});
  if (scopeChanged || sections.some((s) => REEVALUATING_SECTIONS.has(s))) void invalidateJournalData(qc);
  else if (sections.includes("export")) {
    // Ersättning per mil, belopp och layout syns på Översikt och i exportens förhandsvisning.
    void qc.invalidateQueries({ queryKey: qk.dashboard.all });
    void qc.invalidateQueries({ queryKey: qk.exports.all });
  }
  if (sections.some((s) => MIRRORED_IN_INTEGRATIONS.has(s))) void qc.invalidateQueries({ queryKey: INTEGRATIONS_KEY });
}

/**
 * Sparar ändringar: en PATCH /settings/{sektion} per berörd sektion (i tur och ordning).
 * Cachen uppdateras efter varje lyckad sektion, så ett fel i en senare sektion lämnar
 * de redan sparade värdena synliga som sparade.
 */
export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (changes: SettingsChanges) => {
      const saved: string[] = [];
      for (const [section, values] of Object.entries(changes)) {
        if (!Object.keys(values).length) continue;
        const updated = await api.patch<Record<string, unknown>>(`/settings/${section}`, values);
        qc.setQueryData<SettingsPayload>(qk.settings, (old) =>
          old ? { ...old, settings: { ...old.settings, [section]: updated } } : old,
        );
        saved.push(section);
      }
      return saved;
    },
    onSettled: (_data, _err, changes) => invalidateAfterSettings(qc, changes),
  });
}

// ----------------------------------------------------------------------------- fordon

export function useUpdateVehicle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: VehiclePatch }) => api.patch<Vehicle>(`/vehicles/${id}`, patch),
    onSuccess: (vehicle) => {
      qc.setQueryData<Vehicle[]>(qk.vehicles, (old) => old?.map((v) => (v.id === vehicle.id ? vehicle : v)));
      void qc.invalidateQueries({ queryKey: qk.vehicles });
      void qc.invalidateQueries({ queryKey: qk.dashboard.all }); // fordonets namn visas på översikten
      void qc.invalidateQueries({ queryKey: qk.exports.all }); // och i exportens sidhuvud
    },
  });
}

/** Förare (för att visa vilket namn exporten använder när inget eget namn är angivet). */
export function useDrivers() {
  return useQuery({
    queryKey: settingsPageKeys.drivers,
    queryFn: ({ signal }) => api.get<Driver[]>("/drivers", { signal }),
    staleTime: 5 * 60_000,
  });
}

export function useOdometerReadings(vehicleId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: settingsPageKeys.odometer(vehicleId),
    queryFn: ({ signal }) => api.get<OdometerSnapshot[]>(`/vehicles/${vehicleId}/odometer`, { query: { limit: 200 }, signal }),
    enabled: !!vehicleId && enabled,
  });
}

function invalidateOdometer(qc: QueryClient, vehicleId: string): void {
  void qc.invalidateQueries({ queryKey: settingsPageKeys.odometer(vehicleId) });
  void qc.invalidateQueries({ queryKey: qk.dashboard.all }); // senast kända mätarställning
  void qc.invalidateQueries({ queryKey: qk.exports.all }); // exportens kontroller och mätarställningar
}

export function useAddOdometerReading() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ vehicleId, body }: { vehicleId: string; body: OdometerInput }) =>
      api.post<OdometerSnapshot>(`/vehicles/${vehicleId}/odometer`, body),
    onSuccess: (_snap, { vehicleId }) => invalidateOdometer(qc, vehicleId),
  });
}

export function useDeleteOdometerReading() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ vehicleId, snapshotId }: { vehicleId: string; snapshotId: string }) =>
      api.del(`/vehicles/${vehicleId}/odometer/${snapshotId}`),
    onSuccess: (_void, { vehicleId }) => invalidateOdometer(qc, vehicleId),
  });
}

// ----------------------------------------------------------------------------- tjänsteställen

export function useWorkplaces(enabled = true) {
  return useQuery({
    queryKey: settingsPageKeys.workplaces,
    queryFn: ({ signal }) => api.get<Workplace[]>("/settings/workplaces/list", { signal }),
    enabled,
  });
}

/** Platser att välja som tjänsteställe (endast det formuläret behöver). */
export function useLocationOptions(enabled = true) {
  return useQuery({
    queryKey: settingsPageKeys.locations,
    queryFn: ({ signal }) => api.get<LocationOption[]>("/locations", { signal }),
    enabled,
    select: (rows) =>
      rows
        .map(({ id, name, type, city, confirmed }) => ({ id, name, type, city, confirmed }))
        .sort((a, b) => a.name.localeCompare(b.name, "sv")),
  });
}

/** Tjänsteställen styr förslagen för oavgjorda resor – backend räknar om dem direkt. */
function invalidateAfterWorkplace(qc: QueryClient): void {
  void qc.invalidateQueries({ queryKey: settingsPageKeys.workplaces });
  void invalidateJournalData(qc);
}

export function useAddWorkplace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: WorkplaceInput) => api.post<Workplace>("/settings/workplaces", body),
    onSuccess: () => invalidateAfterWorkplace(qc),
  });
}

export function useDeleteWorkplace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`/settings/workplaces/${id}`),
    onSuccess: () => invalidateAfterWorkplace(qc),
  });
}

// ----------------------------------------------------------------------------- sekretess

/** Raderar historik permanent. Allt i appen kan påverkas – hela cachen hämtas om. */
export function useDeleteHistory() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: DeleteHistoryInput) => api.post<DeleteHistoryResult>("/settings/delete-history", body),
    onSuccess: () => qc.invalidateQueries(),
  });
}

// ----------------------------------------------------------------------------- säkerhet

export function useChangePassword() {
  return useMutation({
    mutationFn: (body: { current_password: string; new_password: string }) => api.post<void>("/auth/change-password", body),
  });
}

export function useApiTokens(enabled = true) {
  return useQuery({
    queryKey: settingsPageKeys.tokens,
    queryFn: ({ signal }) => api.get<ApiToken[]>("/auth/tokens", { signal }),
    staleTime: 0,
    enabled,
  });
}

function invalidateTokens(qc: QueryClient): void {
  void qc.invalidateQueries({ queryKey: settingsPageKeys.tokens });
  void qc.invalidateQueries({ queryKey: INTEGRATIONS_TOKENS_KEY });
}

/** Svaret innehåller hemligheten EN gång – den lagras aldrig i cachen. */
export function useCreateApiToken() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.post<ApiTokenCreated>("/auth/tokens", { name, scopes: ["ha"] }),
    onSuccess: () => invalidateTokens(qc),
  });
}

export function useRevokeApiToken() {
  const qc = useQueryClient();
  return useMutation({
    // Första DELETE återkallar, en andra tar bort raden. Listorna visar bara aktiva tokens.
    // Läs om även vid fel (t.ex. 404 om tokenen redan tagits bort i en annan flik).
    mutationFn: (id: string) => api.del(`/auth/tokens/${id}`),
    onSettled: () => invalidateTokens(qc),
  });
}

// ----------------------------------------------------------------------------- säkerhetskopior

export function useBackups(enabled = true) {
  return useQuery({
    queryKey: settingsPageKeys.backups,
    queryFn: ({ signal }) => api.get<BackupFile[]>("/backups", { signal }),
    staleTime: 0,
    enabled,
  });
}

export function useCreateBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ name: string; size_bytes: number }>("/backups"),
    onSuccess: () => qc.invalidateQueries({ queryKey: settingsPageKeys.backups }),
  });
}

export function useDeleteBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.del(`/backups/${encodeURIComponent(name)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: settingsPageKeys.backups }),
  });
}

export function downloadBackup(name: string): Promise<void> {
  return api.download(`/backups/${encodeURIComponent(name)}/download`, { filename: name });
}
