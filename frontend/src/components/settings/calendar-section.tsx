"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { formatNumber, formatPercent } from "@/lib/format";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { Advanced, BoolRow, FormCard, NumberRow, SectionIntro, SettingRow, SettingsSaveBar, Slider } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

/** Gränsvärden enligt backend/app/services/settings.py (_BOUNDS). Tidsfönstren saknar gräns i backend – rimliga gränser här. */
const FIELDS: FieldSpecs = {
  "calendar.enabled": { type: "bool" },
  "calendar.store_body_preview": { type: "bool" },
  "calendar.arrival_window_before_minutes": { type: "number", name: "Tidsfönstret före mötet", min: 0, max: 24 * 60, integer: true },
  "calendar.arrival_window_after_minutes": { type: "number", name: "Tidsfönstret efter mötets start", min: 0, max: 24 * 60, integer: true },
  "calendar.min_match_score": { type: "number", name: "Minsta träffpoäng", min: 0, max: 1 },
  "calendar.sync_days_back": { type: "number", name: "Hämta bakåt", min: 0, max: 3650, integer: true },
  "calendar.sync_days_forward": { type: "number", name: "Hämta framåt", min: 0, max: 365, integer: true },
};

/** 6. Kalender – möten som hjälper till att föreslå syfte och kategori. */
export function CalendarSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS);
  const save = useSectionSave(form, "Inställningarna för kalendern är sparade.");
  useReportDirty("kalender", form.dirty);

  const enabled = !form.has("calendar.enabled") || form.bool("calendar.enabled");
  const score = form.number("calendar.min_match_score") ?? 0.45;
  const std = (path: string, unit: string) => {
    const v = form.defaultOf(path);
    return typeof v === "number" ? ` Standard: ${formatNumber(v, 2, 0)} ${unit}.` : "";
  };
  const advancedPaths = [
    "calendar.arrival_window_before_minutes",
    "calendar.arrival_window_after_minutes",
    "calendar.min_match_score",
    "calendar.sync_days_back",
    "calendar.sync_days_forward",
  ];

  return (
    <section aria-labelledby="settings-kalender">
      <SectionIntro id="settings-kalender" title="Kalender">
        Har du ett möte där resan slutar blir förslaget på syfte mycket bättre. Kalendern kopplas under{" "}
        <Link
          href="/integrations"
          className="inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
        >
          Integrationer
          <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </SectionIntro>

      <FormCard onSubmit={save.save} aria-label="Kalender">
        <BoolRow
          form={form}
          path="calendar.enabled"
          id="cal-enabled"
          label="Använd kalendern för förslag"
          hint="Avstängt hämtas inga möten och förslagen bygger bara på platser och dina tidigare val."
        />
        <div className={cn("divide-y divide-border", !enabled && "opacity-60")}>
          <BoolRow
            form={form}
            path="calendar.store_body_preview"
            id="cal-body"
            label="Spara början av mötets beskrivning"
            hint="Ger lite bättre syftesförslag men sparar mer av din kalender på servern. Av som standard – rubrik, tid och plats räcker oftast."
          />
          <Advanced
            summary="tidsfönster och träffsäkerhet"
            hasError={advancedPaths.some((p) => !!form.error(p))}
          >
            <NumberRow
              form={form}
              path="calendar.arrival_window_before_minutes"
              id="cal-before"
              label="Ankomst före mötet, högst"
              unit="min"
              hint={`Hur tidigt du kan komma fram och resan ändå räknas som resa till mötet.${std("calendar.arrival_window_before_minutes", "min")}`}
            />
            <NumberRow
              form={form}
              path="calendar.arrival_window_after_minutes"
              id="cal-after"
              label="Ankomst efter mötets start, högst"
              unit="min"
              hint={`Hur sent du kan komma fram och resan ändå räknas som resa till mötet.${std("calendar.arrival_window_after_minutes", "min")}`}
            />
            {form.has("calendar.min_match_score") ? (
              <SettingRow
                stacked
                htmlFor="cal-score"
                label={
                  <span className="flex items-baseline justify-between gap-3">
                    Minsta träffpoäng
                    <span className="num text-sm">{formatPercent(score)}</span>
                  </span>
                }
                hint="Hur säkert det måste vara att mötet hör till resan (tid och plats vägs ihop). Lägre värde ger fler men osäkrare träffar."
                error={form.error("calendar.min_match_score")}
              >
                <Slider
                  id="cal-score"
                  min={0}
                  max={1}
                  step={0.05}
                  value={score}
                  onChange={(v) => form.set("calendar.min_match_score", String(v))}
                  valueText={`${Math.round(score * 100)} procent`}
                  describedBy="cal-score-hint"
                />
              </SettingRow>
            ) : null}
            <NumberRow
              form={form}
              path="calendar.sync_days_back"
              id="cal-back"
              label="Hämta möten bakåt"
              unit="dagar"
              hint={`Hur långt tillbaka kalendern läses, t.ex. för att granska äldre resor.${std("calendar.sync_days_back", "dagar")}`}
            />
            <NumberRow
              form={form}
              path="calendar.sync_days_forward"
              id="cal-forward"
              label="Hämta möten framåt"
              unit="dagar"
              hint={`Kommande möten som redan finns när resan registreras.${std("calendar.sync_days_forward", "dagar")}`}
            />
          </Advanced>
        </div>
        <SettingsSaveBar form={form} save={save} defaults />
      </FormCard>
    </section>
  );
}
