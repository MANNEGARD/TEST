"use client";

import * as React from "react";
import Link from "next/link";
import { Fuel, Info, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DateRangePicker } from "@/components/ui/date-range-picker";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { PageHeader } from "@/components/ui/page-header";
import { ChartCard, DataTable } from "@/components/charts/chart-card";
import { EnergyMiniChart } from "@/components/charts/energy-chart";
import type { EnergySummary } from "@/lib/energy-types";
import { formatDate, formatKm, formatKwh, formatLiters, formatPer100, formatRelative } from "@/lib/format";
import { bucketFor, type PeriodValue } from "@/lib/period";
import { useDashboardTimeseries } from "@/lib/queries/dashboard";
import { useEnergySummary } from "@/lib/queries/energy";
import type { TimeseriesPoint, Vehicle } from "@/lib/types";
import { cn, readStorage, writeStorage } from "@/lib/utils";
import { useVehicle } from "@/lib/vehicle";
import { ChargingSection } from "./charging-section";
import { CostsCard, hasCosts, PriceSettings } from "./costs";
import { EnergyPanel, type EnergyKind } from "./energy-panel";
import { FuelSection } from "./fuel-section";

const PERIOD_KEY = "drivelog.energy.period";
const BUCKET_WORD = { day: "dag", week: "vecka", month: "månad" } as const;

/**
 * Vilka energislag som visas. Fordonets drivmedel (från Volvo) avgör när det är känt;
 * annars visas båda – saknade värden visas som "–". Finns det data visas slaget alltid.
 */
function kindsFor(vehicle: Vehicle | undefined, s: EnergySummary | undefined): EnergyKind[] {
  const type = (vehicle?.fuel_type ?? "").toUpperCase();
  const burns = /PETROL|DIESEL|GASOLINE|BENSIN/.test(type);
  const electric = /ELECTRIC|HYBRID|PHEV|BEV/.test(type);
  const known = burns || electric;
  const fuelData = !!s && (s.fuel.verified > 0 || s.fuel.estimated > 0 || s.refuels.count > 0);
  const elData = !!s && (s.electricity.verified > 0 || s.electricity.estimated > 0 || s.electricity.regenerated_kwh > 0 || s.charging.count > 0);
  const kinds: EnergyKind[] = [];
  if (!known || burns || fuelData) kinds.push("fuel");
  if (!known || electric || elData) kinds.push("el");
  return kinds;
}

