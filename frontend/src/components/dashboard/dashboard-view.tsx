"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, Banknote, Briefcase, ClipboardCheck, Gauge, MapPin, RefreshCw, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { DateRangePicker } from "@/components/ui/date-range-picker";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { PageHeader } from "@/components/ui/page-header";
import { StatCard } from "@/components/ui/stat-card";
import { Tooltip } from "@/components/ui/tooltip";
import { ChartCard, DataTable } from "@/components/charts/chart-card";
import { ConsumptionChart } from "@/components/charts/consumption-chart";
import { DestinationsList } from "@/components/charts/destinations-list";
import { DistanceChart } from "@/components/charts/distance-chart";
import { EnergyMiniChart } from "@/components/charts/energy-chart";
import { MonthlyChart } from "@/components/charts/monthly-chart";
import { QualityValue } from "@/components/trips/quality-value";
import {
  formatCurrency,
  formatDate,
  formatDateTime,
  formatKm,
  formatKwh,
  formatLiters,
  formatMil,
  formatNumber,
  formatOdometer,
  formatPer100,
  formatPercent,
  formatRelative,
  milNumber,
  plural,
} from "@/lib/format";
import { INTEGRATION_STATUS_LABELS, label } from "@/lib/labels";
import { bucketFor, type PeriodValue } from "@/lib/period";
import {
  useDashboardDestinations,
  useDashboardMonthly,
  useDashboardRoutes,
  useDashboardSummary,
  useDashboardTimeseries,
} from "@/lib/queries/dashboard";
import { useOpenReviewCount } from "@/lib/queries/review";
import type { DashboardSummary, TimeseriesPoint } from "@/lib/types";
import { readStorage, writeStorage } from "@/lib/utils";

const PERIOD_KEY = "drivelog.dashboard.period";

function KpiRow({ s, loading }: { s: DashboardSummary | undefined; loading: boolean }) {
  const reviewCount = useOpenReviewCount();
  const workKm = s?.distance.work.km ?? 0;
  const prelim = s?.distance.preliminary_work.km ?? 0;
  const privateKm = s?.distance.private.km ?? 0;
  const workTrips = s ? s.trips.by_category.work + s.trips.by_category.mixed : 0;
  const r = s?.reimbursement;
  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
      <StatCard
        label="Tjänstekörning"
        accent="work"
        icon={<Briefcase />}
        loading={loading}
        value={milNumber(workKm)}
        unit="mil"
        sub={
          <>
            <span className="num">{formatKm(workKm)}</span>
            {prelim > 0 ? <> · varav <span className="num">{formatMil(prelim)}</span> preliminärt klassat</> : null}
            {privateKm > 0 ? <span className="block text-fg-subtle">Privat: <span className="num">{formatMil(privateKm)}</span></span> : null}
          </>
        }
      />
      <StatCard
        label="Tjänsteresor"
        icon={<MapPin />}
        loading={loading}
        value={formatNumber(workTrips, 0)}
        sub={s ? <>Körtid totalt {formatNumber(s.trips.driving_hours, 1)} h</> : null}
      />
      {r && r.amount != null ? (
        <StatCard
          label="Milersättning"
          icon={<Banknote />}
          accent="ok"
          loading={loading}
          value={formatCurrency(r.amount, r.currency ?? "SEK").replace(/ kr$/, "")}
          unit="kr"
          sub={r.mil != null && r.rate_per_mil != null ? <><span className="num">{formatNumber(r.mil, 1)} mil</span> × {formatCurrency(r.rate_per_mil)}/mil</> : r.note}
        />
      ) : (
        <StatCard
          label="Senaste mätarställning"
          icon={<Gauge />}
          loading={loading}
          value={s?.odometer.value != null ? formatNumber(Math.round(s.odometer.value), 0) : <QualityValue value={null} missingText="Mätarställning saknas" />}
          unit={s?.odometer.value != null ? "km" : undefined}
          sub={s?.odometer.value != null ? <>{s.odometer.source_label} · {formatDate(s.odometer.at)}</> : null}
        />
      )}
      <Card className="flex min-w-0 flex-col justify-between p-3.5 sm:p-4">
        <div className="flex items-center justify-between text-[13px] font-medium text-fg-muted">
          <span>Att granska</span>
          <ClipboardCheck className="size-4 text-fg-subtle" aria-hidden />
        </div>
        <div className="mt-2 flex flex-wrap items-end justify-between gap-2">
          <span className="text-[21px] leading-none font-medium num sm:text-[26px]">{reviewCount == null ? "–" : formatNumber(reviewCount, 0)}</span>
          {reviewCount ? (
            <Button asChild size="sm" variant="primary">
              <Link href="/review">
                Granska <ArrowRight aria-hidden />
              </Link>
            </Button>
          ) : (
            <span className="text-xs text-ok-ink">Allt är granskat</span>
          )}
        </div>
      </Card>
    </div>
  );
}

