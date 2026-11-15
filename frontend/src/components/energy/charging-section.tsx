"use client";

import * as React from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { QualityValue } from "@/components/trips/quality-value";
import { errorMessage } from "@/lib/api";
import type { ChargingRecord } from "@/lib/energy-types";
import { formatCurrency, formatDate, formatDuration, formatKwh, formatTime, plural } from "@/lib/format";
import type { PeriodValue } from "@/lib/period";
import { inBounds, periodBounds, useChargingRecords, useCreateCharging, useDeleteCharging } from "@/lib/queries/energy";
import { ChargingDialog } from "./charging-dialog";

function duration(r: ChargingRecord): string | null {
  if (!r.ended_at) return null;
  return formatDuration((new Date(r.ended_at).getTime() - new Date(r.started_at).getTime()) / 1000);
}

function Kwh({ r }: { r: ChargingRecord }) {
  return <QualityValue mono value={r.kwh != null ? formatKwh(r.kwh) : null} quality={r.kwh_quality} explanation={r.note ?? "Uppskattad mängd – inte avläst."} missingText="Mängd saknas" />;
}

/** Laddningar i perioden: lista, lägg till, ändra och ta bort. */
export function ChargingSection({ period, onShowAll }: { period: PeriodValue; onShowAll: () => void }) {
  const list = useChargingRecords(period);
  const create = useCreateCharging();
  const del = useDeleteCharging();
  const [dialog, setDialog] = React.useState<{ open: boolean; record: ChargingRecord | null }>({ open: false, record: null });

  const rows = React.useMemo(() => [...(list.data ?? [])].reverse(), [list.data]);
  const kwh = rows.reduce((sum, r) => sum + (r.kwh ?? 0), 0);
  const costs = rows.filter((r) => r.total_cost != null);
  const cost = costs.length ? costs.reduce((sum, r) => sum + (r.total_cost ?? 0), 0) : null;

  const onSaved = (saved: ChargingRecord, what: "created" | "updated") => {
    const outside = !inBounds(saved.started_at, periodBounds(period));
    toast.success(
      what === "created" ? "Laddningen är sparad." : "Laddningen är uppdaterad.",
      outside ? { description: "Den ligger utanför vald period och visas inte i listan.", action: { label: "Visa alla", onClick: onShowAll } } : undefined,
    );
  };

  const restore = (r: ChargingRecord) => {
    if (r.kwh == null) return;
    create.mutate(
      {
        started_at: r.started_at,
        ended_at: r.ended_at,
        kwh: r.kwh,
        kwh_quality: r.kwh_quality === "estimated" ? "estimated" : "verified",
        total_cost: r.total_cost,
        location: r.location,
        note: r.note,
      },
      { onSuccess: () => toast.success("Laddningen är återställd."), onError: (e) => toast.error(errorMessage(e)) },
    );
  };

  const remove = (r: ChargingRecord) => {
    del.mutate(r.id, {
      onSuccess: () => toast.success("Laddningen är borttagen.", r.kwh != null ? { action: { label: "Ångra", onClick: () => restore(r) } } : undefined),
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  const rowActions = (r: ChargingRecord) => {
    const when = formatDate(r.started_at);
    return (
      <div className="flex items-center justify-end gap-0.5">
        <Button size="icon-xs" variant="ghost" onClick={() => setDialog({ open: true, record: r })} aria-label={`Ändra laddningen ${when}`}>
          <Pencil />
        </Button>
        <Button size="icon-xs" variant="ghost" onClick={() => remove(r)} disabled={del.isPending} aria-label={`Ta bort laddningen ${when}`}>
          <Trash2 />
        </Button>
      </div>
    );
  };

  return (
    <Card data-testid="charging-section">
      <CardHeader className="flex-wrap">
        <div className="min-w-0">
          <CardTitle>Laddningar</CardTitle>
          <CardDescription>
            {rows.length ? (
              <>
                {plural(rows.length, "laddning", "laddningar")} · <span className="font-mono">{formatKwh(kwh)}</span>
                {cost != null ? (
                  <>
                    {" "}
                    · <span className="font-mono">{formatCurrency(cost)}</span>
                  </>
                ) : null}
              </>
            ) : (
              "Frivilligt – för dig som vill följa elkostnaden."
            )}
          </CardDescription>
        </div>
        <Button size="sm" variant="primary" onClick={() => setDialog({ open: true, record: null })}>
          <Plus aria-hidden /> Lägg till laddning
        </Button>
      </CardHeader>

      {list.isError ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} compact />
      ) : !list.data ? (
        <SkeletonBlock rows={3} className="px-5 pb-5" />
      ) : rows.length === 0 ? (
        <EmptyState
          compact
          title="Inga laddningar i perioden"
          description="Elförbrukningen ovan kommer från bilens körjournal. Lägg till laddningar om du vill se vad elen har kostat."
          className="border-t border-border py-7"
        />
      ) : (
        <>
          <Table wrapperClassName="hidden border-t border-border sm:block">
            <THead>
              <TR className="hover:bg-transparent">
                <TH className="pl-5">Start</TH>
                <TH>Längd</TH>
                <TH className="text-right">Laddat</TH>
                <TH className="text-right">Belopp</TH>
                <TH>Plats</TH>
                <TH className="w-20 pr-5">
                  <span className="sr-only">Åtgärder</span>
                </TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((r) => (
                <TR key={r.id} data-testid="charging-row">
                  <TD className="pl-5 whitespace-nowrap">
                    <span className="font-mono">{formatDate(r.started_at)}</span> <span className="font-mono text-xs text-fg-muted">{formatTime(r.started_at)}</span>
                  </TD>
                  <TD className="whitespace-nowrap">
                    <QualityValue mono value={duration(r)} missingText="Ingen sluttid angiven" />
                  </TD>
                  <TD className="text-right">
                    <Kwh r={r} />
                  </TD>
                  <TD className="text-right">
                    <QualityValue mono value={r.total_cost != null ? formatCurrency(r.total_cost) : null} missingText="Inget belopp angivet" />
                  </TD>
                  <TD className="max-w-56 truncate" title={r.location ?? undefined}>
                    {r.location ?? <span className="text-fg-subtle">–</span>}
                  </TD>
                  <TD className="pr-5">{rowActions(r)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>

          <ul className="divide-y divide-border border-t border-border sm:hidden">
            {rows.map((r) => (
              <li key={r.id} className="flex items-start gap-3 px-4 py-3" data-testid="charging-row-mobile">
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-center gap-x-2 text-sm">
                    <span className="font-mono font-medium">{formatDate(r.started_at)}</span>
                    <span className="font-mono text-xs text-fg-muted">{formatTime(r.started_at)}</span>
                  </p>
                  <p className="mt-1 flex flex-wrap items-baseline gap-x-2 text-sm">
                    <span className="font-medium">
                      <Kwh r={r} />
                    </span>
                    {r.total_cost != null ? <span className="font-mono text-fg-muted">{formatCurrency(r.total_cost)}</span> : null}
                  </p>
                  {r.location || duration(r) ? <p className="mt-0.5 truncate text-xs text-fg-muted">{[r.location, duration(r)].filter(Boolean).join(" · ")}</p> : null}
                </div>
                {rowActions(r)}
              </li>
            ))}
          </ul>
        </>
      )}

      <ChargingDialog open={dialog.open} onOpenChange={(open) => setDialog((d) => ({ ...d, open }))} record={dialog.record} onSaved={onSaved} />
    </Card>
  );
}