function EnergyChartCard({
  kind,
  points,
  bucket,
  loading,
  error,
  onRetry,
}: {
  kind: EnergyKind;
  points: TimeseriesPoint[];
  bucket: "day" | "week" | "month";
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const fuel = kind === "fuel";
  const value = (p: TimeseriesPoint) => (fuel ? p.fuel_liters : p.energy_kwh);
  const withData = points.filter((p) => value(p) > 0);
  return (
    <ChartCard
      title={`${fuel ? "Bensin" : "El"} per ${BUCKET_WORD[bucket]}`}
      description={`${fuel ? "Liter" : "kWh"} – förbrukning uppmätt av bilen`}
      loading={loading}
      error={error}
      onRetry={onRetry}
      empty={!withData.length}
      emptyText={`Ingen uppmätt ${fuel ? "bensinförbrukning" : "elförbrukning"} i perioden.`}
      height={200}
      table={
        <DataTable
          head={["Period", fuel ? "Bensin" : "El", "Snitt", "Uppmätt sträcka"]}
          rows={withData.map((p) => [
            bucket === "day" ? formatDate(p.bucket) : p.label,
            fuel ? formatLiters(p.fuel_liters) : formatKwh(p.energy_kwh),
            formatPer100(fuel ? p.fuel_per_100km : p.energy_per_100km, fuel ? "l" : "kWh"),
            formatKm(fuel ? p.fuel_km_covered : p.energy_km_covered),
          ])}
        />
      }
    >
      <EnergyMiniChart points={points} bucket={bucket} kind={kind} />
    </ChartCard>
  );
}

export function EnergyView() {
  const { vehicle } = useVehicle();
  const [period, setPeriodState] = React.useState<PeriodValue>(() =>
    typeof window === "undefined" ? { key: "year" } : readStorage<PeriodValue>("session", PERIOD_KEY, { key: "year" }),
  );
  const setPeriod = (p: PeriodValue) => {
    setPeriodState(p);
    writeStorage("session", PERIOD_KEY, p);
  };
  const showAll = () => setPeriod({ key: "all" });

  const bucket = bucketFor(period);
  const summary = useEnergySummary(period);
  const series = useDashboardTimeseries(period, bucket);
  const s = summary.data;
  const points = series.data?.points ?? [];
  const kinds = kindsFor(vehicle, s);
  const two = kinds.length === 2;
  // Ett diagram med en enda stapel säger inget mer än nyckeltalet ovanför.
  const showCharts = !series.data || points.length > 1 || series.isError;
  const nothing = !!s && s.total_km <= 0 && s.refuels.count === 0 && s.charging.count === 0;
  const avg = s?.vehicle_reported_average;

  return (
    <>
      <PageHeader
        title="Bränsle & energi"
        description={s && vehicle ? `${vehicle.name}${vehicle.registration_number ? ` · ${vehicle.registration_number}` : ""} · ${s.period.label}` : "Förbrukning, tankningar och laddningar"}
        actions={<DateRangePicker value={period} onChange={setPeriod} />}
      />

      {summary.isError ? (
        <Card>
          <ErrorState error={summary.error} onRetry={() => summary.refetch()} />
        </Card>
      ) : nothing ? (
        <Card>
          <EmptyState
            icon={<Fuel />}
            title="Inga resor under perioden"
            description="Förbrukningen hämtas ur bilens körjournal. Välj en annan period eller importera körjournalen från Volvo Cars-appen."
            action={
              <>
                <Button onClick={showAll}>Visa hela historiken</Button>
                <Button asChild variant="primary">
                  <Link href="/import">
                    <Upload aria-hidden /> Importera
                  </Link>
                </Button>
              </>
            }
          />
        </Card>
      ) : (
        <div
          className={cn(
            "grid gap-4 transition-opacity",
            two && "md:grid-flow-col md:grid-cols-2",
            two && (showCharts ? "md:grid-rows-[auto_auto]" : "md:grid-rows-1"),
            !two && showCharts && "lg:grid-cols-2",
            summary.isPlaceholderData && "opacity-60",
          )}
          aria-busy={summary.isPlaceholderData || undefined}
        >
          {kinds.map((kind) => (
            <React.Fragment key={kind}>
              <EnergyPanel kind={kind} summary={s} />
              {showCharts ? (
                <EnergyChartCard kind={kind} points={points} bucket={bucket} loading={!series.data && !series.isError} error={series.error} onRetry={() => series.refetch()} />
              ) : null}
            </React.Fragment>
          ))}
        </div>
      )}

      {!summary.isError && !nothing ? (
        <p className="mt-4 flex max-w-3xl items-start gap-2 text-xs leading-relaxed text-fg-muted" data-testid="energy-note">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>
            Bara förbrukning som bilen mätt per resa i den importerade körjournalen räknas som uppmätt. Bilens snittförbrukning och tanknivå från Volvo är inte förbrukning per resa och räknas aldrig in.
            {avg && (avg.fuel_l_per_100km != null || avg.energy_kwh_per_100km != null) ? (
              <>
                {" "}
                Bilens eget snitt (trippmätaren, {formatRelative(avg.fetched_at)}):{" "}
                <span className="font-mono">
                  {[avg.fuel_l_per_100km != null ? formatPer100(avg.fuel_l_per_100km, "l") : null, avg.energy_kwh_per_100km != null ? formatPer100(avg.energy_kwh_per_100km, "kWh") : null]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
                .
              </>
            ) : null}
          </span>
        </p>
      ) : null}

      <div className="mt-6 space-y-4">
        {s && hasCosts(s) ? <CostsCard summary={s} /> : null}
        <FuelSection period={period} onShowAll={showAll} />
        <ChargingSection period={period} onShowAll={showAll} />
        <PriceSettings />
      </div>
    </>
  );
}
