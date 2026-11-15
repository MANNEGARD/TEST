"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  ArrowRight,
  Brain,
  CalendarDays,
  FileSpreadsheet,
  History,
  Layers,
  PenLine,
  Route,
  Scissors,
  Split,
  Undo2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmInline } from "@/components/ui/confirm-inline";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { DescriptionItem, DescriptionList } from "@/components/ui/dl";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { TripMap } from "@/components/map/trip-map";
import { CategoryPill } from "@/components/trips/category-pill";
import { QualityValue } from "@/components/trips/quality-value";
import { SourceBadge } from "@/components/trips/source-badge";
import { TripTime } from "@/components/trips/trip-time";
import { ApiError, errorMessage } from "@/lib/api";
import {
  formatDate,
  formatDateTime,
  formatDuration,
  formatKm,
  formatKwh,
  formatLiters,
  formatMil,
  formatOdometer,
  formatPercent,
  formatTime,
  formatWeekday,
  plural,
} from "@/lib/format";
import {
  AUDIT_ACTION_LABELS,
  CATEGORY_LABELS,
  DISTANCE_QUALITY_LABELS,
  EVIDENCE_DIRECTION_LABELS,
  label,
  MERGE_KIND_LABELS,
  MERGE_OPERATION_LABELS,
  QUALITY_FLAG_LABELS,
  TRIP_SOURCE_LABELS,
} from "@/lib/labels";
import { useUndoMergeOperation, useUndoSplit } from "@/lib/queries/review";
import {
  useCalendarMatchChoice,
  useClearAllocation,
  useDeleteJourney,
  useSplitTrip,
  useTripDetail,
  useUndoFeedback,
  useUnmerge,
  useUpdateJourney,
} from "@/lib/queries/trips";
import type { RawTrip, TripDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AllocationDialog } from "./allocation-dialog";
import { ClassificationCard } from "./classification-card";

function energyExplanation(kind: "fuel" | "el", quality: string): string | undefined {
  if (quality === "estimated") return kind === "fuel" ? "Uppskattad bensinförbrukning – inte uppmätt av bilen." : "Uppskattad elförbrukning – inte uppmätt av bilen.";
  if (quality === "unknown") return kind === "fuel" ? "Bensinförbrukning saknas för resan." : "Elförbrukning saknas för resan.";
  return undefined;
}

