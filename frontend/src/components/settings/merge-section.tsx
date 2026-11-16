"use client";

import { formatNumber } from "@/lib/format";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { Advanced, BoolRow, FormCard, NumberRow, SectionIntro, SettingsSaveBar } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

/** Gränsvärden enligt backend/app/services/settings.py (_BOUNDS). */
const FIELDS: FieldSpecs = {
  "merge.enabled": { type: "bool" },
  "merge.auto_max_minutes": { type: "number", name: "Gränsen för automatisk sammanslagning", min: 0, max: 240 },
  "merge.ask_max_minutes": { type: "number", name: "Gränsen för fråga", min: 0, max: 1440 },
  "merge.odometer_tolerance_km": { type: "number", name: "Toleransen i mätarställning", min: 0, max: 20 },
};

function minutes(value: number | null): string {
  return value == null ? "…" : `${formatNumber(value, 1, 0)} min`;
}

function Rule({ letter, title, when, children, tone }: { letter: string; title: string; when: string; children: React.ReactNode; tone: string }) {
  return (
    <li className="flex gap-3 rounded-lg border border-border bg-surface-2/40 p-3.5">
      <span className={cn("flex size-6 shrink-0 items-center justify-center rounded-full num text-xs font-semibold", tone)} aria-hidden>
        {letter}
      </span>
      <div className="min-w-0">
        <p className="text-[13px] font-medium text-fg">
          <span className="sr-only">Regel {letter}: </span>
          {title}
        </p>
        <p className="mt-0.5 num text-xs text-fg">{when}</p>
        <p className="mt-1.5 text-xs leading-relaxed text-fg-muted">{children}</p>
      </div>
    </li>
  );
}

/** 3. Sammanslagning av resor – regel A (automatiskt), B (fråga) och C (separata). */
export function MergeSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS, (num) => {
    const auto = num("merge.auto_max_minutes");
    const ask = num("merge.ask_max_minutes");
    return auto != null && ask != null && ask < auto
      ? { "merge.ask_max_minutes": "Gränsen för fråga måste vara minst lika stor som gränsen för automatisk sammanslagning." }
      : {};
  });
  const save = useSectionSave(form, "Reglerna för sammanslagning är sparade.");
  useReportDirty("sammanslagning", form.dirty);

  const enabled = !form.has("merge.enabled") || form.bool("merge.enabled");
  const auto = form.number("merge.auto_max_minutes");
  const ask = form.number("merge.ask_max_minutes");
  const defAuto = form.defaultOf("merge.auto_max_minutes");
  const defAsk = form.defaultOf("merge.ask_max_minutes");
  const defTol = form.defaultOf("merge.odometer_tolerance_km");
  const std = (v: unknown, unit: string) => (typeof v === "number" ? ` Standard: ${formatNumber(v, 1, 0)} ${unit}.` : "");

  return (
    <section aria-labelledby="settings-sammanslagning">
      <SectionIntro id="settings-sammanslagning" title="Sammanslagning av resor">
        Bilen delar upp en resa varje gång du stannar. Här bestämmer du hur korta stopp som ska räknas som en och samma resa – till
        exempel när du tankar eller hämtar något på vägen.
      </SectionIntro>

      <FormCard onSubmit={save.save} aria-label="Sammanslagning av resor">
        <BoolRow
          form={form}
          path="merge.enabled"
          id="merge-enabled"
          label="Slå samman resor med korta stopp"
          hint="Avstängt lämnas alla resor som de är. Du kan fortfarande slå samman resor själv i körjournalen."
        />
        <div className={cn("divide-y divide-border", !enabled && "opacity-60")}>
          <NumberRow
            form={form}
            path="merge.auto_max_minutes"
            id="merge-auto"
            label="Slå samman automatiskt vid stopp på högst"
            unit="min"
            decimal
            hint={`Kortare stopp än så räknas som samma resa utan att du behöver göra något.${std(defAuto, "min")}`}
          />
          <NumberRow
            form={form}
            path="merge.ask_max_minutes"
            id="merge-ask"
            label="Fråga mig vid stopp på högst"
            unit="min"
            decimal
            hint={`Stopp mellan de två gränserna ger en fråga i Granskning: samma resa eller två?${std(defAsk, "min")}`}
          />
          <div className="px-5 py-4">
            <h3 className="text-[13px] font-medium text-fg">Så här behandlas ett stopp</h3>
            <ol className="mt-3 grid gap-2.5 sm:grid-cols-3">
              <Rule letter="A" title="Slås samman" when={`Stopp upp till ${minutes(auto)}`} tone="bg-ok-soft text-ok-ink">
                Resorna blir en resa automatiskt.
              </Rule>
              <Rule letter="B" title="Du får en fråga" when={`${minutes(auto)} – ${minutes(ask)}`} tone="bg-accent-soft text-accent-ink">
                Du väljer själv: samma resa eller två.
              </Rule>
              <Rule letter="C" title="Förblir två resor" when={`Längre än ${minutes(ask)}`} tone="bg-surface-3 text-fg-muted">
                Slås bara samman om du gör det själv.
              </Rule>
            </ol>
            <p className="mt-3 text-xs leading-relaxed text-fg-muted">
              En sammanslagning går alltid att ångra, och privat körning görs aldrig om till tjänsteresa genom att resor slås samman.
            </p>
          </div>
        </div>
        <Advanced summary="tolerans i mätarställning" hasError={!!form.error("merge.odometer_tolerance_km")}>
          <NumberRow
            form={form}
            path="merge.odometer_tolerance_km"
            id="merge-tolerance"
            label="Tolerans i mätarställning"
            unit="km"
            decimal
            hint={`Så mycket får mätarställningen skilja mellan två resor som hör ihop. Bilen avrundar till hela kilometer, så upp till 2 km räknas alltid som avrundning och ger aldrig något ärende.${std(defTol, "km")}`}
          />
        </Advanced>
        <SettingsSaveBar form={form} save={save} defaults />
      </FormCard>
    </section>
  );
}
