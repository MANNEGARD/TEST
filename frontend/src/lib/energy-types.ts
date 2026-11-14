/**
 * Typer för Bränsle & energi.
 *  - Tankningar och laddningar: Pydantic-modeller → genererade typer.
 *  - GET /energy/summary svarar med dict[str, Any]; handskriven typ mot
 *    backend/app/services/stats/energy.py → summary() och monthly_breakdown().
 */

import type { components } from "./api-types";
import type { EnergyTotals, PeriodOut } from "./types";

type S = components["schemas"];

export type FuelRecord = S["FuelOut"];
export type ChargingRecord = S["ChargingOut"];

/** Body för POST/PATCH /energy/fuel (se FuelIn/FuelPatch i routes/energy.py). */
export interface FuelInput {
  at: string;
  liters: number;
  liters_quality?: "verified" | "estimated";
  total_cost: number | null;
  price_per_liter?: number | null;
  odometer_km: number | null;
  station: string | null;
  full_tank: boolean;
  note?: string | null;
}

/** Body för POST/PATCH /energy/charging (se ChargingIn/ChargingPatch). */
export interface ChargingInput {
  started_at: string;
  ended_at: string | null;
  kwh: number;
  kwh_quality?: "verified" | "estimated";
  total_cost: number | null;
  location: string | null;
  note?: string | null;
}

export interface EnergyMonth {
  /** yyyy-MM */
  month: string;
  total_km: number;
  fuel_verified_l: number;
  fuel_verified_km: number;
  fuel_estimated_l: number;
  fuel_unknown_km: number;
  fuel_coverage: number | null;
  fuel_per_100km: number | null;
  energy_verified_kwh: number;
  energy_verified_km: number;
  energy_estimated_kwh: number;
  energy_unknown_km: number;
  energy_coverage: number | null;
  energy_per_100km: number | null;
  regen_kwh: number;
  refuels: number;
  refuel_liters: number;
  fuel_cost: number | null;
  charging_sessions: number;
  charged_kwh: number;
  charging_cost: number | null;
}

export interface EnergySummary {
  period: PeriodOut;
  total_km: number;
  fuel: EnergyTotals & { data_types: { verified: string; estimated: string; unknown: string } };
  electricity: EnergyTotals & { regenerated_kwh: number };
  refuels: {
    count: number;
    liters: number;
    manual: number;
    detected: number;
    /** Summan av angivna belopp, null om inget belopp är angivet. */
    cost: number | null;
    average_price_per_liter: number | null;
  };
  charging: { count: number; kwh: number; cost: number | null };
  /** Uppmätt förbrukning × pris i inställningarna (costs.*), null utan pris. */
  estimated_costs: { fuel: number | null; electricity: number | null; note: string };
  /** Bilens eget snitt (trippmätare) – aldrig förbrukning per resa. */
  vehicle_reported_average: {
    fuel_l_per_100km: number | null;
    energy_kwh_per_100km: number | null;
    fetched_at: string;
    note: string;
  } | null;
  monthly: EnergyMonth[];
}

export interface DetectRefuelsResult {
  created: number;
  items: FuelRecord[];
}

/** settings.costs (backend/app/services/settings.py → DEFAULTS["costs"]). */
export interface CostSettings {
  fuel_price_per_liter: number | null;
  electricity_price_per_kwh: number | null;
}