function Facts({ d }: { d: TripDetail }) {
  const t = d.trip;
  const firstRaw = d.raw_trips[0];
  const estimatedDistance = t.quality_flags.includes("distance_estimated");
  return (
    <Card>
      <CardContent className="pt-5">
        <DescriptionList>
          <DescriptionItem label="Sträcka">
            <span className="num text-lg font-medium">
              <QualityValue value={formatMil(t.distance_km)} quality={estimatedDistance ? "estimated" : "actual"} explanation="Sträckan är uppskattad." />
            </span>
            <span className="ml-1.5 num text-xs text-fg-muted">{formatKm(t.distance_km)}</span>
          </DescriptionItem>
          <DescriptionItem label="Körtid">
            <QualityValue value={t.driving_seconds != null ? formatDuration(t.driving_seconds) : null} missingText="Körtid saknas" />
          </DescriptionItem>
          <DescriptionItem label="Tid">
            <span className="num">
              <TripTime at={t.started_at} quality={t.start_time_quality} earliest={firstRaw?.start_earliest} latest={firstRaw?.start_latest} />
              {" – "}
              <TripTime at={t.ended_at} quality={t.end_time_quality} />
            </span>
          </DescriptionItem>
          <DescriptionItem label="Mätare start">
            <QualityValue mono value={t.odometer_start != null ? formatOdometer(t.odometer_start) : null} missingText="Mätarställning saknas" />
          </DescriptionItem>
          <DescriptionItem label="Mätare slut">
            <QualityValue mono value={t.odometer_end != null ? formatOdometer(t.odometer_end) : null} missingText="Mätarställning saknas" />
          </DescriptionItem>
          <DescriptionItem label="Källa">
            <SourceBadge source={t.source} short />
          </DescriptionItem>
          <DescriptionItem label="Bensin">
            <span className="inline-flex items-center gap-1.5">
              <span className="size-2 rounded-full bg-fuel" aria-hidden />
              <QualityValue mono value={t.fuel_liters != null ? formatLiters(t.fuel_liters) : null} quality={t.fuel_quality} explanation={energyExplanation("fuel", t.fuel_quality)} />
            </span>
          </DescriptionItem>
          <DescriptionItem label="El">
            <span className="inline-flex items-center gap-1.5">
              <span className="size-2 rounded-full bg-el" aria-hidden />
              <QualityValue mono value={t.energy_kwh != null ? formatKwh(t.energy_kwh) : null} quality={t.energy_quality} explanation={energyExplanation("el", t.energy_quality)} />
            </span>
          </DescriptionItem>
          {t.regen_kwh != null ? (
            <DescriptionItem label="Återvunnet">
              <span className="num">{formatKwh(t.regen_kwh)}</span>
            </DescriptionItem>
          ) : null}
        </DescriptionList>
        {t.quality_flags.length ? (
          <div className="mt-4 flex flex-wrap gap-1.5 border-t border-border pt-3">
            {t.quality_flags.map((f) => (
              <Badge key={f} tone="neutral" size="sm">
                {label(QUALITY_FLAG_LABELS, f)}
              </Badge>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Stops({ d }: { d: TripDetail }) {
  const stops = d.map.stops.length ? d.map.stops : [{ address: d.trip.start_address, at: d.trip.started_at, location: d.trip.start_location ?? null }, { address: d.trip.end_address, at: d.trip.ended_at, location: d.trip.end_location ?? null }];
  return (
    <ol className="relative space-y-3" aria-label="Start, stopp och mål">
      {stops.map((s, i) => {
        const kind = i === 0 ? "Start" : i === stops.length - 1 ? "Mål" : "Stopp";
        const name = s.location?.confirmed ? s.location.name : s.address;
        return (
          <li key={i} className="relative flex gap-3 pl-0.5">
            <span className="relative flex flex-col items-center">
              <span className={cn("mt-1 size-2.5 rounded-full ring-2 ring-surface", i === 0 ? "bg-ok" : i === stops.length - 1 ? "bg-err" : "bg-warn")} aria-hidden />
              {i < stops.length - 1 ? <span className="mt-1 w-px flex-1 bg-border-strong" aria-hidden /> : null}
            </span>
            <div className="min-w-0 pb-1">
              <p className="text-xs text-fg-muted">
                {kind}
                {s.at ? <span className="ml-1.5 num">{formatTime(s.at)}</span> : null}
              </p>
              <p className="truncate text-sm font-medium">{name ?? <span className="text-fg-subtle">Adress saknas</span>}</p>
              {s.location?.confirmed && s.address && s.address !== name ? <p className="truncate text-xs text-fg-muted">{s.address}</p> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function CalendarMatches({ d }: { d: TripDetail }) {
  const choose = useCalendarMatchChoice(d.trip.id);
  if (!d.calendar_matches.length) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2">
          <CalendarDays className="size-4 text-fg-subtle" aria-hidden /> Möten i Outlook
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {d.calendar_matches.map((m) => (
          <div key={m.id} className={cn("rounded-md border p-3", m.is_selected ? "border-accent/50 bg-accent-soft/50" : "border-border")}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{m.subject ?? "Möte utan rubrik"}</p>
                <p className="text-xs text-fg-muted">
                  <span className="num">
                    {formatTime(m.start_at)}–{formatTime(m.end_at)}
                  </span>
                  {m.location ? ` · ${m.location}` : m.is_online ? " · Onlinemöte" : ""} · matchning {formatPercent(m.score)}
                </p>
                {m.is_conflict ? <p className="mt-1 text-xs text-warn-ink">Tid eller plats stämmer inte helt med fordonsdata.</p> : null}
              </div>
              <div className="flex shrink-0 gap-1">
                <Button
                  size="xs"
                  variant={m.is_selected ? "primary" : "secondary"}
                  onClick={() => choose.mutate({ matchId: m.id, selected: true }, { onError: (e) => toast.error(errorMessage(e)) })}
                  aria-pressed={m.is_selected}
                >
                  {m.is_selected ? "Vald" : "Välj"}
                </Button>
                <Button size="xs" variant="ghost" onClick={() => choose.mutate({ matchId: m.id, selected: false }, { onError: (e) => toast.error(errorMessage(e)) })}>
                  Avvisa
                </Button>
              </div>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function Reasoning({ d }: { d: TripDetail }) {
  const a = d.assessment;
  if (!a && !d.evidence.length && !d.trip.tax_note) return null;
  return (
    <CollapsibleSection title="Varför detta förslag?" icon={<Brain />} meta={a ? `${a.category} · ${formatPercent(a.confidence)}` : undefined}>
      <div className="space-y-4 text-sm">
        {a ? (
          <p className="text-fg-muted">
            Bedömning: <strong className="text-fg">{a.category}</strong> med {formatPercent(a.confidence)} säkerhet
            {a.model ? ` (${a.model})` : a.source === "rule" ? " (regler)" : ""}. {a.requires_review ? "Kräver ditt godkännande." : ""}
            {a.purpose_rationale ? ` ${a.purpose_rationale}` : ""}
          </p>
        ) : null}
        {d.evidence.length ? (
          <ul className="space-y-1.5">
            {d.evidence.map((e, i) => (
              <li key={i} className="flex items-start gap-2.5">
                <span
                  className={cn(
                    "mt-0.5 inline-flex shrink-0 items-center rounded-sm px-1.5 text-[11px] font-medium",
                    e.direction === "work" ? "bg-work-soft text-work-ink" : e.direction === "private" ? "bg-private-soft text-private-ink" : "bg-surface-2 text-fg-muted",
                  )}
                  title={label(EVIDENCE_DIRECTION_LABELS, e.direction)}
                >
                  {e.direction === "work" ? "Arbete" : e.direction === "private" ? "Privat" : "Neutral"}
                </span>
                <span className="flex-1">{e.label}</span>
                {e.weight ? <span className="num text-xs text-fg-subtle">vikt {e.weight.toFixed(2).replace(".", ",")}</span> : null}
              </li>
            ))}
          </ul>
        ) : null}
        {a?.caps.length ? (
          <div className="flex flex-wrap gap-1.5">
            {a.caps.map((c) => (
              <Badge key={c} tone="neutral">
                {c}
              </Badge>
            ))}
          </div>
        ) : null}
        {d.trip.tax_note ? <p className="rounded-md bg-surface-2 p-3 text-fg-muted">{d.trip.tax_note}</p> : null}
      </div>
    </CollapsibleSection>
  );
}

function RawTrips({ d, onAllocate }: { d: TripDetail; onAllocate: (raw: RawTrip) => void }) {
  const split = useSplitTrip();
  const undoSplit = useUndoSplit();
  const clear = useClearAllocation();
  const router = useRouter();
  const merged = d.raw_trips.length > 1;
  return (
    <CollapsibleSection
      title={merged ? "Delresor och fördelning" : "Originalresa och fördelning"}
      icon={<FileSpreadsheet />}
      meta={merged ? `${d.raw_trips.length} delresor` : undefined}
      defaultOpen={d.trip.has_mixed_categories}
    >
      <ol className="space-y-3">
        {d.raw_trips.map((r, i) => {
          const segs = d.segments.filter((s) => s.raw_trip_id === r.id);
          const allocated = segs.length > 1;
          return (
            <li key={r.id}>
              <div className="rounded-md border border-border p-3.5">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-medium">
                      <span className="num">
                        <TripTime at={r.started_at} quality={r.start_time_quality} earliest={r.start_earliest} latest={r.start_latest} />–
                        <TripTime at={r.ended_at} quality={r.end_time_quality} />
                      </span>
                      <span className="mx-2 text-fg-subtle">·</span>
                      <QualityValue mono value={formatKm(r.distance_km)} quality={r.distance_quality === "estimated" ? "estimated" : "actual"} explanation={label(DISTANCE_QUALITY_LABELS, r.distance_quality)} />
                    </p>
                    <p className="mt-0.5 truncate text-xs text-fg-muted">
                      {r.start_address ?? "–"} → {r.end_address ?? "–"}
                    </p>
                  </div>
                  <div className="flex gap-1">
                    <Button size="xs" variant="secondary" onClick={() => onAllocate(r)}>
                      <Split aria-hidden /> Fördela
                    </Button>
                    {allocated ? (
                      <Button
                        size="xs"
                        variant="ghost"
                        onClick={() => clear.mutate({ id: d.trip.id, rawTripId: r.id }, { onSuccess: () => toast.success("Fördelningen är borttagen."), onError: (e) => toast.error(errorMessage(e)) })}
                      >
                        Ta bort fördelning
                      </Button>
                    ) : null}
                  </div>
                </div>
                {allocated ? (
                  <div className="mt-2.5 flex flex-wrap gap-1.5">
                    {segs.map((s) => (
                      <span key={s.id} className="inline-flex items-center gap-1.5 text-xs">
                        <CategoryPill category={s.category} size="sm" />
                        <span className="num">{formatKm(s.distance_km)}</span>
                        {s.note ? <span className="text-fg-muted">({s.note})</span> : null}
                      </span>
                    ))}
                  </div>
                ) : null}
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 border-t border-border pt-2.5 text-xs sm:grid-cols-4">
                  <div>
                    <dt className="text-fg-muted">Källa</dt>
                    <dd>{label(TRIP_SOURCE_LABELS, r.source)}</dd>
                  </div>
                  <div>
                    <dt className="text-fg-muted">Ursprunglig kategori</dt>
                    <dd>{r.original_category ?? "–"}</dd>
                  </div>
                  <div>
                    <dt className="text-fg-muted">Mätarställning</dt>
                    <dd className="num">
                      {r.odometer_start != null || r.odometer_end != null ? (
                        <>
                          {r.odometer_start != null ? formatOdometer(r.odometer_start) : "–"} – {r.odometer_end != null ? formatOdometer(r.odometer_end) : "–"}
                        </>
                      ) : (
                        <span className="text-fg-subtle">Saknas</span>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-fg-muted">Bensin / el</dt>
                    <dd className="num">
                      <QualityValue value={r.fuel_liters != null ? formatLiters(r.fuel_liters) : null} quality={r.fuel_quality} explanation={energyExplanation("fuel", r.fuel_quality)} />
                      <span className="mx-1 text-fg-subtle">/</span>
                      <QualityValue value={r.energy_kwh != null ? formatKwh(r.energy_kwh) : null} quality={r.energy_quality} explanation={energyExplanation("el", r.energy_quality)} />
                    </dd>
                  </div>
                  <div className="col-span-2">
                    <dt className="text-fg-muted">Originalanteckning</dt>
                    <dd className="break-words">{r.original_notes || r.original_title || <span className="text-fg-subtle">Ingen</span>}</dd>
                  </div>
                  {r.import_origin ? (
                    <div className="col-span-2 sm:col-span-4">
                      <dt className="text-fg-muted">Originalfil</dt>
                      <dd className="font-mono break-all">
                        {r.import_origin.file ?? "okänd fil"}, rad {r.import_origin.row_number}
                      </dd>
                    </div>
                  ) : null}
                </dl>
              </div>
              {merged && i < d.raw_trips.length - 1 ? (
                <div className="flex justify-center py-1.5">
                  <Button
                    size="xs"
                    variant="ghost"
                    loading={split.isPending}
                    onClick={() =>
                      split.mutate(
                        { id: d.trip.id, afterIndex: i },
                        {
                          onSuccess: (res) => {
                            toast.success("Resan är delad i två.", {
                              action: {
                                label: "Ångra",
                                onClick: () =>
                                  undoSplit.mutate(
                                    res.map((x) => x.id),
                                    {
                                      onSuccess: (back) => {
                                        toast.success("Uppdelningen är ångrad.");
                                        if (back[0]) router.replace(`/journal/${back[0].id}`);
                                      },
                                      onError: (e) => toast.error(errorMessage(e)),
                                    },
                                  ),
                              },
                            });
                            if (res[0]) router.replace(`/journal/${res[0].id}`);
                          },
                          onError: (e) => toast.error(errorMessage(e)),
                        },
                      )
                    }
                  >
                    <Scissors aria-hidden /> Dela resan här
                  </Button>
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
    </CollapsibleSection>
  );
}

function TripListMini({ trips }: { trips: TripDetail["similar_trips"] }) {
  return (
    <ul className="divide-y divide-border">
      {trips.map((s) => (
        <li key={s.id}>
          <Link href={`/journal/${s.id}`} className="flex items-center gap-3 py-2 text-sm hover:bg-surface-2/50 focus-visible:outline-2 focus-visible:outline-ring">
            <span className="w-24 shrink-0 num text-xs text-fg-muted">{formatDate(s.started_at ?? s.sort_time)}</span>
            <span className="min-w-0 flex-1 truncate">{s.purpose ?? `${s.start_address ?? "–"} → ${s.end_address ?? "–"}`}</span>
            <CategoryPill category={s.category} size="sm" />
            <span className="w-16 shrink-0 text-right num text-xs">{formatKm(s.distance_km)}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function HistorySection({ d }: { d: TripDetail }) {
  const undo = useUndoFeedback();
  const latest = d.feedback.find((f) => !f.undone_at);
  return (
    <CollapsibleSection title="Ändringshistorik" icon={<History />} meta={plural(d.history.length, "händelse", "händelser")}>
      {latest ? (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-md bg-surface-2 p-3 text-sm">
          <span>
            Senaste ändring: {latest.original_category && latest.new_category && latest.original_category !== latest.new_category
              ? `${CATEGORY_LABELS[latest.original_category]} → ${CATEGORY_LABELS[latest.new_category]}`
              : latest.new_purpose
                ? `syfte "${latest.new_purpose}"`
                : "bekräftelse"}{" "}
            <span className="text-fg-muted">({formatDateTime(latest.created_at)})</span>
          </span>
          <Button
            size="sm"
            variant="secondary"
            loading={undo.isPending}
            onClick={() =>
              undo.mutate(latest.id, {
                onSuccess: () => toast.success("Ändringen är ångrad."),
                onError: (e) => toast.error(e instanceof ApiError && e.code === "later_changes" ? e.detail : errorMessage(e)),
              })
            }
          >
            <Undo2 aria-hidden /> Ångra ändring
          </Button>
        </div>
      ) : null}
      {d.history.length ? (
        <ol className="space-y-2 text-sm">
          {d.history.map((h) => (
            <li key={h.id} className="flex gap-3">
              <span className="w-32 shrink-0 num text-xs text-fg-muted">{formatDateTime(h.at)}</span>
              <span className="flex-1">
                {AUDIT_ACTION_LABELS[h.action] ?? h.action}
                <span className="ml-1.5 text-xs text-fg-muted">
                  {h.actor_type === "user" ? "av dig" : h.actor_type === "system" ? "av systemet" : h.actor_type === "ha" ? "via Home Assistant" : ""}
                </span>
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-sm text-fg-muted">Inga ändringar ännu.</p>
      )}
    </CollapsibleSection>
  );
}

function MergeSection({ d }: { d: TripDetail }) {
  const unmerge = useUnmerge();
  const undoOp = useUndoMergeOperation();
  const router = useRouter();
  const t = d.trip;
  if (t.segment_count <= 1 && !d.merged_from.length && !d.merge_operations.length) return null;
  return (
    <CollapsibleSection title="Sammanslagning" icon={<Layers />} meta={label(MERGE_KIND_LABELS, t.merge_kind)}>
      <div className="space-y-3 text-sm">
        {t.segment_count > 1 ? (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-fg-muted">Resan består av {t.segment_count} delresor som slagits samman.</p>
            <Button
              size="sm"
              variant="secondary"
              loading={unmerge.isPending}
              onClick={() =>
                unmerge.mutate(t.id, {
                  onSuccess: (res) => {
                    toast.success("Sammanslagningen är ångrad.");
                    if (res[0]) router.replace(`/journal/${res[0].id}`);
                  },
                  onError: (e) => toast.error(errorMessage(e)),
                })
              }
            >
              <Undo2 aria-hidden /> Ångra sammanslagning
            </Button>
          </div>
        ) : null}
        {d.merged_from.length ? <TripListMini trips={d.merged_from} /> : null}
        {d.merge_operations.length ? (
          <ul className="space-y-1 text-xs text-fg-muted">
            {d.merge_operations.map((op) => (
              <li key={op.id} className="flex min-h-7 flex-wrap items-center gap-x-1">
                <span className="num">{formatDateTime(op.created_at)}</span> · {label(MERGE_OPERATION_LABELS, op.operation)}
                {op.undone_at ? " (ångrad)" : (
                  <Button
                    size="xs"
                    variant="ghost"
                    className="ml-1 text-fg-muted"
                    loading={undoOp.isPending && undoOp.variables === op.id}
                    disabled={undoOp.isPending}
                    aria-label={`Ångra ${label(MERGE_OPERATION_LABELS, op.operation).toLowerCase()} ${formatDateTime(op.created_at)}`}
                    onClick={() =>
                      undoOp.mutate(op.id, {
                        onSuccess: (res) => {
                          toast.success("Åtgärden är ångrad.");
                          if (res[0] && !res.some((x) => x.id === t.id)) router.replace(`/journal/${res[0].id}`);
                        },
                        onError: (e) => toast.error(errorMessage(e)),
                      })
                    }
                  >
                    <Undo2 aria-hidden /> Ångra
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </CollapsibleSection>
  );
}

function JourneySection({ d }: { d: TripDetail }) {
  const update = useUpdateJourney();
  const dissolve = useDeleteJourney();
  const [renaming, setRenaming] = React.useState(false);
  const [name, setName] = React.useState("");
  const j = d.journey;
  if (!j) return null;
  const saveName = (e: React.FormEvent) => {
    e.preventDefault();
    const next = name.trim();
    if (!next || next === j.name) return setRenaming(false);
    update.mutate(
      { id: j.id, name: next },
      { onSuccess: () => { toast.success("Namnet är ändrat."); setRenaming(false); }, onError: (err) => toast.error(errorMessage(err)) },
    );
  };
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2">
          <Route className="size-4 text-fg-subtle" aria-hidden /> Tjänsteresa
        </CardTitle>
        <Badge tone={j.status === "confirmed" ? "ok" : "accent"}>{j.status === "confirmed" ? "Bekräftad" : "Förslag"}</Badge>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {renaming ? (
          <form onSubmit={saveName} className="flex flex-wrap items-center gap-2">
            <Input
              aria-label="Tjänsteresans namn"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
              autoFocus
              className="h-8 min-w-0 flex-1 basis-48"
              onKeyDown={(e) => e.key === "Escape" && setRenaming(false)}
            />
            <Button type="submit" size="sm" variant="primary" loading={update.isPending} disabled={!name.trim()}>
              Spara
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setRenaming(false)}>
              Avbryt
            </Button>
          </form>
        ) : (
          <p className="flex items-center gap-1 font-medium">
            <span className="min-w-0 break-words">{j.name}</span>
            <Button size="icon-xs" variant="ghost" className="text-fg-subtle" aria-label="Byt namn på tjänsteresan" title="Byt namn" onClick={() => { setName(j.name); setRenaming(true); }}>
              <PenLine aria-hidden />
            </Button>
          </p>
        )}
        {j.purpose ? <p className="text-fg-muted">{j.purpose}</p> : null}
        <p className="num text-xs text-fg-muted">
          {formatDate(j.started_at)} – {formatDate(j.ended_at)}
        </p>
        <div className="flex flex-wrap gap-2 pt-1">
          <Button size="sm" variant="ghost" asChild>
            <Link href={`/journal?journey=${j.id}`}>Visa alla resor</Link>
          </Button>
          {j.status !== "confirmed" ? (
            <Button size="sm" variant="secondary" loading={update.isPending && !renaming} onClick={() => update.mutate({ id: j.id, confirm: true }, { onSuccess: () => toast.success("Tjänsteresan är bekräftad."), onError: (e) => toast.error(errorMessage(e)) })}>
              Bekräfta
            </Button>
          ) : null}
        </div>
        <ConfirmInline
          label="Inte en tjänsteresa"
          confirmLabel="Lös upp"
          size="xs"
          triggerClassName="-ml-2 text-fg-muted"
          description="Grupperingen tas bort. Körningarna och deras klassning påverkas inte."
          loading={dissolve.isPending}
          onConfirm={() => dissolve.mutate(j.id, { onSuccess: () => toast.success("Tjänsteresan är upplöst."), onError: (e) => toast.error(errorMessage(e)) })}
        />
      </CardContent>
    </Card>
  );
}

export function TripDetailView({ id }: { id: string }) {
  const q = useTripDetail(id);
  const [allocRaw, setAllocRaw] = React.useState<RawTrip | null>(null);

  if (q.isError) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    return (
      <>
        <BackLink />
        <Card>
          <ErrorState error={q.error} title={notFound ? "Resan finns inte" : undefined} onRetry={notFound ? undefined : () => q.refetch()} />
        </Card>
      </>
    );
  }
  if (!q.data) {
    return (
      <div role="status" aria-label="Laddar resan" className="space-y-4">
        <Skeleton className="h-8 w-72" />
        <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
          <Skeleton className="h-80" />
          <Skeleton className="h-80" />
        </div>
      </div>
    );
  }
  const d = q.data;
  const t = d.trip;
  const when = t.started_at ?? t.sort_time;
  return (
    <>
      <BackLink />
      <header className="mb-5">
        <p className="text-sm text-fg-muted">
          <span className="capitalize">{formatWeekday(when)}</span> <span className="num">{formatDate(when)}</span>
        </p>
        <h1 className="mt-1 flex flex-wrap items-center gap-x-2 text-xl leading-snug font-semibold tracking-tight sm:text-2xl">
          <span>{t.start_location?.confirmed ? t.start_location.name : (t.start_address ?? "Okänd start")}</span>
          <ArrowRight className="size-5 text-fg-subtle" aria-label="till" />
          <span>{t.end_location?.confirmed ? t.end_location.name : (t.end_address ?? "Okänt mål")}</span>
        </h1>
      </header>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-5">
          <Card className="p-3">
            <TripMap map={d.map} />
          </Card>
          {/* Mobil: klassning direkt under kartan (överst: karta, sträcka, kategori, syfte). */}
          <div className="lg:hidden">
            <ClassificationCard detail={d} />
          </div>
          <Facts d={d} />
        </div>
        <div className="space-y-5">
          <div className="hidden lg:block">
            <ClassificationCard detail={d} />
          </div>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle>Färdväg</CardTitle>
            </CardHeader>
            <CardContent>
              <Stops d={d} />
            </CardContent>
          </Card>
          <CalendarMatches d={d} />
          <JourneySection d={d} />
        </div>
      </div>

      <div className="mt-6 space-y-3">
        <h2 className="text-sm font-semibold text-fg-muted">Underlag och detaljer</h2>
        <Reasoning d={d} />
        <RawTrips d={d} onAllocate={setAllocRaw} />
        <MergeSection d={d} />
        {d.similar_trips.length ? (
          <CollapsibleSection title="Tidigare liknande resor" icon={<Route />} meta={`${d.similar_trips.length} st`}>
            <TripListMini trips={d.similar_trips} />
          </CollapsibleSection>
        ) : null}
        <HistorySection d={d} />
      </div>
      {allocRaw ? (
        <AllocationDialog
          key={allocRaw.id}
          tripId={t.id}
          raw={allocRaw}
          segments={d.segments.filter((s) => s.raw_trip_id === allocRaw.id)}
          open
          onOpenChange={(o) => !o && setAllocRaw(null)}
        />
      ) : null}
    </>
  );
}

function BackLink() {
  return (
    <Link href="/journal" className="mb-4 inline-flex items-center gap-1.5 rounded-sm text-sm text-fg-muted hover:text-fg focus-visible:outline-2 focus-visible:outline-ring">
      <ArrowLeft className="size-4" aria-hidden /> Körjournal
    </Link>
  );
}
