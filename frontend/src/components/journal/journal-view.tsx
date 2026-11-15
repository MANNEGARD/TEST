"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { NotebookTabs, Plus, Search, Upload, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DateRangePicker } from "@/components/ui/date-range-picker";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/page-header";
import { Pagination } from "@/components/ui/pagination";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Skeleton } from "@/components/ui/skeleton";
import { TripCard } from "@/components/trips/trip-card";
import { DEFAULT_VISIBLE } from "@/components/trips/trip-columns";
import { TotalsLine, TripTable, type TripRowHandlers } from "@/components/trips/trip-table";
import { useTripActions } from "@/components/trips/use-trip-actions";
import { periodToRange } from "@/lib/period";
import { EXPORT_SELECTION_KEY } from "@/components/export/export-period";
import { useJourneys, useTrips, type TripListParams } from "@/lib/queries/trips";
import { readStorage, writeStorage } from "@/lib/utils";
import { BulkBar } from "./bulk-bar";
import { ColumnPicker } from "./column-picker";
import { ActiveFilterChips, FilterButton } from "./filter-panel";
import { activeFilterCount, EMPTY_FILTERS, type JournalFilters } from "./filters";
import { ManualTripDialog } from "./manual-trip-dialog";
import { useMergeFlow } from "./merge-flow";

const COLS_KEY = "drivelog.journal.columns";
const PAGE_SIZE = 50;

type Quick = "all" | "work" | "private" | "review";

function quickOf(f: JournalFilters): Quick | undefined {
  const plain = !f.favorite && !f.review_status.length && !f.source.length;
  if (!plain) return undefined;
  if (f.needs_review && !f.category.length) return "review";
  if (f.needs_review) return undefined;
  if (!f.category.length) return "all";
  if (f.category.length === 1 && (f.category[0] === "work" || f.category[0] === "private")) return f.category[0] as Quick;
  return undefined;
}

function filtersFromUrl(params: URLSearchParams): JournalFilters {
  return {
    ...EMPTY_FILTERS,
    q: params.get("q") ?? "",
    category: params.getAll("category"),
    needs_review: params.get("needs_review") === "1",
    favorite: params.get("favorite") === "1",
    journey: params.get("journey") ?? "",
  };
}

