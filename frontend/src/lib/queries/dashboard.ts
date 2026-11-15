"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { bucketFor, periodQuery, type PeriodValue } from "../period";
import type { DashboardSummary, DestinationRow, MonthlyComparison, RouteRow, Timeseries } from "../types";
import { useVehicle } from "../vehicle";
import { qk } from "./keys";

/*
 * Dashboard-endpoints (backend/app/api/routes/dashboard.py):
 *   GET /dashboard/summary       ?period&from&to&vehicle_id
 *   GET /dashboard/timeseries    ?period&from&to&bucket&vehicle_id
 *   GET /dashboard/destinations  ?period&from&to&limit&vehicle_id  → {period, items}
 *   GET /dashboard/routes        ?period&from&to&limit&vehicle_id  → {period, items}
 *   GET /dashboard/monthly       ?year&vehicle_id
 */

function enabledFor(p: PeriodValue, vehicleId?: string) {
  return !!vehicleId && (p.key !== "custom" || (!!p.from && !!p.to));
}

export function useDashboardSummary(p: PeriodValue) {
  const { vehicleId } = useVehicle();
  const query = { ...periodQuery(p), vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.dashboard.part("summary", vehicleId, query),
    queryFn: ({ signal }) => api.get<DashboardSummary>("/dashboard/summary", { query, signal }),
    enabled: enabledFor(p, vehicleId),
    placeholderData: keepPreviousData,
  });
}

export function useDashboardTimeseries(p: PeriodValue, bucket = bucketFor(p)) {
  const { vehicleId } = useVehicle();
  const query = { ...periodQuery(p), bucket, vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.dashboard.part("timeseries", vehicleId, query),
    queryFn: ({ signal }) => api.get<Timeseries>("/dashboard/timeseries", { query, signal }),
    enabled: enabledFor(p, vehicleId),
    placeholderData: keepPreviousData,
  });
}

export function useDashboardDestinations(p: PeriodValue, limit = 8) {
  const { vehicleId } = useVehicle();
  const query = { ...periodQuery(p), limit, vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.dashboard.part("destinations", vehicleId, query),
    queryFn: ({ signal }) => api.get<{ items: DestinationRow[] }>("/dashboard/destinations", { query, signal }),
    enabled: enabledFor(p, vehicleId),
    placeholderData: keepPreviousData,
  });
}

export function useDashboardRoutes(p: PeriodValue, limit = 6) {
  const { vehicleId } = useVehicle();
  const query = { ...periodQuery(p), limit, vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.dashboard.part("routes", vehicleId, query),
    queryFn: ({ signal }) => api.get<{ items: RouteRow[] }>("/dashboard/routes", { query, signal }),
    enabled: enabledFor(p, vehicleId),
    placeholderData: keepPreviousData,
  });
}

export function useDashboardMonthly(year?: number) {
  const { vehicleId } = useVehicle();
  const query = { year, vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.dashboard.part("monthly", vehicleId, query),
    queryFn: ({ signal }) => api.get<MonthlyComparison>("/dashboard/monthly", { query, signal }),
    enabled: !!vehicleId,
    placeholderData: keepPreviousData,
  });
}

/** GET /reports/weekly (backend/app/services/stats/reports.py → weekly_report). Endast de fält som visas. */
export interface WeeklyReport {
  journal_scope: "business_only" | "complete" | string;
  week: { year: number; week: number; start: string; end: string };
  distance: { total: { km: number }; work: { km: number }; private: { km: number }; undecided: { km: number } };
  trips: { total: number; by_category: Record<string, number> };
  top_destinations: { label: string; trips: number; total_km: number }[];
  unreviewed_trips: number;
  missing_purpose_trips: number;
  trips_to_review: number;
  data_gaps: { count: number; km: number };
}

/** Veckorapporten för veckan som börjar `weekStart` (måndag, yyyy-MM-dd); utelämnad = innevarande vecka. */
export function useWeeklyReport(weekStart?: string) {
  const { vehicleId } = useVehicle();
  const query = { week_start: weekStart, vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.dashboard.part("weekly", vehicleId, query),
    queryFn: ({ signal }) => api.get<WeeklyReport>("/reports/weekly", { query, signal }),
    enabled: !!vehicleId,
    placeholderData: keepPreviousData,
  });
}
