import {
  Bell,
  Building2,
  CalendarDays,
  Car,
  DatabaseBackup,
  HandCoins,
  KeyRound,
  Merge,
  ShieldCheck,
  Sparkles,
  WholeWord,
  type LucideIcon,
} from "lucide-react";

/**
 * Inställningssidans sektioner i visningsordning – det vanligaste först.
 * `key` är värdet i URL:en (/settings?sektion=notiser).
 */
export type SectionKey =
  | "fordon"
  | "ersattning"
  | "sammanslagning"
  | "forslag"
  | "notiser"
  | "kalender"
  | "tjanstestalle"
  | "forkortningar"
  | "sekretess"
  | "sakerhet"
  | "sakerhetskopia";

export interface SectionDef {
  key: SectionKey;
  label: string;
  /** Kort etikett för flikarna på mobil. */
  short: string;
  icon: LucideIcon;
  group: "journal" | "automation" | "data";
}

export const SECTIONS: SectionDef[] = [
  { key: "fordon", label: "Fordon och förare", short: "Fordon", icon: Car, group: "journal" },
  { key: "ersattning", label: "Milersättning och export", short: "Milersättning", icon: HandCoins, group: "journal" },
  { key: "sammanslagning", label: "Sammanslagning av resor", short: "Sammanslagning", icon: Merge, group: "journal" },
  { key: "forslag", label: "Förslag och AI", short: "Förslag och AI", icon: Sparkles, group: "automation" },
  { key: "notiser", label: "Notiser", short: "Notiser", icon: Bell, group: "automation" },
  { key: "kalender", label: "Kalender", short: "Kalender", icon: CalendarDays, group: "automation" },
  { key: "tjanstestalle", label: "Tjänsteställe", short: "Tjänsteställe", icon: Building2, group: "automation" },
  { key: "forkortningar", label: "Förkortningar", short: "Förkortningar", icon: WholeWord, group: "automation" },
  { key: "sekretess", label: "Sekretess", short: "Sekretess", icon: ShieldCheck, group: "data" },
  { key: "sakerhet", label: "Säkerhet", short: "Säkerhet", icon: KeyRound, group: "data" },
  { key: "sakerhetskopia", label: "Säkerhetskopia", short: "Säkerhetskopia", icon: DatabaseBackup, group: "data" },
];

export const SECTION_GROUP_LABELS: Record<SectionDef["group"], string> = {
  journal: "Körjournal",
  automation: "Automatik",
  data: "Data och konto",
};

export const DEFAULT_SECTION: SectionKey = "fordon";

export function isSectionKey(value: string | null | undefined): value is SectionKey {
  return !!value && SECTIONS.some((s) => s.key === value);
}

/** Länk till en sektion, t.ex. från en annan vy. */
export function sectionHref(key: SectionKey): string {
  return key === DEFAULT_SECTION ? "/settings" : `/settings?sektion=${key}`;
}
