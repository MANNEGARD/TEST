"use client";

import * as React from "react";
import { Check, Pencil, Plus, Sparkles, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tooltip } from "@/components/ui/tooltip";
import { QualityValue } from "@/components/trips/quality-value";
import { errorMessage } from "@/lib/api";
import type { FuelRecord } from "@/lib/energy-types";
import { formatCurrency, formatDate, formatLiters, formatNumber, formatOdometer, formatTime, plural } from "@/lib/format";
import type { PeriodValue } from "@/lib/period";
import { inBounds, periodBounds, useCreateFuel, useDeleteFuel, useDetectRefuels, useFuelRecords } from "@/lib/queries/energy";
import { FuelDialog, type SavedKind } from "./fuel-dialog";

const SAVED_TEXT: Record<SavedKind, string> = {
  created: "Tankningen är sparad.",
  updated: "Tankningen är uppdaterad.",
  confirmed: "Tankningen är bekräftad.",
};

const ESTIMATE_NOTE = "Uppskattad volym ur tanknivån – inte avläst från kvitto.";

function isSuggestion(r: FuelRecord): boolean {
  return r.source === "detected";
}

function Liters({ r }: { r: FuelRecord }) {
  return <QualityValue mono value={r.liters != null ? formatLiters(r.liters) : null} quality={r.liters_quality} explanation={r.note ?? ESTIMATE_NOTE} missingText="Volym saknas" />;
}

function pricePerLiter(r: FuelRecord): string | null {
  const p = r.price_per_liter ?? (r.total_cost != null && r.liters ? r.total_cost / r.liters : null);
  return p != null ? `${formatNumber(p, 2, 2)} kr/l` : null;
}

function SuggestionBadge() {
  return (
    <Tooltip content="Föreslagen ur bilens tanknivå. Volymen är en uppskattning tills du bekräftar den.">
      <Badge tone="accent" size="sm" tabIndex={0} className="cursor-help">
        <Sparkles aria-hidden /> Förslag
      </Badge>
    </Tooltip>
  );
}

