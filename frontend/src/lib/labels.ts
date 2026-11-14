/**
 * Svenska etiketter för alla enum-koder från backend (backend/app/models/enums.py).
 * Använd `label(MAP, code)` som faller tillbaka på koden om etikett saknas.
 * Lägg till nya koder här – skriv aldrig svenska etiketter direkt i komponenter.
 */

export function label(map: Record<string, string>, code: string | null | undefined, fallback = "–"): string {
  if (code == null || code === "") return fallback;
  return map[code] ?? code;
}

export const CATEGORY_LABELS: Record<string, string> = {
  work: "Arbete",
  private: "Privat",
  undecided: "Ej avgjord",
  mixed: "Blandad",
};

export const SUBCATEGORY_LABELS: Record<string, string> = {
  customer_visit: "Kundbesök",
  office_work: "Kontorsarbete",
  meeting: "Möte",
  training: "Utbildning",
  personnel: "Personalärende",
  operations: "Operativ insats",
  return_home: "Hemresa från tjänsteuppdrag",
  hotel_transfer: "Resa mellan kontor och hotell",
  transit: "Förflyttning i tjänsten",
  commute: "Resa mellan bostad och arbetsplats",
  private_errand: "Privat ärende",
  other: "Övrigt",
};

export const TRIP_SOURCE_LABELS: Record<string, string> = {
  volvo_import: "Importerad Volvo-körjournal",
  volvo_api: "Rekonstruerad från fordonsrapporter",
  manual: "Manuellt inmatad",
  gap_fill: "Sammanfattad lucka",
  demo: "Demo",
};

/** Korta varianter för täta tabeller. */
export const TRIP_SOURCE_SHORT: Record<string, string> = {
  volvo_import: "Importerad",
  volvo_api: "Rekonstruerad",
  manual: "Manuell",
  gap_fill: "Lucka",
  demo: "Demo",
};

export const TIME_QUALITY_LABELS: Record<string, string> = {
  exact: "Faktisk tidpunkt",
  reported: "Fordonets tidsstämpel",
  estimated: "Uppskattad tid",
  bounded: "Endast gränser kända",
  unknown: "Okänd tid",
};

export const DISTANCE_QUALITY_LABELS: Record<string, string> = {
  measured: "Uppmätt av bilen",
  odometer: "Härledd ur mätarställningar",
  estimated: "Uppskattad",
  user: "Angiven av användaren",
};

export const VALUE_QUALITY_LABELS: Record<string, string> = {
  verified: "Verifierat",
  estimated: "Uppskattat",
  unknown: "Uppgift saknas",
};

export const POSITION_SOURCE_LABELS: Record<string, string> = {
  export_address: "Adress i exporten",
  api_position: "Fordonets position",
  user: "Angiven av användaren",
  geocoded: "Geokodad",
  none: "Saknas",
};

export const LOGICAL_STATE_LABELS: Record<string, string> = {
  active: "Aktiv",
  merged: "Sammanslagen",
  split: "Uppdelad",
  voided: "Makulerad",
};

export const REVIEW_STATUS_LABELS: Record<string, string> = {
  needs_review: "Behöver granskas",
  proposed: "Förslag – väntar på godkännande",
  auto: "Automatiskt klassad (preliminär)",
  approved: "Godkänd",
  imported: "Importerad",
};

export const REVIEW_STATUS_SHORT: Record<string, string> = {
  needs_review: "Granska",
  proposed: "Förslag",
  auto: "Preliminär",
  approved: "Godkänd",
  imported: "Importerad",
};

export const DECISION_SOURCE_LABELS: Record<string, string> = {
  user: "Du",
  import: "Importerad körjournal",
  rule: "Regel",
  ai: "AI",
  pattern: "Inlärt mönster",
  ha: "Svar via Home Assistant",
  system: "Systemet",
};

export const MERGE_KIND_LABELS: Record<string, string> = {
  none: "Ej sammanslagen",
  auto: "Automatiskt sammanslagen",
  prompted: "Sammanslagen efter fråga",
  manual: "Manuellt sammanslagen",
};

export const MERGE_OPERATION_LABELS: Record<string, string> = {
  merge: "Sammanslagning",
  split: "Uppdelning",
  unmerge: "Upplöst sammanslagning",
  dissolve: "Upplöst sammanslagning",
  reject: "Avböjd sammanslagning",
};

export const LOCATION_TYPE_LABELS: Record<string, string> = {
  home: "Hem",
  regular_workplace: "Ordinarie arbetsplats",
  office: "Företagskontor",
  customer: "Kund",
  hotel: "Hotell",
  private: "Privat destination",
  fuel_station: "Bensinstation",
  charging_station: "Laddstation",
  other: "Övrig",
  unknown: "Okänd",
};

