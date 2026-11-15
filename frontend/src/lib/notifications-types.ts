/**
 * Typer för notisfrågor (mobil snabbvy /m/q/[id]).
 *  - GET /notifications/{public_id} → NotificationDetail (backend/app/api/routes/notifications.py).
 *    `answer` och `summary` är dict i OpenAPI; handskrivna här mot get_notification().
 *  - POST /notifications/{public_id}/respond svarar med dict[str, Any]; typen följer
 *    backend/app/services/ha/responses.py → _result() och handle().
 */

import type { components } from "./api-types";

type S = components["schemas"];

export type QuestionAction = S["ActionOut"];
/**
 * Resans sammanfattning i frågan. Platsetiketter (aldrig gatuadresser) och tidernas kvalitet
 * finns i nyare backend (TripSummary i notifications.py) – valfria så att äldre instanser fungerar.
 */
export type QuestionTrip = S["TripSummary"] & {
  start_label?: string | null;
  end_label?: string | null;
  start_time_quality?: string | null;
  end_time_quality?: string | null;
};

/** classify | purpose | merge | digest | weekly | daily | test */
export type QuestionKind = "classify" | "purpose" | "merge" | "digest" | "weekly" | "daily" | "test";

export interface QuestionAnswer {
  action: string;
  /** web | ha | demo */
  source: string;
  received_at: string;
  message: string | null;
}

export interface Question extends Omit<S["NotificationDetail"], "answer" | "summary" | "trip" | "merge_trips"> {
  trip: QuestionTrip | null;
  merge_trips: QuestionTrip[];
  summary: Record<string, unknown> | null;
  answer: QuestionAnswer | null;
}

/** applied | duplicate | expired | invalid | opened | snoozed */
export type RespondOutcome = "applied" | "duplicate" | "expired" | "invalid" | "opened" | "snoozed";

export interface RespondResult {
  outcome: RespondOutcome;
  /** Svensk text att visa användaren. */
  message: string;
  public_id: string | null;
  kind: string | null;
  status: string | null;
  trip_id: string | null;
  follow_up: string | null;
  notify_user: boolean;
  /** Sätts när samma svar (idempotensnyckel) redan tagits emot: utfallet första gången. */
  first_outcome?: RespondOutcome;
  category?: "work" | "private";
  purpose?: string;
  merged_trip_id?: string;
  snoozed_until?: string;
  /** Felkod vid outcome = invalid, t.ex. not_active, empty_purpose, purpose_too_long. */
  code?: string;
}

export interface RespondInput {
  action: string;
  reply_text?: string | null;
  idempotency_key: string;
}