/** Tankningar i perioden: lista, lägg till, ändra, ta bort samt förslag ur tanknivån. */
export function FuelSection({ period, onShowAll }: { period: PeriodValue; onShowAll: () => void }) {
  const list = useFuelRecords(period);
  const create = useCreateFuel();
  const del = useDeleteFuel();
  const detect = useDetectRefuels();
  const [dialog, setDialog] = React.useState<{ open: boolean; record: FuelRecord | null }>({ open: false, record: null });

  const rows = React.useMemo(() => [...(list.data ?? [])].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime()), [list.data]);
  const suggestions = rows.filter(isSuggestion).length;
  const liters = rows.reduce((sum, r) => sum + (r.liters ?? 0), 0);
  const costs = rows.filter((r) => r.total_cost != null);
  const cost = costs.length ? costs.reduce((sum, r) => sum + (r.total_cost ?? 0), 0) : null;

  const outsideNote = (count: number) =>
    count
      ? { description: count === 1 ? "Den ligger utanför vald period och visas inte i listan." : `${count} ligger utanför vald period och visas inte i listan.`, action: { label: "Visa alla", onClick: onShowAll } }
      : undefined;

  const onSaved = (saved: FuelRecord, what: SavedKind) => {
    toast.success(SAVED_TEXT[what], outsideNote(inBounds(saved.at, periodBounds(period)) ? 0 : 1));
  };

  const restore = (r: FuelRecord) => {
    if (r.liters == null) return;
    create.mutate(
      {
        at: r.at,
        liters: r.liters,
        liters_quality: r.liters_quality === "estimated" ? "estimated" : "verified",
        total_cost: r.total_cost,
        price_per_liter: r.price_per_liter,
        odometer_km: r.odometer_km,
        station: r.station,
        full_tank: r.full_tank,
        note: r.note,
      },
      { onSuccess: () => toast.success("Tankningen är återställd."), onError: (e) => toast.error(errorMessage(e)) },
    );
  };

  const remove = (r: FuelRecord) => {
    del.mutate(r.id, {
      onSuccess: () =>
        toast.success(isSuggestion(r) ? "Förslaget är borttaget." : "Tankningen är borttagen.", r.liters != null ? { action: { label: "Ångra", onClick: () => restore(r) } } : undefined),
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  const runDetect = () => {
    detect.mutate(undefined, {
      onSuccess: (res) => {
        if (!res.created) {
          toast.info("Inga nya tankningar hittades.", { description: "Förslagen bygger på tanknivån som bilen rapporterar via Volvo Cars." });
          return;
        }
        const bounds = periodBounds(period);
        const outside = res.items.filter((i) => !inBounds(i.at, bounds)).length;
        toast.success(res.created === 1 ? "1 förslag på tankning lades till." : `${formatNumber(res.created, 0)} förslag på tankningar lades till.`, {
          description: outside ? outsideNote(outside)?.description : "Volymen är uppskattad – bekräfta eller ta bort förslagen i listan.",
          action: outsideNote(outside)?.action,
        });
      },
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  const rowActions = (r: FuelRecord) => {
    const when = formatDate(r.at);
    return (
      <div className="flex items-center justify-end gap-0.5">
        {isSuggestion(r) ? (
          <Button size="xs" variant="secondary" onClick={() => setDialog({ open: true, record: r })} aria-label={`Bekräfta tankningen ${when}`}>
            <Check aria-hidden /> Bekräfta
          </Button>
        ) : (
          <Button size="icon-xs" variant="ghost" onClick={() => setDialog({ open: true, record: r })} aria-label={`Ändra tankningen ${when}`}>
            <Pencil />
          </Button>
        )}
        <Button size="icon-xs" variant="ghost" onClick={() => remove(r)} disabled={del.isPending} aria-label={`${isSuggestion(r) ? "Ta bort förslaget" : "Ta bort tankningen"} ${when}`}>
          <Trash2 />
        </Button>
      </div>
    );
  };

  return (
    <Card data-testid="fuel-section">
      <CardHeader className="flex-wrap">
        <div className="min-w-0">
          <CardTitle>Tankningar</CardTitle>
          <CardDescription>
            {rows.length ? (
              <>
                {plural(rows.length, "tankning", "tankningar")} · <span className="num">{formatLiters(liters)}</span>
                {cost != null ? (
                  <>
                    {" "}
                    · <span className="num">{formatCurrency(cost)}</span>
                  </>
                ) : null}
                {suggestions ? <> · {plural(suggestions, "förslag", "förslag")} att bekräfta</> : null}
              </>
            ) : (
              "Frivilligt – för dig som vill följa bränslekostnaden."
            )}
          </CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Tooltip content="Letar efter tillfällen där tanknivån ökat mellan två fordonsrapporter. Förslagen märks som uppskattade tills du bekräftar dem.">
            <Button size="sm" variant="ghost" onClick={runDetect} loading={detect.isPending}>
              {detect.isPending ? null : <Sparkles aria-hidden />} Föreslå ur tanknivå
            </Button>
          </Tooltip>
          <Button size="sm" variant="primary" onClick={() => setDialog({ open: true, record: null })}>
            <Plus aria-hidden /> Lägg till tankning
          </Button>
        </div>
      </CardHeader>

      {list.isError ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} compact />
      ) : !list.data ? (
        <SkeletonBlock rows={3} className="px-5 pb-5" />
      ) : rows.length === 0 ? (
        <EmptyState
          compact
          title="Inga tankningar i perioden"
          description="Förbrukningen ovan kommer från bilens körjournal och kräver inga tankningar. Lägg till dem om du vill se vad bränslet har kostat."
          className="border-t border-border py-7"
        />
      ) : (
        <>
          <Table wrapperClassName="hidden border-t border-border sm:block">
            <THead>
              <TR className="hover:bg-transparent">
                <TH className="pl-5">Datum</TH>
                <TH className="text-right">Volym</TH>
                <TH className="text-right">Belopp</TH>
                <TH className="text-right">Literpris</TH>
                <TH className="text-right">Mätarställning</TH>
                <TH>Station</TH>
                <TH className="w-28 pr-5">
                  <span className="sr-only">Åtgärder</span>
                </TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((r) => (
                <TR key={r.id} data-testid="fuel-row">
                  <TD className="pl-5 whitespace-nowrap">
                    <span className="num">{formatDate(r.at)}</span> <span className="num text-xs text-fg-muted">{formatTime(r.at)}</span>
                    {isSuggestion(r) ? <span className="ml-2 align-middle"><SuggestionBadge /></span> : null}
                  </TD>
                  <TD className="text-right">
                    <Liters r={r} />
                    {r.full_tank ? <span className="ml-1.5 text-xs text-fg-muted">full tank</span> : null}
                  </TD>
                  <TD className="text-right">
                    <QualityValue mono value={r.total_cost != null ? formatCurrency(r.total_cost) : null} missingText="Inget belopp angivet" />
                  </TD>
                  <TD className="text-right">
                    <QualityValue mono value={pricePerLiter(r)} missingText="Literpris saknas – inget belopp angivet" />
                  </TD>
                  <TD className="text-right">
                    <QualityValue mono value={r.odometer_km != null ? formatOdometer(r.odometer_km) : null} missingText="Ingen mätarställning angiven" />
                  </TD>
                  <TD className="max-w-56 truncate" title={r.station ?? undefined}>
                    {r.station ?? <span className="text-fg-subtle">–</span>}
                  </TD>
                  <TD className="pr-5">{rowActions(r)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>

          <ul className="divide-y divide-border border-t border-border sm:hidden">
            {rows.map((r) => (
              <li key={r.id} className="flex items-start gap-3 px-4 py-3" data-testid="fuel-row-mobile">
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                    <span className="num font-medium">{formatDate(r.at)}</span>
                    <span className="num text-xs text-fg-muted">{formatTime(r.at)}</span>
                    {isSuggestion(r) ? <SuggestionBadge /> : null}
                  </p>
                  <p className="mt-1 flex flex-wrap items-baseline gap-x-2 text-sm">
                    <span className="font-medium">
                      <Liters r={r} />
                    </span>
                    {r.total_cost != null ? <span className="num text-fg-muted">{formatCurrency(r.total_cost)}</span> : null}
                    {pricePerLiter(r) ? <span className="num text-xs text-fg-muted">{pricePerLiter(r)}</span> : null}
                  </p>
                  {r.station || r.odometer_km != null || r.full_tank ? (
                    <p className="mt-0.5 truncate text-xs text-fg-muted">
                      {[r.station, r.odometer_km != null ? formatOdometer(r.odometer_km) : null, r.full_tank ? "full tank" : null].filter(Boolean).join(" · ")}
                    </p>
                  ) : null}
                </div>
                {rowActions(r)}
              </li>
            ))}
          </ul>
        </>
      )}

      <FuelDialog open={dialog.open} onOpenChange={(open) => setDialog((d) => ({ ...d, open }))} record={dialog.record} onSaved={onSaved} />
    </Card>
  );
}
