"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { ChargingInput, ChargingRecord, CostSettings, DetectRefuelsResult, EnergySummary, FuelInput, FuelRecord } from "../energy-types";
import { zonedToISO } from "../format";
import { addDays, periodQuery, periodToRange, type PeriodValue } from "../period";
import { useVehicle } from "../vehicle";
import { qk } from "./keys";

/*
 * Bränsle & energi (backend/app/api/routes/energy.py):
 *   GET  /energy/summary          ?period&from&to&vehicle_id
 *   GET  /energy/fuel             ?from&to (tidpunkter, UTC)&vehicle_id
 *   POST /energy/fuel, PATCH/DELETE /energy/fuel/{id}
 *   GET  /energy/charging         ?from&to&vehicle_id
 *   POST /energy/charging, PATCH/DELETE /energy/charging/{id}
 *   POST /energy/detect-refuels   ?vehicle_id  → {created, items}
 *
 * Tankningar och laddningar påverkar inte resorna, men kostnaderna visas även på
 * Översikt – därför invalideras dashboarden tillsammans med energifrågorna.
 */

export const energyKeys = {
  all: qk.energy.all,
  summary: (vehicleId: string | undefined, params: Record<string, unknown>) => ["energy", "summary", vehicleId, params] as const,
  fuel: (vehicleId: string | undefined, bounds: PeriodBounds) => ["energy", "fuel", vehicleId, bounds] as const,
  charging: (vehicleId: string | undefined, bounds: PeriodBounds) => ["energy", "charging", vehicleId, bounds] as const,
};

export function invalidateEnergy(qc: QueryClient): Promise<unknown> {
  return Promise.all([qc.invalidateQueries({ queryKey: energyKeys.all }), qc.invalidateQueries({ queryKey: qk.dashboard.all })]);
}

/** Periodens gränser som tidpunkter (UTC): från och med `from`, fram till `to`. */
export interface PeriodBounds {
  from?: string;
  to?: string;
}

/** Lokala datumgränser → tidpunkter i användarens tidszon (samma gränser som backend räknar fram). */
export function periodBounds(p: PeriodValue): PeriodBounds {
  const range = periodToRange(p);
  return {
    from: range.from ? zonedToISO(range.from, "00:00") : undefined,
    to: range.to ? zonedToISO(addDays(range.to, 1), "00:00") : undefined,
  };
}

/** Sant om tidpunkten ligger inom perioden. */
export function inBounds(at: string, b: PeriodBounds): boolean {
  const t = new Date(at).getTime();
  return (!b.from || t >= new Date(b.from).getTime()) && (!b.to || t < new Date(b.to).getTime());
}

function enabledFor(p: PeriodValue, vehicleId?: string) {
  return !!vehicleId && (p.key !== "custom" || (!!p.from && !!p.to));
}

export function useEnergySummary(p: PeriodValue) {
  const { vehicleId } = useVehicle();
  const query = { ...periodQuery(p), vehicle_id: vehicleId };
  return useQuery({
    queryKey: energyKeys.summary(vehicleId, query),
    queryFn: ({ signal }) => api.get<EnergySummary>("/energy/summary", { query, signal }),
    enabled: enabledFor(p, vehicleId),
    placeholderData: keepPreviousData,
  });
}

export function useFuelRecords(p: PeriodValue) {
  const { vehicleId } = useVehicle();
  const bounds = periodBounds(p);
  return useQuery({
    queryKey: energyKeys.fuel(vehicleId, bounds),
    queryFn: ({ signal }) => api.get<FuelRecord[]>("/energy/fuel", { query: { ...bounds, vehicle_id: vehicleId }, signal }),
    enabled: enabledFor(p, vehicleId),
    placeholderData: keepPreviousData,
  });
}

export function useChargingRecords(p: PeriodValue) {
  const { vehicleId } = useVehicle();
  const bounds = periodBounds(p);
  return useQuery({
    queryKey: energyKeys.charging(vehicleId, bounds),
    queryFn: ({ signal }) => api.get<ChargingRecord[]>("/energy/charging", { query: { ...bounds, vehicle_id: vehicleId }, signal }),
    enabled: enabledFor(p, vehicleId),
    placeholderData: keepPreviousData,
  });
}

// ----------------------------------------------------------------------------- tankningar

export function useCreateFuel() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: (body: FuelInput) => api.post<FuelRecord>("/energy/fuel", { ...body, vehicle_id: vehicleId }),
    onSuccess: () => invalidateEnergy(qc),
  });
}

export function useUpdateFuel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<FuelInput> }) => api.patch<FuelRecord>(`/energy/fuel/${id}`, patch),
    onSuccess: () => invalidateEnergy(qc),
  });
}

export function useDeleteFuel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`/energy/fuel/${id}`),
    onSuccess: () => invalidateEnergy(qc),
  });
}

/** Föreslår tankningar där tanknivån ökat mellan två fordonsrapporter (märks som uppskattade). */
export function useDetectRefuels() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: () => api.post<DetectRefuelsResult>("/energy/detect-refuels", undefined, { query: { vehicle_id: vehicleId } }),
    onSuccess: (res) => (res.created ? invalidateEnergy(qc) : undefined),
  });
}

// ----------------------------------------------------------------------------- laddningar

export function useCreateCharging() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: (body: ChargingInput) => api.post<ChargingRecord>("/energy/charging", { ...body, vehicle_id: vehicleId }),
    onSuccess: () => invalidateEnergy(qc),
  });
}

export function useUpdateCharging() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<ChargingInput> }) => api.patch<ChargingRecord>(`/energy/charging/${id}`, patch),
    onSuccess: () => invalidateEnergy(qc),
  });
}

export function useDeleteCharging() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`/energy/charging/${id}`),
    onSuccess: () => invalidateEnergy(qc),
  });
}

// ----------------------------------------------------------------------------- priser

/** Pris per liter/kWh för kostnadsuppskattningen (PATCH /settings/costs). */
export function useSaveCostSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CostSettings) => api.patch<CostSettings>("/settings/costs", body),
    onSuccess: () => Promise.all([qc.invalidateQueries({ queryKey: qk.settings }), invalidateEnergy(qc)]),
  });
}