function StatusStrip({ s, showOdometer }: { s: DashboardSummary; showOdometer: boolean }) {
  const sync = s.sync;
  const items: React.ReactNode[] = [];
  if (showOdometer) {
    items.push(
      <span key="odo" className="inline-flex items-center gap-1.5">
        <Gauge className="size-3.5" aria-hidden /> Mätarställning{" "}
        <QualityValue mono className="text-fg" value={s.odometer.value != null ? formatOdometer(s.odometer.value) : null} missingText="Mätarställning saknas" />
        {s.odometer.at ? <span>({formatDate(s.odometer.at)})</span> : null}
      </span>,
    );
  }
  items.push(
    <span key="sync" className="inline-flex items-center gap-1.5">
      <RefreshCw className="size-3.5" aria-hidden />
      {sync.status === "not_configured" ? (
        <>
          Ingen fordonssynk –{" "}
          <Link href="/integrations" className="text-accent-ink hover:underline">
            koppla Volvo Cars
          </Link>
        </>
      ) : (
        <>
          {label(INTEGRATION_STATUS_LABELS, sync.status)}
          {sync.last_success_at ? <> · senast {formatRelative(sync.last_success_at)}</> : null}
        </>
      )}
    </span>,
  );
  if (s.position) {
    items.push(
      <Tooltip key="pos" content={`Rapporterad ${formatDateTime(s.position.reported_at ?? s.position.fetched_at)}`}>
        <span tabIndex={0} className="inline-flex items-center gap-1.5">
          <MapPin className="size-3.5" aria-hidden /> {s.position.place?.name ?? "Senast rapporterad position"}
          {s.position.reported_at || s.position.fetched_at ? <span>· {formatRelative(s.position.reported_at ?? s.position.fetched_at)}</span> : null}
        </span>
      </Tooltip>,
    );
  }
  return <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-xs text-fg-muted">{items}</div>;
}

export function DashboardView() {
  const [period, setPeriodState] = React.useState<PeriodValue>(() =>
    typeof window === "undefined" ? { key: "month" } : readStorage<PeriodValue>("session", PERIOD_KEY, { key: "month" }),
  );
  const setPeriod = (p: PeriodValue) => {
    setPeriodState(p);
    writeStorage("session", PERIOD_KEY, p);
  };
  const bucket = bucketFor(period);
  const summary = useDashboardSummary(period);
  const series = useDashboardTimeseries(period, bucket);
  const destinations = useDashboardDestinations(period, 6);
  const s = summary.data;
  const points = series.data?.points ?? [];
  const hasTrips = (s?.trips.total ?? 0) > 0;
  const showPrivate = points.some((p) => p.private_km > 0);
  const showUndecided = points.some((p) => p.undecided_km > 0);
  const hasEnergy = !!s && (s.fuel.verified > 0 || s.electricity.verified > 0);

  return (
    <>
      <PageHeader
        title="Översikt"
        description={s ? `${s.vehicle.name}${s.vehicle.registration_number ? ` · ${s.vehicle.registration_number}` : ""} · ${s.period.label}` : "Tjänstekörning och milersättning"}
        actions={<DateRangePicker value={period} onChange={setPeriod} />}
      />

      {summary.isError ? (
        <Card>
          <ErrorState error={summary.error} onRetry={() => summary.refetch()} />
        </Card>
      ) : (
        <>
          <KpiRow s={s} loading={!s} />
          {s ? <StatusStrip s={s} showOdometer={!!s.reimbursement && s.reimbursement.amount != null} /> : null}

          {s && !hasTrips ? (
            <Card className="mt-6">
              <EmptyState
                icon={<Briefcase />}
                title="Inga resor under perioden"
                description="Välj en annan period, eller importera körjournalen från Volvo Cars-appen."
                action={
                  <>
                    <Button onClick={() => setPeriod({ key: "year" })}>Visa hela året</Button>
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
            <div className="mt-6 grid gap-4 lg:grid-cols-5">
              <ChartCard
                className="lg:col-span-3"
                title="Tjänstekörning över tid"
                description={`Mil per ${bucket === "day" ? "dag" : bucket === "week" ? "vecka" : "månad"}`}
                legend={
                  showPrivate || showUndecided
                    ? [
                        { label: "Arbete", colorVar: "--work" },
                        ...(showUndecided ? [{ label: "Ej avgjord", colorVar: "--undecided" }] : []),
                        ...(showPrivate ? [{ label: "Privat", colorVar: "--private" }] : []),
                      ]
                    : undefined
                }
                loading={!series.data}
                error={series.error}
                onRetry={() => series.refetch()}
                empty={!points.length}
                table={<DataTable head={["Period", "Arbete", "Resor"]} rows={points.filter((p) => p.trips).map((p) => [bucket === "day" ? formatDate(p.bucket) : p.label, formatKm(p.work_km), p.trips])} />}
              >
                <DistanceChart points={points} bucket={bucket} showPrivate={showPrivate} showUndecided={showUndecided} />
              </ChartCard>
              <ChartCard
                className="lg:col-span-2"
                title="Vanligaste destinationerna"
                description="Körsträcka per mål"
                loading={!destinations.data}
                error={destinations.error}
                onRetry={() => destinations.refetch()}
                empty={!destinations.data?.items.length}
                table={<DataTable head={["Destination", "Sträcka", "Resor"]} rows={(destinations.data?.items ?? []).map((d) => [d.label, formatKm(d.total_km), d.trips])} />}
              >
                <DestinationsList items={destinations.data?.items ?? []} />
              </ChartCard>
              {hasEnergy && s ? (
                <Card className="p-5 lg:col-span-5">
                  <div className="grid gap-6 md:grid-cols-2">
                    <EnergyBlock kind="fuel" s={s} points={points} bucket={bucket} />
                    <EnergyBlock kind="el" s={s} points={points} bucket={bucket} />
                  </div>
                </Card>
              ) : null}
            </div>
          )}

          {s && hasTrips ? <MoreStats period={period} /> : null}
        </>
      )}
    </>
  );
}

function EnergyBlock({ kind, s, points, bucket }: { kind: "fuel" | "el"; s: DashboardSummary; points: TimeseriesPoint[]; bucket: "day" | "week" | "month" }) {
  const e = kind === "fuel" ? s.fuel : s.electricity;
  const coverageNote =
    e.coverage != null && e.coverage < 1 ? `Snittet bygger på ${formatPercent(e.coverage)} av sträckan (där förbrukningen är uppmätt).` : "Uppmätt av bilen.";
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <span className={kind === "fuel" ? "size-2.5 rounded-full bg-fuel" : "size-2.5 rounded-full bg-el"} aria-hidden />
          {kind === "fuel" ? "Bensin" : "El"}
        </h3>
        <p className="text-sm">
          <span className="num font-medium">{kind === "fuel" ? formatLiters(e.verified) : formatKwh(e.verified)}</span>
          <Tooltip content={coverageNote}>
            <span tabIndex={0} className="ml-2 cursor-help num text-xs text-fg-muted">
              {e.per_100km != null ? formatPer100(e.per_100km, kind === "fuel" ? "l" : "kWh") : "–"}
            </span>
          </Tooltip>
        </p>
      </div>
      <div className="mt-3 h-36">
        <EnergyMiniChart points={points} bucket={bucket} kind={kind} />
      </div>
    </div>
  );
}

