"use client";

import Link from "next/link";
import { ArrowRight, Check, TriangleAlert, UtensilsCrossed } from "lucide-react";
import { Input } from "@/components/ui/input";
import { formatNumber } from "@/lib/format";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { BoolRow, FormCard, GroupHeading, Note, NumberRow, SectionIntro, SettingRow, SettingsSaveBar } from "./form-parts";
import { sectionHref } from "./sections";
import { useSectionSave, useSettingsForm, type FieldSpecs, type SettingsForm } from "./use-settings-form";

/** Gränsvärden enligt backend/app/services/settings.py (sektionen lunch, byggkontraktet 7.A.1). */
const FIELDS: FieldSpecs = {
  "lunch.enabled": { type: "bool" },
  "lunch.window_start": { type: "time", name: "Lunchfönster från" },
  "lunch.window_end": { type: "time", name: "Lunchfönster till" },
  "lunch.max_drive_minutes": { type: "number", name: "Längsta körtid", min: 1, max: 60, integer: true },
  "lunch.max_distance_km": { type: "number", name: "Längsta sträcka", min: 0.5, max: 50, decimals: 1 },
  "lunch.min_stop_minutes": { type: "number", name: "Kortaste stopp", min: 0, max: 180, integer: true },
  "lunch.max_stop_minutes": { type: "number", name: "Längsta stopp", min: 1, max: 240, integer: true },
};

const WINDOW_ERROR = "Lunchfönstrets början måste ligga före slutet.";
const STOP_ERROR = "Kortaste stoppet får inte vara längre än det längsta.";
/** Marginalen efter stopp och körtid innan DriveLog slutar vänta på återresan (RETURN_SLACK i lunch.py). */
const RETURN_SLACK_MIN = 10;

function minutesOf(time: string): number | null {
  const m = /^(\d{2}):(\d{2})$/.exec(time);
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function TimeInput({ form, path, id, label }: { form: SettingsForm; path: string; id: string; label: string }) {
  return (
    <Input
      id={id}
      type="time"
      aria-label={label}
      value={form.text(path)}
      onChange={(e) => form.set(path, e.target.value)}
      aria-invalid={form.error(path) ? true : undefined}
      className="w-[7.5rem] shrink-0 num"
    />
  );
}

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[11px] font-semibold text-accent-ink num" aria-hidden>
        {n}
      </span>
      <span className="text-[13px] leading-relaxed text-fg">{children}</span>
    </li>
  );
}

const num = (v: number | null, fallback: string) => (v == null ? fallback : formatNumber(v, 1, 0));

/**
 * Lunchregeln (byggkontraktet 7.C, tilläggets 3.H): en kort tur från ett av arbetsgivarens kontor
 * mitt på dagen, ett kort stopp och tillbaka till samma kontor blir privat utan fråga. Gränserna
 * kan ändras och regeln stängas av. Texten säger exakt vilket mönster som krävs.
 */
