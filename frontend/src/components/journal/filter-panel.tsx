"use client";

import * as React from "react";
import { SlidersHorizontal, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { formatDate } from "@/lib/format";
import { CATEGORY_LABELS, REVIEW_STATUS_LABELS, TRIP_SOURCE_LABELS } from "@/lib/labels";
import { periodLabel } from "@/lib/period";
import { activeFilterCount, EMPTY_FILTERS, type JournalFilters } from "./filters";

function CheckGroup({
  legend,
  options,
  value,
  onChange,
}: {
  legend: string;
  options: Record<string, string>;
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <fieldset className="space-y-2">
      <legend className="mb-2 text-xs font-semibold tracking-wide text-fg-muted uppercase">{legend}</legend>
      {Object.entries(options).map(([code, text]) => {
        const id = `f-${legend}-${code}`;
        return (
          <div key={code} className="flex items-center gap-2.5">
            <Checkbox
              id={id}
              checked={value.includes(code)}
              onCheckedChange={(on) => onChange(on === true ? [...value, code] : value.filter((v) => v !== code))}
            />
            <label htmlFor={id} className="text-sm">
              {text}
            </label>
          </div>
        );
      })}
    </fieldset>
  );
}

const CATS = { work: CATEGORY_LABELS.work, private: CATEGORY_LABELS.private, undecided: CATEGORY_LABELS.undecided };

export function FilterButton({ filters, onChange }: { filters: JournalFilters; onChange: (f: JournalFilters) => void }) {
  const [open, setOpen] = React.useState(false);
  const count = activeFilterCount(filters) - (filters.period.key !== "all" ? 1 : 0);
  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)} aria-haspopup="dialog">
        <SlidersHorizontal aria-hidden />
        Filter
        {count ? (
          <Badge tone="accent" size="sm" className="num">
            {count}
          </Badge>
        ) : null}
      </Button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="gap-0 overflow-hidden p-0">
          <SheetHeader className="shrink-0 border-b border-border px-5 py-4 pr-12">
            <SheetTitle>Filtrera resor</SheetTitle>
            <SheetDescription>Urvalet påverkar tabellen, summeringen och massåtgärderna.</SheetDescription>
          </SheetHeader>
          <div className="scrollbar-thin flex-1 space-y-6 overflow-y-auto px-5 py-5">
            <div className="space-y-3 rounded-lg border border-border p-3">
              <label className="flex items-center justify-between gap-3 text-sm">
                <span>
                  <span className="font-medium">Behöver granskas</span>
                  <span className="block text-xs text-fg-muted">Ej godkända förslag och preliminära klassningar</span>
                </span>
                <Switch checked={filters.needs_review} onCheckedChange={(v) => onChange({ ...filters, needs_review: v })} />
              </label>
              <label className="flex items-center justify-between gap-3 text-sm">
                <span className="font-medium">Endast favoriter</span>
                <Switch checked={filters.favorite} onCheckedChange={(v) => onChange({ ...filters, favorite: v })} />
              </label>
            </div>
            <CheckGroup legend="Kategori" options={CATS} value={filters.category} onChange={(category) => onChange({ ...filters, category })} />
            <CheckGroup legend="Granskningsstatus" options={REVIEW_STATUS_LABELS} value={filters.review_status} onChange={(review_status) => onChange({ ...filters, review_status })} />
            <CheckGroup legend="Datakälla" options={TRIP_SOURCE_LABELS} value={filters.source} onChange={(source) => onChange({ ...filters, source })} />
          </div>
          <div className="flex shrink-0 gap-2 border-t border-border px-5 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
            <Button variant="ghost" onClick={() => onChange({ ...EMPTY_FILTERS, q: filters.q, period: filters.period })}>
              Rensa filter
            </Button>
            <Button variant="primary" className="ml-auto" onClick={() => setOpen(false)}>
              Visa resor
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}

/** Aktiva filter som borttagbara chips. */
export function ActiveFilterChips({ filters, onChange }: { filters: JournalFilters; onChange: (f: JournalFilters) => void }) {
  const chips: { key: string; label: string; remove: () => void }[] = [];
  if (filters.period.key !== "all") {
    const text = filters.period.key === "custom" ? `${formatDate(filters.period.from)} – ${formatDate(filters.period.to)}` : periodLabel(filters.period.key);
    chips.push({ key: "period", label: text, remove: () => onChange({ ...filters, period: { key: "all" } }) });
  }
  if (filters.q) chips.push({ key: "q", label: `Sök: "${filters.q}"`, remove: () => onChange({ ...filters, q: "" }) });
  if (filters.needs_review) chips.push({ key: "nr", label: "Behöver granskas", remove: () => onChange({ ...filters, needs_review: false }) });
  if (filters.favorite) chips.push({ key: "fav", label: "Favoriter", remove: () => onChange({ ...filters, favorite: false }) });
  for (const c of filters.category) chips.push({ key: `c${c}`, label: CATEGORY_LABELS[c] ?? c, remove: () => onChange({ ...filters, category: filters.category.filter((x) => x !== c) }) });
  for (const c of filters.review_status) chips.push({ key: `s${c}`, label: REVIEW_STATUS_LABELS[c] ?? c, remove: () => onChange({ ...filters, review_status: filters.review_status.filter((x) => x !== c) }) });
  for (const c of filters.source) chips.push({ key: `k${c}`, label: TRIP_SOURCE_LABELS[c] ?? c, remove: () => onChange({ ...filters, source: filters.source.filter((x) => x !== c) }) });
  if (!chips.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5" aria-label="Aktiva filter">
      {chips.map((c) => (
        <span key={c.key} className="inline-flex items-center gap-1 rounded-full border border-border bg-surface py-0.5 pr-1 pl-2.5 text-xs text-fg">
          {c.label}
          <button type="button" onClick={c.remove} className="rounded-full p-0.5 text-fg-subtle hover:bg-surface-2 hover:text-fg" aria-label={`Ta bort filter ${c.label}`}>
            <X className="size-3" />
          </button>
        </span>
      ))}
      {chips.length > 1 ? (
        <button type="button" onClick={() => onChange(EMPTY_FILTERS)} className="ml-1 text-xs font-medium text-accent-ink hover:underline">
          Rensa alla
        </button>
      ) : null}
    </div>
  );
}
