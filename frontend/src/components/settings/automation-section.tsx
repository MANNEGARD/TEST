"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Input } from "@/components/ui/input";
import { formatDateTime } from "@/lib/format";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { ChoiceCards, FormCard, GroupHeading, Note, SectionIntro, SettingRow, SettingsSaveBar } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

/** Kontorstid som backend sparar den (services/settings.py, _validate_office_hours). */
export interface OfficeHours {
  days: number[];
  start: string;
  end: string;
}

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Samma regler som backend: minst en dag, giltiga klockslag och start före slut. */
export function officeHoursError(value: unknown): string | null {
  const v = value as Partial<OfficeHours> | null;
  if (!v || !Array.isArray(v.days)) return "Kontorstiden saknas.";
  if (!v.days.length) return "Välj minst en dag för kontorstiden.";
  if (!TIME_RE.test(String(v.start)) || !TIME_RE.test(String(v.end))) return "Kontorstiden anges som klockslag (TT:MM).";
  if (String(v.start) >= String(v.end)) return "Kontorstiden måste börja före den slutar.";
  return null;
}

/** Gränsvärden enligt backend/app/services/settings.py (_CHOICES, _BOUNDS). */
const FIELDS: FieldSpecs = {
  "automation.mode": { type: "choice", options: ["self_driving", "manual"] },
  "automation.office_hours": { type: "json", validate: officeHoursError },
  "automation.assumption_veto_hours": { type: "number", name: "Vetotiden", min: 0, max: 168, integer: true },
};

const WEEKDAYS = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"];
const WEEKDAY_NAMES = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"];

/** Webbens val för vetotiden. Ett annat värde (satt tidigare) visas som ett eget val. */
const VETO_CHOICES: { value: string; title: string; description: string }[] = [
  {
    value: "0",
    title: "Fråga mig först",
    description: "Inget antas. Ett troligt kundbesök blir en snabbknapp i en vanlig fråga i telefonen.",
  },
  {
    value: "24",
    title: "1 dygn",
    description: "Resan räknas som tjänst efter ett dygn om du inte säger emot.",
  },
  {
    value: "72",
    title: "3 dygn",
    description: "Resan räknas som tjänst efter tre dygn om du inte säger emot.",
  },
];

/** Skälen som visas på resor som godkänts automatiskt (byggkontraktet A.2). */
const REASONS: { title: string; text: string }[] = [
  { title: "Din regel för …", text: "Du har själv sagt hur resor till platsen eller på rutten ska räknas." },
  {
    title: "Som de 3 senaste gångerna",
    text: "Samma rutt med samma kategori de tre senaste gångerna du själv godkände den – för tjänst även samma ärende och liknande sträcka.",
  },
  { title: "Hemresa efter …", text: "Hem samma dag efter en godkänd tjänsteresa, ungefär lika lång som utresan." },
  { title: "Privat plats", text: "Mellan hemmet och platser som du har märkt som privata. Resor mellan bostad och arbetsplats blir privata på samma sätt." },
  {
    title: "Antaget kundbesök",
    text: "En arbetsplats med namn i kartan under kontorstid. Du får en notis första gången och kan säga emot under vetotiden. Antagna resor märks \"Antaget\".",
  },
];

