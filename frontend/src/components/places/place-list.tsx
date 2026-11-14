"use client";

import * as React from "react";
import Link from "next/link";
import { ChevronRight, CircleDashed, MapPin, Plus, Search, Upload, X } from "lucide-react";
import { CategoryPill } from "@/components/trips/category-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatNumber, plural } from "@/lib/format";
import type { Place } from "@/lib/places-types";
import { cn } from "@/lib/utils";
import { LOCATION_TYPES, PlaceAvatar, typeLabel, typeText } from "./location-type";

type Sort = "visits" | "name" | "recent";

const SORT_LABELS: Record<Sort, string> = {
  visits: "Flest besök",
  name: "Namn",
  recent: "Senast besökt",
};

/** Kolumnmall för rader och rubrikrad (md och uppåt). */
const GRID = "md:grid-cols-[2.25rem_minmax(0,1.5fr)_4.5rem_6.5rem_minmax(0,1.2fr)_1rem]";

function matches(p: Place, q: string): boolean {
  if (!q) return true;
  const hay = [p.name, p.address, p.city, p.company, p.notes, typeLabel(p.type), ...p.aliases].filter(Boolean).join("\n").toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .every((word) => hay.includes(word));
}

function time(value: string | null): number {
  return value ? new Date(value).getTime() : -Infinity;
}

function subtitle(p: Place): string {
  const where = p.address && p.address !== p.name ? p.address : p.city && !p.name.includes(p.city) ? p.city : null;
  return [typeText(p.type), p.company && p.company !== p.name ? p.company : null, where].filter(Boolean).join(" · ");
}

