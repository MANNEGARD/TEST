"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Input, Label } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { Advanced, BoolRow, FormCard, GroupHeading, NumberRow, SectionIntro, SettingRow, SettingsSaveBar } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs, type SettingsForm } from "./use-settings-form";

/** Gränsvärden enligt backend/app/services/settings.py (_BOUNDS, _validate_time). */
const FIELDS: FieldSpecs = {
  "notifications.enabled": { type: "bool" },
  "notifications.kinds": { type: "json" },
  "notifications.quiet_start": { type: "time", name: "Tysta timmar från" },
  "notifications.quiet_end": { type: "time", name: "Tysta timmar till" },
  "notifications.max_per_day": { type: "number", name: "Max antal notiser per dygn", min: 0, max: 100, integer: true },
  "notifications.reminder_after_hours": { type: "number", name: "Påminnelse efter", min: 1, max: 24 * 14, integer: true },
  "notifications.max_reminders": { type: "number", name: "Antal påminnelser", min: 0, max: 10, integer: true },
  "notifications.weekly_day": { type: "number", name: "Veckodag", min: 0, max: 6, integer: true },
  "notifications.weekly_time": { type: "time", name: "Klockslag för veckosammanställningen" },
  "notifications.daily_time": { type: "time", name: "Klockslag för dagssammanställningen" },
  "notifications.hide_locations_on_lock_screen": { type: "bool" },
  "notifications.batch_window_minutes": { type: "number", name: "Samla notiser inom", min: 0, max: 240, integer: true },
  "notifications.expire_after_hours": { type: "number", name: "Frågor upphör efter", min: 1, max: 24 * 60, integer: true },
};

type Kinds = Record<string, boolean>;

const KIND_OPTIONS: { key: string; label: string; hint: string }[] = [
  { key: "classify", label: "Arbete eller privat?", hint: "När en ny resa behöver ditt beslut." },
  { key: "purpose", label: "Syfte saknas", hint: "När en tjänsteresa saknar syfte." },
  { key: "merge", label: "Slå samman resor?", hint: "När ett stopp ligger mellan gränserna för sammanslagning." },
];

const WEEKDAYS = ["Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag"];

function TimeInput({ form, path, id, label }: { form: SettingsForm; path: string; id: string; label: string }) {
  return (
    <Input
      id={id}
      type="time"
      aria-label={label}
      value={form.text(path)}
      onChange={(e) => form.set(path, e.target.value)}
      aria-invalid={form.error(path) ? true : undefined}
      className="w-[7.5rem] shrink-0 font-mono"
    />
  );
}