/** Automatik – självgående läge eller "jag godkänner allt själv", kontorstid och vetotid. */
export function AutomationSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS);
  const save = useSectionSave(form, "Automatiken är sparad.");
  useReportDirty("automatik", form.dirty);

  const mode = form.text("automation.mode");
  const selfDriving = mode === "self_driving";
  const hours = form.has("automation.office_hours") ? form.json<OfficeHours>("automation.office_hours") : null;
  const setHours = (patch: Partial<OfficeHours>) => hours && form.set("automation.office_hours", { ...hours, ...patch });
  const toggleDay = (day: number) => {
    if (!hours) return;
    const days = hours.days.includes(day) ? hours.days.filter((d) => d !== day) : [...hours.days, day].sort((a, b) => a - b);
    setHours({ days });
  };
  const veto = form.text("automation.assumption_veto_hours");
  const vetoOptions = VETO_CHOICES.some((c) => c.value === veto) || !veto
    ? VETO_CHOICES
    : [...VETO_CHOICES, { value: veto, title: `${veto} timmar`, description: "Ett värde som satts tidigare." }];
  const activeSince = data.settings.automation?.active_since;
  // Datumet gäller bara om det sparade läget redan är självgående: byter du tillbaka från
  // "Jag godkänner allt själv" sätts en ny starttid när du sparar.
  const savedSelfDriving = data.settings.automation?.mode === "self_driving";
  const since = savedSelfDriving && typeof activeSince === "string" && activeSince ? formatDateTime(activeSince) : null;

  return (
    <section aria-labelledby="settings-automatik">
      <SectionIntro id="settings-automatik" title="Automatik">
        Välj om DriveLog ska sköta körjournalen själv eller om du vill godkänna varje resa. Importerade och manuellt inlagda resor rörs aldrig.
      </SectionIntro>

      <FormCard onSubmit={save.save} aria-label="Automatik">
        {form.has("automation.mode") ? (
          <ChoiceCards
            legend="Hur ska resorna godkännas?"
            name="automation-mode"
            value={mode}
            onChange={(v) => form.set("automation.mode", v)}
            options={[
              {
                value: "self_driving",
                title: "Självgående",
                isDefault: form.defaultOf("automation.mode") === "self_driving",
                description:
                  "Resor som går att avgöra säkert godkänns av sig själva, med skälet utskrivet. Resten blir en fråga i telefonen. Månadens underlag skapas av sig självt.",
              },
              {
                value: "manual",
                title: "Jag godkänner allt själv",
                isDefault: form.defaultOf("automation.mode") === "manual",
                description: "Inget godkänns automatiskt. Resorna väntar i Granskning tills du har gått igenom dem.",
              },
            ]}
          />
        ) : null}
        {selfDriving ? (
          <div className="px-5 py-3 text-xs leading-relaxed text-fg-muted" data-testid="automation-since">
            {since
              ? `Självgående sedan ${since}. Resor från före dess rörs inte.`
              : "Självgående läge börjar gälla när du sparar. Resor från före dess rörs inte."}
          </div>
        ) : null}

        <GroupHeading description="Gäller bara i självgående läge.">Antagna kundbesök</GroupHeading>
        <div className={cn("divide-y divide-border", !selfDriving && "opacity-60")}>
          {hours ? (
            <SettingRow
              label="Kontorstid"
              hint="Ett okänt företag antas bara vara ett kundbesök om du stannade där under kontorstid. Utanför den får du en fråga i stället."
              error={form.error("automation.office_hours")}
              stacked
            >
              <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
                <div role="group" aria-label="Dagar" className="flex flex-wrap gap-1.5">
                  {WEEKDAYS.map((d, i) => {
                    const on = hours.days.includes(i);
                    return (
                      <button
                        key={d}
                        type="button"
                        aria-pressed={on}
                        aria-label={WEEKDAY_NAMES[i]}
                        disabled={!selfDriving}
                        onClick={() => toggleDay(i)}
                        className={cn(
                          "h-8 min-w-11 rounded-md border px-2 text-[13px] font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed",
                          on ? "border-accent bg-accent-soft text-accent-ink" : "border-border bg-surface text-fg-muted hover:text-fg",
                        )}
                      >
                        {d}
                      </button>
                    );
                  })}
                </div>
                <div className="flex items-center gap-2">
                  <Input
                    id="auto-office-start"
                    type="time"
                    aria-label="Kontorstid från"
                    value={hours.start}
                    disabled={!selfDriving}
                    onChange={(e) => setHours({ start: e.target.value })}
                    aria-invalid={form.error("automation.office_hours") ? true : undefined}
                    className="w-[7.5rem] num"
                  />
                  <span className="text-sm text-fg-muted" aria-hidden>
                    –
                  </span>
                  <Input
                    id="auto-office-end"
                    type="time"
                    aria-label="Kontorstid till"
                    value={hours.end}
                    disabled={!selfDriving}
                    onChange={(e) => setHours({ end: e.target.value })}
                    aria-invalid={form.error("automation.office_hours") ? true : undefined}
                    className="w-[7.5rem] num"
                  />
                </div>
              </div>
            </SettingRow>
          ) : null}
          {form.has("automation.assumption_veto_hours") ? (
            <ChoiceCards
              legend="Vetotid"
              hint="Hur länge du kan säga emot ett antaget kundbesök innan det räknas med i månadens underlag."
              name="automation-veto"
              value={veto}
              onChange={(v) => form.set("automation.assumption_veto_hours", v)}
              options={vetoOptions.map((c) => ({
                ...c,
                isDefault: String(form.defaultOf("automation.assumption_veto_hours")) === c.value,
              }))}
            />
          ) : null}
        </div>

        <GroupHeading>Så avgörs en resa</GroupHeading>
        <div className="space-y-3 px-5 py-4">
          <p className="text-[13px] leading-relaxed text-fg-muted">
            En resa godkänns bara automatiskt av något av skälen nedan. Skälet står på resan, till exempel &quot;Godkänd automatiskt · Din regel för
            Acme&quot;. Allt annat blir en fråga. Inget godkänns som bara vilar på en kalenderrubrik eller en plats du inte bekräftat.
          </p>
          <ul className="space-y-2" data-testid="automation-reasons">
            {REASONS.map((r) => (
              <li key={r.title} className="text-[13px] leading-relaxed">
                <span className="font-medium text-fg">{r.title}</span>
                <span className="text-fg-muted"> – {r.text}</span>
              </li>
            ))}
          </ul>
          <Note>
            Rättar du en automatisk resa gäller ditt beslut. En regel som slog fel frågar dig i stället nästa gång.{" "}
            <Link
              href="/places?tab=minne"
              className="inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
            >
              Se och ändra dina egna regler
              <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          </Note>
        </div>
        <SettingsSaveBar form={form} save={save} defaults />
      </FormCard>
    </section>
  );
}