/** Platslistan med sök, typfilter, "obekräftade" och sortering (allt i klienten). */
export function PlaceList({ places, onOpen, onNew }: { places: Place[]; onOpen: (id: string) => void; onNew: () => void }) {
  const [q, setQ] = React.useState("");
  const [type, setType] = React.useState("all");
  const [onlyUnconfirmed, setOnlyUnconfirmed] = React.useState(false);
  const [sort, setSort] = React.useState<Sort>("visits");

  const byType = React.useMemo(() => {
    const counts: Record<string, number> = {};
    for (const p of places) counts[p.type] = (counts[p.type] ?? 0) + 1;
    return counts;
  }, [places]);
  const unconfirmed = React.useMemo(() => places.filter((p) => !p.confirmed).length, [places]);
  const activeType = type !== "all" && !byType[type] ? "all" : type;
  const activeUnconfirmed = onlyUnconfirmed && unconfirmed > 0;

  const rows = React.useMemo(() => {
    const term = q.trim();
    const out = places.filter((p) => (activeType === "all" || p.type === activeType) && (!activeUnconfirmed || !p.confirmed) && matches(p, term));
    out.sort((a, b) => {
      if (sort === "name") return a.name.localeCompare(b.name, "sv");
      if (sort === "recent") return time(b.last_visited_at) - time(a.last_visited_at) || b.visit_count - a.visit_count;
      return b.visit_count - a.visit_count || a.name.localeCompare(b.name, "sv");
    });
    return out;
  }, [places, q, activeType, activeUnconfirmed, sort]);

  const filtered = !!q.trim() || activeType !== "all" || activeUnconfirmed;
  const clear = () => {
    setQ("");
    setType("all");
    setOnlyUnconfirmed(false);
  };

  if (!places.length) {
    return (
      <Card>
        <EmptyState
          icon={<MapPin />}
          title="Inga platser ännu"
          description="Platser skapas automatiskt av adresserna i din körjournal. Importera resor så dyker de upp här – eller lägg till en plats för hand."
          action={
            <>
              <Button asChild variant="primary">
                <Link href="/import">
                  <Upload aria-hidden /> Importera körjournal
                </Link>
              </Button>
              <Button onClick={onNew}>
                <Plus aria-hidden /> Ny plats
              </Button>
            </>
          }
        />
      </Card>
    );
  }

  return (
    <>
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
        <div className="relative min-w-0 flex-1 sm:basis-64">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden />
          <Input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Sök namn, adress eller företag"
            aria-label="Sök bland platser"
            className="pr-8 pl-9"
          />
          {q ? (
            <button
              type="button"
              onClick={() => setQ("")}
              className="absolute top-1/2 right-2 -translate-y-1/2 rounded-sm p-1 text-fg-subtle hover:text-fg"
              aria-label="Rensa sökning"
            >
              <X className="size-3.5" />
            </button>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {unconfirmed > 0 ? (
            <button
              type="button"
              aria-pressed={activeUnconfirmed}
              onClick={() => setOnlyUnconfirmed(!activeUnconfirmed)}
              className={cn(
                "inline-flex h-9 shrink-0 items-center gap-1.5 rounded-md border px-3 text-sm font-medium shadow-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
                activeUnconfirmed
                  ? "border-undecided/60 bg-undecided-soft text-undecided-ink"
                  : "border-border bg-surface text-fg hover:border-border-strong hover:bg-surface-2",
              )}
            >
              <CircleDashed className="size-4" aria-hidden />
              Obekräftade
              <span className={cn("font-mono text-xs", activeUnconfirmed ? "opacity-80" : "text-fg-muted")}>{formatNumber(unconfirmed, 0)}</span>
            </button>
          ) : null}
          <Select value={activeType} onValueChange={setType}>
            <SelectTrigger aria-label="Filtrera på typ" className="w-auto min-w-36 flex-1 sm:flex-none">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Alla typer</SelectItem>
              {LOCATION_TYPES.filter((t) => byType[t]).map((t) => (
                <SelectItem key={t} value={t}>
                  {typeLabel(t)} ({formatNumber(byType[t], 0)})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={(v) => setSort(v as Sort)}>
            <SelectTrigger aria-label="Sortering" className="w-auto min-w-36 flex-1 sm:flex-none">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(SORT_LABELS) as Sort[]).map((s) => (
                <SelectItem key={s} value={s}>
                  {SORT_LABELS[s]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <Card className="overflow-hidden">
        {rows.length === 0 ? (
          <EmptyState icon={<Search />} title="Inga platser matchar" description="Prova ett annat sökord eller ta bort filtren." action={<Button onClick={clear}>Rensa filter</Button>} />
        ) : (
          <>
            <div aria-hidden className={cn("hidden items-center gap-x-3 border-b border-border px-5 py-2 text-xs font-medium text-fg-muted md:grid", GRID)}>
              <span />
              <span>Plats</span>
              <span className="text-right">Besök</span>
              <span>Oftast</span>
              <span>Vanliga syften</span>
              <span />
            </div>
            <ul className="divide-y divide-border" aria-label="Platser" data-testid="place-list">
              {rows.map((p) => (
                <PlaceRow key={p.id} place={p} onOpen={onOpen} />
              ))}
            </ul>
            <p className="border-t border-border px-4 py-2.5 text-xs text-fg-muted num sm:px-5" aria-live="polite">
              {filtered ? `Visar ${formatNumber(rows.length, 0)} av ${plural(places.length, "plats", "platser")}` : plural(places.length, "plats", "platser")}
            </p>
          </>
        )}
      </Card>
    </>
  );
}

function PlaceRow({ place: p, onOpen }: { place: Place; onOpen: (id: string) => void }) {
  const purposes = p.stats.top_purposes ?? [];
  const category = p.stats.most_common_category;
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(p.id)}
        data-testid="place-row"
        className={cn(
          "grid w-full grid-cols-[2.25rem_minmax(0,1fr)_1rem] items-center gap-x-3 gap-y-1.5 px-4 py-3 text-left transition-colors hover:bg-surface-2/60 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:px-5",
          GRID,
        )}
      >
        <PlaceAvatar type={p.type} confirmed={p.confirmed} />
        <span className="min-w-0">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate text-sm font-medium">{p.name}</span>
            {!p.confirmed ? (
              <Badge tone="undecided" size="sm" className="shrink-0">
                <CircleDashed aria-hidden /> Obekräftad
              </Badge>
            ) : null}
            {p.is_demo ? (
              <Badge tone="warn" size="sm" className="shrink-0">
                Demo
              </Badge>
            ) : null}
          </span>
          <span className="mt-0.5 block truncate text-xs text-fg-muted">{subtitle(p)}</span>
        </span>

        {/* md och uppåt: egna kolumner */}
        <span className="hidden text-right font-mono text-sm md:block">
          {formatNumber(p.visit_count, 0)}
          <span className="sr-only"> besök</span>
        </span>
        <span className="hidden md:block">
          {category ? <CategoryPill category={category} size="sm" /> : <span className="text-fg-subtle">–</span>}
        </span>
        <span className="hidden min-w-0 truncate text-[13px] text-fg-muted md:block">
          {purposes.length ? (
            <>
              {purposes[0].text}
              {purposes.length > 1 ? <span className="text-fg-subtle"> +{purposes.length - 1}</span> : null}
            </>
          ) : (
            <span className="text-fg-subtle">–</span>
          )}
        </span>
        <ChevronRight className="size-4 text-fg-subtle" aria-hidden />

        {/* mobil: sammanfattning på en rad under namnet */}
        <span className="col-span-2 col-start-2 flex min-w-0 items-center gap-2 text-xs text-fg-muted md:hidden">
          <span className="shrink-0 num">{plural(p.visit_count, "besök", "besök")}</span>
          {category ? <CategoryPill category={category} size="sm" /> : null}
          {purposes.length ? <span className="min-w-0 truncate">{purposes[0].text}</span> : null}
        </span>
      </button>
    </li>
  );
}
