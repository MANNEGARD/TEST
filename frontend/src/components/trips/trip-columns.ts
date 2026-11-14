/**
 * Kolumner i körjournalens tabell. `sort` = fält för serversidig sortering.
 * `defaultVisible` styr standardvalet i kolumnväljaren (sparas i localStorage).
 */
export interface TripColumn {
  id: string;
  label: string;
  sort?: "date" | "distance" | "category" | "status" | "start" | "end" | "odometer" | "purpose";
  align?: "left" | "right";
  defaultVisible: boolean;
  required?: boolean;
}

export const TRIP_COLUMNS: TripColumn[] = [
  { id: "date", label: "Datum", sort: "date", defaultVisible: true, required: true },
  { id: "start_time", label: "Start", defaultVisible: true },
  { id: "end_time", label: "Slut", defaultVisible: true },
  { id: "start_address", label: "Från", sort: "start", defaultVisible: true },
  { id: "end_address", label: "Till", sort: "end", defaultVisible: true },
  { id: "odometer_start", label: "Mätare start", sort: "odometer", align: "right", defaultVisible: false },
  { id: "odometer_end", label: "Mätare slut", align: "right", defaultVisible: false },
  { id: "distance", label: "Avstånd", sort: "distance", align: "right", defaultVisible: true, required: true },
  { id: "category", label: "Kategori", sort: "category", defaultVisible: true, required: true },
  { id: "purpose", label: "Syfte", sort: "purpose", defaultVisible: true },
  { id: "ai", label: "AI-status", defaultVisible: false },
  { id: "source", label: "Källa", defaultVisible: true },
  { id: "status", label: "Granskning", sort: "status", defaultVisible: true },
];

export const DEFAULT_VISIBLE = TRIP_COLUMNS.filter((c) => c.defaultVisible).map((c) => c.id);
