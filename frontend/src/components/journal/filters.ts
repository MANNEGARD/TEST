import type { PeriodValue } from "@/lib/period";

/** Körjournalens filterläge. Synkas delvis med URL:en (q, needs_review, category, favorite). */
export interface JournalFilters {
  period: PeriodValue;
  q: string;
  category: string[];
  review_status: string[];
  source: string[];
  favorite: boolean;
  needs_review: boolean;
}

export const EMPTY_FILTERS: JournalFilters = {
  period: { key: "all" },
  q: "",
  category: [],
  review_status: [],
  source: [],
  favorite: false,
  needs_review: false,
};

export function activeFilterCount(f: JournalFilters): number {
  return (
    (f.period.key !== "all" ? 1 : 0) +
    f.category.length +
    f.review_status.length +
    f.source.length +
    (f.favorite ? 1 : 0) +
    (f.needs_review ? 1 : 0)
  );
}