function MoreStats({ period }: { period: PeriodValue }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div className="mt-6">
      <CollapsibleSection onToggle={setOpen} title="Mer statistik" meta="Resvägar, månadsjämförelse och förbrukning">
        {open ? <MoreStatsBody period={period} /> : null}
      </CollapsibleSection>
    </div>
  );
}

function MoreStatsBody({ period }: { period: PeriodValue }) {
  const bucket = bucketFor(period);
  const routes = useDashboardRoutes(period, 8);
  const monthly = useDashboardMonthly();
  const series = useDashboardTimeseries(period, bucket);
  const points = series.data?.points ?? [];
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <ChartCard
        title="Vanligaste resvägarna"
        loading={!routes.data}
        error={routes.error}
        empty={!routes.data?.items.length}
        height={260}
      >
        <DataTable
          head={["Resväg", "Resor", "Snitt", "Totalt"]}
          rows={(routes.data?.items ?? []).map((r) => [
            <span key="r" className="block max-w-[260px] truncate" title={`${r.origin} → ${r.destination}`}>
              {r.origin} → {r.destination}
            </span>,
            r.trips,
            formatKm(r.average_km),
            formatMil(r.total_km),
          ])}
        />
      </ChartCard>
      <ChartCard
        title="Månad för månad"
        description={monthly.data ? `Tjänstekörning ${monthly.data.year} jämfört med ${monthly.data.previous_year}` : undefined}
        legend={monthly.data ? [{ label: String(monthly.data.year), colorVar: "--work" }, { label: String(monthly.data.previous_year), colorVar: "--border-strong" }] : undefined}
        loading={!monthly.data}
        error={monthly.error}
        empty={!monthly.data?.months.some((m) => m.current.total_km || m.previous.total_km)}
        height={260}
        table={
          monthly.data ? (
            <DataTable head={["Månad", String(monthly.data.year), String(monthly.data.previous_year)]} rows={monthly.data.months.map((m) => [m.label, formatMil(m.current.work_km), formatMil(m.previous.work_km)])} />
          ) : undefined
        }
      >
        {monthly.data ? <MonthlyChart data={monthly.data} /> : null}
      </ChartCard>
      <ChartCard title="Bensinförbrukning över tid" description="l/100 km, endast uppmätta resor" loading={!series.data} empty={!points.some((p) => p.fuel_per_100km != null)} height={200}>
        <ConsumptionChart points={points} bucket={bucket} kind="fuel" />
      </ChartCard>
      <ChartCard title="Elförbrukning över tid" description="kWh/100 km, endast uppmätta resor" loading={!series.data} empty={!points.some((p) => p.energy_per_100km != null)} height={200}>
        <ConsumptionChart points={points} bucket={bucket} kind="el" />
      </ChartCard>
      <p className="text-xs text-fg-muted lg:col-span-2">
        Antal resor i perioden: {plural(points.reduce((a, p) => a + p.trips, 0), "resa", "resor")}.
      </p>
    </div>
  );
}