/** 5. Notiser – frågor till mobilen via Home Assistant. */
export function NotificationsSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS);
  const save = useSectionSave(form, "Inställningarna för notiser är sparade.");
  useReportDirty("notiser", form.dirty);

  const enabled = form.bool("notifications.enabled");
  const kinds = form.has("notifications.kinds") ? form.json<Kinds>("notifications.kinds") : {};
  const setKind = (key: string, value: boolean) => form.set("notifications.kinds", { ...kinds, [key]: value });
  const weeklyOn = kinds.weekly !== false;
  const dailyOn = kinds.daily === true;
  const quietError = form.error("notifications.quiet_start") ?? form.error("notifications.quiet_end");
  const weeklyError = form.error("notifications.weekly_day") ?? form.error("notifications.weekly_time");

  return (
    <section aria-labelledby="settings-notiser">
      <SectionIntro id="settings-notiser" title="Notiser">
        Korta frågor till mobilen när en resa behöver ditt svar, så att körjournalen blir klar medan du minns resan. Kräver Home Assistant –{" "}
        <Link
          href="/integrations"
          className="inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
        >
          koppla under Integrationer
          <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </SectionIntro>

      <FormCard onSubmit={save.save} aria-label="Notiser">
        <BoolRow
          form={form}
          path="notifications.enabled"
          id="ntf-enabled"
          label="Skicka notiser"
          hint="Avstängt hamnar allt i Granskning i stället, där du tar det när det passar."
        />
        <div className={cn("divide-y divide-border", !enabled && "opacity-60")}>
          {form.has("notifications.kinds") ? (
            <fieldset className="px-5 py-4">
              <legend className="float-left w-full text-[13px] font-medium text-fg">Fråga mig om</legend>
              <div className="clear-both space-y-3 pt-3">
                {KIND_OPTIONS.map((k) => (
                  <div key={k.key} className="flex items-start gap-3">
                    <Checkbox
                      id={`ntf-kind-${k.key}`}
                      checked={kinds[k.key] !== false}
                      onCheckedChange={(v) => setKind(k.key, v === true)}
                      disabled={!enabled}
                      aria-describedby={`ntf-kind-${k.key}-hint`}
                      className="mt-0.5"
                    />
                    <div className="min-w-0">
                      <Label htmlFor={`ntf-kind-${k.key}`} className="block cursor-pointer">
                        {k.label}
                      </Label>
                      <p id={`ntf-kind-${k.key}-hint`} className="text-xs leading-relaxed text-fg-muted">
                        {k.hint}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </fieldset>
          ) : null}

          {form.has("notifications.quiet_start") && form.has("notifications.quiet_end") ? (
            <SettingRow
              label="Tysta timmar"
              hint="Inga notiser mellan de här klockslagen. Frågor som uppstår då skickas när de tysta timmarna är slut."
              error={quietError}
            >
              <div className="flex items-center gap-2">
                <TimeInput form={form} path="notifications.quiet_start" id="ntf-quiet-start" label="Tysta timmar från" />
                <span className="text-sm text-fg-muted" aria-hidden>
                  –
                </span>
                <TimeInput form={form} path="notifications.quiet_end" id="ntf-quiet-end" label="Tysta timmar till" />
              </div>
            </SettingRow>
          ) : null}

          <NumberRow
            form={form}
            path="notifications.max_per_day"
            id="ntf-max"
            label="Högst antal notiser per dygn"
            unit="st"
            hint="Resten väntar i Granskning till nästa dag. 0 betyder inga notiser alls."
          />

          <GroupHeading>Påminnelser och sammanställningar</GroupHeading>
          <NumberRow
            form={form}
            path="notifications.reminder_after_hours"
            id="ntf-reminder"
            label="Påminn efter"
            unit="timmar"
            hint="Om en fråga inte är besvarad efter så här lång tid kommer en påminnelse."
          />
          <NumberRow
            form={form}
            path="notifications.max_reminders"
            id="ntf-reminders"
            label="Antal påminnelser"
            unit="st"
            hint="Hur många gånger samma fråga påminns om. 0 stänger av påminnelser."
          />
          {form.has("notifications.weekly_day") ? (
            <div>
              <div className="flex items-start justify-between gap-4 px-5 pt-4 sm:gap-8">
                <div className="min-w-0 sm:max-w-md">
                  <Label htmlFor="ntf-weekly" className="block cursor-pointer">
                    Veckosammanställning
                  </Label>
                  <p id="ntf-weekly-hint" className="mt-1 text-xs leading-relaxed text-fg-muted">
                    En notis i veckan med veckans tjänstemil och det som återstår att granska.
                  </p>
                </div>
                <Switch
                  id="ntf-weekly"
                  checked={weeklyOn}
                  onCheckedChange={(v) => setKind("weekly", v)}
                  disabled={!enabled}
                  aria-describedby="ntf-weekly-hint"
                  className="mt-0.5"
                />
              </div>
              <div className={cn("flex flex-wrap items-center gap-2 px-5 pt-3 pb-4", !weeklyOn && "opacity-60")}>
                <Select
                  value={form.text("notifications.weekly_day")}
                  onValueChange={(v) => form.set("notifications.weekly_day", v)}
                  disabled={!weeklyOn}
                >
                  <SelectTrigger className="w-40" aria-label="Veckodag för veckosammanställningen">
                    <SelectValue placeholder="Välj dag" />
                  </SelectTrigger>
                  <SelectContent>
                    {WEEKDAYS.map((d, i) => (
                      <SelectItem key={d} value={String(i)}>
                        {d}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-sm text-fg-muted">kl.</span>
                <TimeInput form={form} path="notifications.weekly_time" id="ntf-weekly-time" label="Klockslag för veckosammanställningen" />
                {weeklyError ? <p className="basis-full text-xs font-medium text-err-ink">{weeklyError}</p> : null}
              </div>
            </div>
          ) : null}

          <BoolRow
            form={form}
            path="notifications.hide_locations_on_lock_screen"
            id="ntf-lock"
            label="Dölj platser på låsskärmen"
            hint="Notisen visar bara tid och sträcka tills telefonen är upplåst – bra om andra kan se din skärm."
          />
        </div>

        <Advanced
          summary="dagssammanställning, samling och giltighetstid"
          hasError={
            !!form.error("notifications.daily_time") ||
            !!form.error("notifications.batch_window_minutes") ||
            !!form.error("notifications.expire_after_hours")
          }
        >
          {form.has("notifications.daily_time") ? (
            <SettingRow
              label="Dagssammanställning"
              hint="En notis varje kväll med dagens resor. Av som standard – veckosammanställningen räcker för de flesta."
              error={form.error("notifications.daily_time")}
            >
              <div className="flex items-center gap-3">
                <Checkbox
                  id="ntf-daily"
                  checked={dailyOn}
                  onCheckedChange={(v) => setKind("daily", v === true)}
                  disabled={!enabled}
                  aria-label="Skicka dagssammanställning"
                />
                <TimeInput form={form} path="notifications.daily_time" id="ntf-daily-time" label="Klockslag för dagssammanställningen" />
              </div>
            </SettingRow>
          ) : null}
          <NumberRow
            form={form}
            path="notifications.batch_window_minutes"
            id="ntf-batch"
            label="Samla notiser inom"
            unit="min"
            hint="Flera frågor inom den här tiden skickas som en notis i stället för många."
          />
          <NumberRow
            form={form}
            path="notifications.expire_after_hours"
            id="ntf-expire"
            label="Frågor upphör efter"
            unit="timmar"
            hint="Obesvarade frågor tas bort från mobilen efter så här lång tid. Resan finns kvar i Granskning."
          />
        </Advanced>
        <SettingsSaveBar form={form} save={save} defaults />
      </FormCard>
    </section>
  );
}