export const REVIEW_KIND_LABELS: Record<string, string> = {
  category: "Oklar kategori",
  purpose: "Oklart syfte",
  merge: "Förfrågan om sammanslagning",
  missing_odometer: "Saknad mätarställning",
  data_gap: "Misstänkt datalucka",
  calendar_conflict: "Konflikt mellan kalender och fordonsdata",
  import_problem: "Importproblem",
  tax_uncertain: "Skattemässig bedömning krävs",
  mixed_allocation: "Fördela privat/tjänst",
  anomaly: "Avvikelse",
};

export const REVIEW_KIND_SHORT: Record<string, string> = {
  category: "Kategori",
  purpose: "Syfte",
  merge: "Sammanslagning",
  missing_odometer: "Mätarställning",
  data_gap: "Datalucka",
  calendar_conflict: "Kalenderkonflikt",
  import_problem: "Import",
  tax_uncertain: "Skatt",
  mixed_allocation: "Fördelning",
  anomaly: "Avvikelse",
};

export const REVIEW_STATE_LABELS: Record<string, string> = {
  open: "Öppen",
  resolved: "Hanterad",
  dismissed: "Avfärdad",
  snoozed: "Uppskjuten",
};

export const INTEGRATION_PROVIDER_LABELS: Record<string, string> = {
  volvo: "Volvo Cars",
  microsoft: "Microsoft Outlook",
  home_assistant: "Home Assistant",
  ai: "AI-tjänst",
  geocoder: "Geokodning",
};

export const INTEGRATION_STATUS_LABELS: Record<string, string> = {
  not_configured: "Inte konfigurerad",
  configured: "Konfigurerad – ej verifierad",
  demo: "Demoläge",
  connected: "Ansluten",
  needs_reauth: "Behöver logga in igen",
  error: "Fel",
};

export const NOTIFICATION_KIND_LABELS: Record<string, string> = {
  classify: "Klassning",
  purpose: "Syfte",
  merge: "Sammanslagning",
  digest: "Samlad fråga",
  weekly: "Veckorapport",
  daily: "Daglig avstämning",
  test: "Testnotis",
};

export const NOTIFICATION_STATUS_LABELS: Record<string, string> = {
  pending: "Väntar",
  sent: "Skickad",
  answered: "Besvarad",
  expired: "Utgången",
  cancelled: "Avbruten",
  failed: "Misslyckad",
};

export const IMPORT_ROW_STATUS_LABELS: Record<string, string> = {
  ok: "Ny",
  duplicate: "Dubblett",
  duplicate_differs: "Dubblett med avvikelser",
  error: "Fel",
  imported: "Importerad",
  skipped: "Överhoppad",
};

export const BATCH_STATUS_LABELS: Record<string, string> = {
  preview: "Förhandsgranskning",
  committed: "Bekräftad",
  undone: "Ångrad",
  failed: "Misslyckad",
};

export const COVERAGE_LABELS: Record<string, string> = {
  complete: "Komplett fordonsjournal",
  selection: "Importerat urval",
  incomplete: "Ofullständig period",
  unknown: "Okänd täckning",
  no_data: "Inga data",
};

export const GAP_KIND_LABELS: Record<string, string> = {
  odometer: "Lucka i mätarställningen",
  collection: "Avbrott i datainsamlingen",
  multi_trip: "Möjligen flera körningar",
};

export const GAP_STATUS_LABELS: Record<string, string> = {
  open: "Öppen",
  resolved_private: "Intygad som privat körning",
  resolved_filled: "Ifylld",
  ignored: "Lämnad oredovisad",
};

export const PATTERN_STATUS_LABELS: Record<string, string> = {
  suggested: "Förslag",
  approved: "Godkänt",
  rejected: "Avvisat",
};

export const QUALITY_FLAG_LABELS: Record<string, string> = {
  missing_odometer: "Mätarställning saknas",
  missing_time: "Tidpunkt saknas",
  distance_estimated: "Sträckan är uppskattad",
  possible_missed_trips: "Möjligen fler körningar",
  summary_of_gap: "Sammanfattning av lucka",
  user_entered: "Inmatad av användaren",
};

export const EVIDENCE_DIRECTION_LABELS: Record<string, string> = {
  work: "Talar för arbete",
  private: "Talar för privat",
  neutral: "Neutral",
};

export const SYNC_STATUS_LABELS = INTEGRATION_STATUS_LABELS;

export const AUDIT_ACTION_LABELS: Record<string, string> = {
  "trip.update": "Resan ändrades",
  "trip.classify": "Klassning",
  "trip.approve": "Godkänd",
  "trip.allocate": "Fördelning privat/tjänst",
  "trip.manual_create": "Manuellt inlagd",
  "trip.manual_delete": "Borttagen",
  "trip.merge": "Sammanslagning",
  "trip.unmerge": "Sammanslagning ångrad",
  "trip.split": "Uppdelning",
  "calendar.match_choice": "Kalenderträff vald/avvisad",
  "feedback.undo": "Ändring ångrad",
};
