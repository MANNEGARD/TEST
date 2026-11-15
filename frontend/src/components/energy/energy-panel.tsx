"use client";

import * as React from "react";
import { Fuel, Zap } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { QualityValue } from "@/components/trips/quality-value";
import type { EnergySummary } from "@/lib/energy-types";
import { formatKwh, formatLiters, formatMil, formatNumber, formatPercent } from "@/lib/format";
import type { EnergyTotals } from "@/lib/types";
import { cn } from "@/lib/utils";

export type EnergyKind = "fuel" | "el";

const KIND = {
  fuel: { title: "Bensin", icon: Fuel, dot: "bg-fuel", track: "bg-fuel-soft", fill: "bg-fuel", unit: "liter", per: "l/100 km" },
  el: { title: "El", icon: Zap, dot: "bg-el", track: "bg-el-soft", fill: "bg-el", unit: "kWh", per: "kWh/100 km" },
} as const;

function Figure({ label, unit, children }: { label: string; unit: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="mt-1.5 flex items-baseline gap-1.5">
        <span className="font-mono text-[21px] leading-none font-medium tracking-tight text-fg num sm:text-[26px]">{children}</span>
        <span className="text-sm whitespace-nowrap text-fg-muted">{unit}</span>
      </dd>
    </div>
  );
}

/**
 * Hur stor del av sträckan som har uppmätt förbrukning. Mätaren är en andel mot en
 * helhet: fyllnad i energifärgen på ett spår i samma färgskala.
 */
function Coverage({ kind, e }: { kind: EnergyKind; e: EnergyTotals }) {
  const k = KIND[kind];
  const totalKm = e.verified_km + e.estimated_km + e.unknown_km;
  const amount = (v: number) => (kind === "fuel" ? formatLiters(v) : formatKwh(v));
  if (e.coverage == null) {
    return <p className="text-xs text-fg-muted">Inga resor i perioden.</p>;
  }
  const pct = Math.round(e.coverage * 100);
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="text-fg-muted">Uppmätt förbrukning</span>
        <span className="text-fg">
          <span className="font-mono">{formatPercent(e.coverage)}</span> av sträckan
        </span>
      </div>
      <div
        role="meter"
        aria-label={`Andel av sträckan med uppmätt ${kind === "fuel" ? "bensinförbrukning" : "elförbrukning"}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={`${pct} procent`}
        className={cn("mt-1.5 h-1.5 overflow-hidden rounded-full", k.track)}
      >
        <div className={cn("h-full rounded-full", k.fill)} style={{ width: `${Math.max(e.coverage > 0 ? 2 : 0, e.coverage * 100)}%` }} />
      </div>
      <p className="mt-2 text-xs leading-relaxed text-fg-muted">
        <span className="font-mono">{formatMil(e.verified_km)}</span> av <span className="font-mono">{formatMil(totalKm)}</span> har förbrukning uppmätt av bilen.
        {e.estimated_km > 0 ? (
          <>
            {" "}
            <QualityValue mono value={amount(e.estimated)} quality="estimated" explanation="Beräknad förbrukning – inte uppmätt. Räknas inte in i summan eller snittet." /> är uppskattat för{" "}
            <span className="font-mono">{formatMil(e.estimated_km)}</span>.
          </>
        ) : null}
        {e.unknown_km > 0 ? (
          <>
            {" "}
            För <span className="font-mono">{formatMil(e.unknown_km)}</span> saknas uppgift.
          </>
        ) : null}
      </p>
    </div>
  );
}

/** Nyckeltal för bensin eller el: mängd, snitt per 100 km (och regenererat för el) samt täckningsgrad. */
export function EnergyPanel({ kind, summary, className }: { kind: EnergyKind; summary: EnergySummary | undefined; className?: string }) {
  const k = KIND[kind];
  const Icon = k.icon;
  const e = summary ? (kind === "fuel" ? summary.fuel : summary.electricity) : undefined;
  const regen = summary?.electricity.regenerated_kwh ?? 0;
  const onlyEstimated = !!e && e.verified <= 0 && e.estimated > 0;

  return (
    <Card className={cn("flex flex-col p-4 sm:p-5", className)} data-testid={`energy-panel-${kind}`}>
      <div className="flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
          <span className={cn("size-2.5 rounded-full", k.dot)} aria-hidden />
          {k.title}
        </h2>
        <Icon className="size-4 text-fg-subtle" aria-hidden />
      </div>

      {!e ? (
        <div className="mt-4 space-y-4" role="status" aria-label="Laddar nyckeltal">
          <Skeleton className="h-10 w-3/4" />
          <Skeleton className="h-9 w-full" />
        </div>
      ) : (
        <>
          <dl className={cn("mt-4 grid gap-x-6 gap-y-4", kind === "el" ? "grid-cols-2 lg:grid-cols-3" : "grid-cols-2")}>
            <Figure label="Förbrukat" unit={k.unit}>
              {e.verified > 0 ? (
                formatNumber(e.verified, 1)
              ) : (
                <QualityValue
                  value={onlyEstimated ? formatNumber(e.estimated, 1) : null}
                  quality={onlyEstimated ? "estimated" : "unknown"}
                  explanation={onlyEstimated ? "Beräknad förbrukning – inte uppmätt av bilen." : "Ingen uppmätt förbrukning i perioden."}
                />
              )}
            </Figure>
            <Figure label="Snitt" unit={k.per}>
              <QualityValue value={e.per_100km != null ? formatNumber(e.per_100km, 1, 1) : null} missingText="Snittet räknas bara på sträcka med uppmätt förbrukning." />
            </Figure>
            {kind === "el" ? (
              <Figure label="Regenererat" unit="kWh">
                <QualityValue value={regen > 0 ? formatNumber(regen, 1) : null} missingText="Ingen regenererad energi rapporterad i perioden." />
              </Figure>
            ) : null}
          </dl>
          <div className="mt-auto pt-5">
            <div className="border-t border-border pt-3.5">
              <Coverage kind={kind} e={e} />
            </div>
          </div>
        </>
      )}
    </Card>
  );
}
