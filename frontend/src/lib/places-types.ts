/**
 * Typer för vyn Platser (platser, förslag, inlärda mönster och regler).
 *
 * Fält som backend beskriver som `dict[str, Any]` är handskrivna här mot:
 *  - backend/app/services/places/suggest.py → location_stats()   (LocationOut.stats)
 *  - backend/app/services/places/registry.py → merge_suggestions()
 *  - backend/app/services/learning/patterns.py                    (PatternOut.purposes)
 */

import type { components } from "./api-types";
import type { PositionLookup } from "./types";

type S = components["schemas"];

/** backend/app/models/enums.py → LocationType */
export type LocationTypeCode =
  | "home"
  | "regular_workplace"
  | "office"
  | "customer"
  | "hotel"
  | "private"
  | "fuel_station"
  | "charging_station"
  | "other"
  | "unknown";

export interface PlaceStats {
  /** Antal aktiva resor som slutar på platsen. */
  arrivals?: number;
  work?: number;
  private?: number;
  undecided?: number;
  km?: number;
  most_common_category?: "work" | "private" | null;
  /** Vanligaste BEKRÄFTADE syftena för resor hit (högst fem). */
  top_purposes?: { text: string; count: number }[];
}

export interface Place extends Omit<S["LocationOut"], "stats"> {
  stats: PlaceStats;
  /**
   * Adressuppslaget när platsen har adress men saknar position (1.3.0). Valfritt så att typen
   * fungerar före och efter att api-types.ts genererats om. `coordinate_source` kan då vara "geocoded".
   */
  position_lookup?: PositionLookup | null;
}

/** Body för POST /locations och PATCH /locations/{id} (LocationIn). */
export interface PlaceInput {
  name?: string;
  type?: string;
  address?: string | null;
  company?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  radius_m?: number;
  confirmed?: boolean;
  notes?: string | null;
  clear_coordinates?: boolean;
}

export interface MergeSuggestion {
  street_key: string;
  locations: { id: string; name: string; address: string | null; visit_count: number }[];
  reason: string;
}

/** GET /locations/suggestions */
export interface PlaceSuggestions {
  types: Place[];
  merges: MergeSuggestion[];
}

export interface PatternPurpose {
  key: string;
  text: string;
  type: string | null;
  count: number;
  user_count: number;
  category: string | null;
  last_used: string | null;
}

export interface Pattern extends Omit<S["PatternOut"], "purposes" | "pattern_type"> {
  pattern_type: "destination" | "route";
  purposes: PatternPurpose[];
}

/** Body för POST /learning/rules (RuleIn). */
export interface RuleInput {
  origin_location_id?: string | null;
  destination_location_id: string;
  /** "undecided" = "Fråga mig alltid": resorna föreslås aldrig automatiskt. */
  category?: "work" | "private" | "undecided" | null;
  purpose?: string | null;
  /**
   * När den fasta texten gäller (1.3.0, location_patterns.manual_purpose_mode):
   * "unless_calendar" = när kalendern inte säger något annat (standard), "always" = alltid.
   */
  purpose_mode?: PurposeMode | null;
}

export type PurposeMode = "unless_calendar" | "always";

/** Status på ett inlärt mönster (PatternUpdate i backend/app/api/routes/learning.py). */
export type PatternStatus = "suggested" | "approved" | "rejected";