export function LunchSection({ data }: { data: SettingsPayload }) {
  const base = useSettingsForm(data, FIELDS, (n) => {
    const low = n("lunch.min_stop_minutes");
    const high = n("lunch.max_stop_minutes");
    return low != null && high != null && low > high ? { "lunch.min_stop_minutes": STOP_ERROR } : {};
  });
  // Klockslagen jämförs här (korsvalideringen ovan ser bara tal).
  const startMin = minutesOf(base.text("lunch.window_start"));
  const endMin = minutesOf(base.text("lunch.window_end"));
  const windowError = startMin != null && endMin != null && startMin >= endMin && !base.error("lunch.window_start") && !base.error("lunch.window_end") ? WINDOW_ERROR : null;
  const form: SettingsForm = windowError
    ? { ...base, firstError: base.firstError ?? windowError, error: (p) => (p === "lunch.window_end" ? windowError : base.error(p)) }
    : base;
  const save = useSectionSave(form, "Lunchregeln är sparad.");
  useReportDirty("lunch", form.dirty);

  if (!form.has("lunch.enabled")) {
    return (
      <section aria-labelledby="settings-lunch">
        <SectionIntro id="settings-lunch" title="Lunchregeln" />
        <Note>Servern känner inte till lunchregeln ännu. Uppdatera DriveLog för att använda den.</Note>
      </section>
    );
  }

  const enabled = form.bool("lunch.enabled");
  const officeReady = data.status?.lunch_office_ready !== false;
  const manual = data.settings.automation?.mode === "manual";
  const start = form.text("lunch.window_start") || "11:00";
  const end = form.text("lunch.window_end") || "13:00";
  const maxDrive = form.number("lunch.max_drive_minutes");
  const maxKm = form.number("lunch.max_distance_km");
  const minStop = form.number("lunch.min_stop_minutes");
  const maxStop = form.number("lunch.max_stop_minutes");
  const waitMinutes = maxStop != null && maxDrive != null ? maxStop + maxDrive + RETURN_SLACK_MIN : null;
  const timeError = form.error("lunch.window_start") ?? form.error("lunch.window_end");

  return (
    <section aria-labelledby="settings-lunch">
      <SectionIntro id="settings-lunch" title="Lunchregeln">
        Korta turer från kontoret mitt på dagen är oftast lunch. När hela mönstret stämmer blir båda resorna privata utan att du får någon fråga.
      </SectionIntro>

      <FormCard onSubmit={save.save} aria-label="Lunchregeln">
        <BoolRow form={form} path="lunch.enabled" id="lunch-enabled" label="Använd lunchregeln" hint="Avstängd bedöms lunchturer som vanliga resor." />

        {!officeReady ? (
          <div className="px-5 py-4">
            <Note tone="warn" icon={<TriangleAlert />}>
              <p data-testid="lunch-office-missing">
                Lunchregeln kräver att minst ett kontor finns som bekräftad plats. Markera era kontor under Platser eller Tjänsteställe.
              </p>
              <p className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
                <Link href="/places" className="inline-flex items-center gap-1 font-medium underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring">
                  Öppna Platser <ArrowRight className="size-3.5" aria-hidden />
                </Link>
                <Link
                  href={sectionHref("tjanstestalle")}
                  className="inline-flex items-center gap-1 font-medium underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
                >
                  Öppna Tjänsteställe <ArrowRight className="size-3.5" aria-hidden />
                </Link>
              </p>
            </Note>
          </div>
        ) : null}

        <div className={cn("divide-y divide-border", !enabled && "opacity-60")}>
          <GroupHeading>Så här måste det se ut – annars gäller den vanliga hanteringen</GroupHeading>
          <div className="space-y-3 px-5 py-4" data-testid="lunch-pattern">
            <ol className="space-y-2">
              <Step n={1}>Resan börjar vid ett av era kontor en arbetsdag.</Step>
              <Step n={2}>
                Den är kort: högst <span className="num font-medium">{num(maxDrive, "–")}</span> minuter och{" "}
                <span className="num font-medium">{num(maxKm, "–")}</span> km.
              </Step>
              <Step n={3}>
                Du stannar mellan <span className="num font-medium">{num(minStop, "–")}</span> och <span className="num font-medium">{num(maxStop, "–")}</span>{" "}
                minuter på samma ställe.
              </Step>
              <Step n={4}>
                Du kör lika kort tillbaka till samma kontor, och allt sker mellan <span className="num font-medium">{start}</span> och{" "}
                <span className="num font-medium">{end}</span>.
              </Step>
            </ol>
            <ul className="space-y-1.5 border-t border-border pt-3 text-[13px] leading-relaxed text-fg-muted">
              <li className="flex gap-2">
                <Check className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                <span>
                  Regeln gäller inte om stället är en bekräftad kund eller arbetsplats, om du har en egen regel för det, om du tidigare har sagt att en resa dit var
                  i tjänsten, om kartan säger kontor eller industri, eller om en kalenderbokning stämmer med resan. Säger kartan restaurang, kafé eller butik räknas
                  det som ett starkare tecken.
                </span>
              </li>
              <li className="flex gap-2">
                <Check className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                <span>
                  Du får ingen fråga om första resan medan du äter: DriveLog väntar på återresan. Kommer ingen återresa inom{" "}
                  <span className="num">{waitMinutes == null ? "–" : formatNumber(waitMinutes, 0)}</span> minuter hanteras resan som vanligt.
                </span>
              </li>
              <li className="flex gap-2">
                <Check className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                <span>Klockslaget ensamt gör aldrig en resa privat – hela mönstret krävs.</span>
              </li>
              <li className="flex gap-2">
                <Check className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                <span data-testid="lunch-home">
                  Åker du hem och sedan tillbaka till samma kontor enligt mönstret görs resorna inte privata av sig själva. Har du valt att resor mellan hemmet och
                  kontoren är tjänsteresor godkänns de inte heller automatiskt som tjänst – du får en fråga när du är tillbaka på kontoret.
                </span>
              </li>
              {manual ? (
                <li className="flex gap-2">
                  <UtensilsCrossed className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                  <span data-testid="lunch-manual">I läget Jag godkänner allt själv blir lunchresorna förslag på Privat som du godkänner.</span>
                </li>
              ) : null}
            </ul>
          </div>

          <GroupHeading>Gränser</GroupHeading>
          <SettingRow label="Lunchfönster" hint="Både utresan och återresan ska ligga inom fönstret." error={timeError}>
            <div className="flex items-center gap-2">
              <TimeInput form={form} path="lunch.window_start" id="lunch-start" label="Lunchfönster från" />
              <span className="text-sm text-fg-muted" aria-hidden>
                –
              </span>
              <TimeInput form={form} path="lunch.window_end" id="lunch-end" label="Lunchfönster till" />
            </div>
          </SettingRow>
          <NumberRow form={form} path="lunch.max_drive_minutes" id="lunch-drive" label="Längsta körtid" unit="min" hint="Varje väg. Är körtiden okänd avgör bara sträckan." />
          <NumberRow form={form} path="lunch.max_distance_km" id="lunch-km" label="Längsta sträcka" unit="km" decimal hint="Varje väg." />
          <NumberRow form={form} path="lunch.min_stop_minutes" id="lunch-min-stop" label="Kortaste stopp" unit="min" />
          <NumberRow form={form} path="lunch.max_stop_minutes" id="lunch-max-stop" label="Längsta stopp" unit="min" hint="Är stoppets längd okänd blir det ingen lunch." />
        </div>
        <SettingsSaveBar form={form} save={save} defaults />
      </FormCard>
    </section>
  );
}