export function JournalView() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [filters, setFiltersState] = React.useState<JournalFilters>(() => filtersFromUrl(params));
  const [search, setSearch] = React.useState(filters.q);
  const [sort, setSort] = React.useState<NonNullable<TripListParams["sort"]>>("date");
  const [order, setOrder] = React.useState<"asc" | "desc">("desc");
  const [page, setPage] = React.useState(1);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [visible, setVisible] = React.useState<string[]>(() => readStorage("local", COLS_KEY, DEFAULT_VISIBLE));

  // ⌘K-sökningen navigerar hit med ?q= – följ URL:en när den ändras utifrån.
  const urlQ = params.get("q") ?? "";
  const [lastUrlQ, setLastUrlQ] = React.useState(urlQ);
  if (urlQ !== lastUrlQ) {
    setLastUrlQ(urlQ);
    setSearch(urlQ);
    setFiltersState((f) => ({ ...f, q: urlQ }));
    setPage(1);
  }

  const setFilters = React.useCallback(
    (f: JournalFilters) => {
      setFiltersState(f);
      setPage(1);
      setSelected(new Set());
      const sp = new URLSearchParams();
      if (f.q) sp.set("q", f.q);
      for (const c of f.category) sp.append("category", c);
      if (f.needs_review) sp.set("needs_review", "1");
      if (f.favorite) sp.set("favorite", "1");
      if (f.journey) sp.set("journey", f.journey);
      const qs = sp.toString();
      setLastUrlQ(f.q);
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname],
  );

  // Fördröjd sökning (300 ms) medan användaren skriver.
  React.useEffect(() => {
    if (search === filters.q) return;
    const id = window.setTimeout(() => setFilters({ ...filters, q: search.trim() }), 300);
    return () => window.clearTimeout(id);
  }, [search, filters, setFilters]);

  const range = periodToRange(filters.period);
  const query: TripListParams = {
    from: range.from,
    to: range.to,
    category: filters.category.length ? filters.category : undefined,
    review_status: filters.review_status.length ? filters.review_status : undefined,
    source: filters.source.length ? filters.source : undefined,
    q: filters.q || undefined,
    favorite: filters.favorite || undefined,
    needs_review: filters.needs_review || undefined,
    journey_id: filters.journey || undefined,
    sort,
    order,
    page,
    page_size: PAGE_SIZE,
  };
  const trips = useTrips(query);
  const journeys = useJourneys(!!filters.journey);
  const journeyName = journeys.data?.find((j) => j.id === filters.journey)?.name;
  const actions = useTripActions();
  const mergeFlow = useMergeFlow(() => setSelected(new Set()));

  const handlers: TripRowHandlers = {
    onCategory: (t, c) => void actions.setCategory(t.id, c).catch(() => {}),
    onPurpose: (t, text) => void actions.setPurpose(t.id, text).catch(() => {}),
    onFavorite: (t) => void actions.toggleFavorite(t.id, !t.is_favorite),
  };

  const onSort = (field: string) => {
    if (field === sort) setOrder(order === "asc" ? "desc" : "asc");
    else {
      setSort(field as NonNullable<TripListParams["sort"]>);
      setOrder(field === "date" || field === "distance" || field === "odometer" ? "desc" : "asc");
    }
    setPage(1);
  };

  const onSelect = (ids: string[], on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });

  const ids = [...selected];
  const data = trips.data;
  const quick = quickOf(filters);
  const hasFilters = activeFilterCount(filters) > 0 || !!filters.q;

  return (
    <>
      <PageHeader
        title="Körjournal"
        description="Alla resor med datum, adresser, mätarställning och klassning. Byt kategori med ett klick."
        actions={
          <>
            <Button asChild variant="ghost" className="hidden sm:inline-flex">
              <Link href="/import">
                <Upload aria-hidden /> Importera
              </Link>
            </Button>
            <ManualTripDialog />
          </>
        }
      />

      <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-center">
        <SegmentedControl<Quick>
          ariaLabel="Snabbfilter"
          value={quick}
          size="md"
          className="self-start"
          onChange={(v) =>
            setFilters({
              ...filters,
              favorite: false,
              review_status: [],
              source: [],
              category: v === "work" || v === "private" ? [v] : [],
              needs_review: v === "review",
            })
          }
          options={[
            { value: "all", label: "Alla" },
            { value: "work", label: "Arbete" },
            { value: "private", label: "Privat" },
            { value: "review", label: "Att granska" },
          ]}
        />
        <div className="flex flex-1 flex-wrap items-center gap-2">
          <div className="relative min-w-0 flex-1 basis-56">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden />
            <Input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Sök adress, syfte, kontakt eller anteckning"
              aria-label="Sök i körjournalen"
              className="pr-8 pl-9"
            />
            {search ? (
              <button
                type="button"
                onClick={() => setSearch("")}
                className="absolute top-1/2 right-2 -translate-y-1/2 rounded-sm p-1 text-fg-subtle hover:text-fg"
                aria-label="Rensa sökning"
              >
                <X className="size-3.5" />
              </button>
            ) : null}
          </div>
          <DateRangePicker value={filters.period} onChange={(period) => setFilters({ ...filters, period })} />
          <FilterButton filters={filters} onChange={setFilters} />
          <ColumnPicker
            visible={visible}
            onChange={(v) => {
              setVisible(v);
              writeStorage("local", COLS_KEY, v);
            }}
          />
        </div>
      </div>
      <ActiveFilterChips filters={{ ...filters, category: quick ? [] : filters.category, needs_review: quick ? false : filters.needs_review }} onChange={setFilters} journeyName={journeyName} />

      <Card className="mt-3 overflow-clip">
        <BulkBar
          count={selected.size}
          busy={actions.isPending || mergeFlow.isPending}
          handlers={{
            onCategory: (c) => void actions.bulk(ids, { category: c }, `${ids.length} resor satta till ${c === "work" ? "Arbete" : "Privat"}.`).catch(() => {}),
            onPurpose: (text) => void actions.bulk(ids, { purpose: text }, "Syftet är sparat.").catch(() => {}),
            onApprove: () => void actions.bulk(ids, { approve: true }, `${ids.length} resor godkända.`).then(() => setSelected(new Set()), () => {}),
            onFavorite: () => void actions.bulk(ids, { is_favorite: true }, "Favoritmarkerade.").catch(() => {}),
            onMerge: () => mergeFlow.run(ids),
            onExport: () => {
              writeStorage("session", EXPORT_SELECTION_KEY, ids);
              router.push("/export?selected=1");
            },
            onClear: () => setSelected(new Set()),
          }}
        />
        {trips.isError ? (
          <ErrorState error={trips.error} onRetry={() => trips.refetch()} />
        ) : !data ? (
          <div className="space-y-2 p-4" role="status" aria-label="Laddar resor">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : data.items.length === 0 ? (
          hasFilters ? (
            <EmptyState
              icon={<Search />}
              title="Inga resor matchar urvalet"
              description="Prova en annan period eller ta bort några filter."
              action={<Button onClick={() => { setSearch(""); setFilters(EMPTY_FILTERS); }}>Rensa filter</Button>}
            />
          ) : (
            <EmptyState
              icon={<NotebookTabs />}
              title="Inga resor ännu – importera din körjournal"
              description="Ladda upp exporten från Volvo Cars-appen så fylls journalen på, eller lägg in en resa för hand."
              action={
                <>
                  <Button asChild variant="primary">
                    <Link href="/import">
                      <Upload aria-hidden /> Importera körjournal
                    </Link>
                  </Button>
                  <ManualTripDialog trigger={<Button><Plus aria-hidden /> Lägg till manuellt</Button>} />
                </>
              }
            />
          )
        ) : (
          <div className={trips.isFetching && trips.isPlaceholderData ? "opacity-60 transition-opacity" : undefined}>
            <div className="hidden md:block">
              <TripTable
                data={data}
                visible={visible}
                sort={sort}
                order={order}
                onSort={onSort}
                selected={selected}
                onSelect={onSelect}
                handlers={handlers}
              />
            </div>
            <div className="space-y-2.5 p-3 md:hidden">
              <TotalsLine total={data.total} totals={data.totals} className="px-1 pb-1 text-xs" />
              {data.items.map((t) => (
                <TripCard key={t.id} trip={t} selected={selected.has(t.id)} onSelect={(on) => onSelect([t.id], on)} handlers={handlers} />
              ))}
            </div>
            <div className="border-t border-border px-4 py-2.5">
              <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPage={(p) => { setPage(p); window.scrollTo({ top: 0 }); }} />
            </div>
          </div>
        )}
      </Card>
      {mergeFlow.dialog}
    </>
  );
}
