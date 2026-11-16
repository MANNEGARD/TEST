"use client";

import { formatNumber } from "@/lib/format";
import type { SettingsChanges, SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { Advanced, BoolRow, FormCard, GroupHeading, Note, NumberRow, SaveBar, SectionIntro } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs, type SettingsForm } from "./use-settings-form";

/** Gränsvärden enligt backend/app/services/settings.py (_BOUNDS). */
const FIELDS: FieldSpecs = {
  "merge.enabled": { type: "bool" },
  "merge.auto_max_minutes": { type: "number", name: "Gränsen för automatisk sammanslagning (tjänsteresor)", min: 0, max: 240 },
  "merge.ask_max_minutes": { type: "number", name: "Gränsen för fråga (tjänsteresor)", min: 0, max: 1440 },
  "merge.private_auto_max_minutes": { type: "number", name: "Gränsen för automatisk sammanslagning (privata resor)", min: 0, max: 240 },
  "merge.private_ask_max_minutes": { type: "number", name: "Gränsen för fråga (privata resor)", min: 0, max: 1440 },
  "merge.odometer_tolerance_km": { type: "number", name: "Toleransen i mätarställning", min: 0, max: 20 },
};

const ASK_ERROR = "Gränsen för fråga måste vara minst lika stor som gränsen för automatisk sammanslagning.";
const PRIVATE_ASK_ERROR = "Gränsen för fråga för privata resor måste vara minst lika stor som gränsen för automatisk sammanslagning.";

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

/** Regelremsan A (automatiskt), B (fråga) och C (separata) för en uppsättning gränser. */
function RuleStrip({ auto, ask, label }: { auto: number | null; ask: number | null; label: string }) {
  const noQuestion = auto != null && ask != null && ask <= auto;
  return (
    <div className="px-5 py-4">
      <ol className="grid gap-2.5 sm:grid-cols-3" aria-label={label}>
        <Rule letter="A" title="Slås samman" when={`Stopp upp till ${minutes(auto)}`} tone="bg-ok-soft text-ok-ink">
          Resorna blir en resa automatiskt.
        </Rule>
        {noQuestion ? (
          <Rule letter="B" title="Ingen fråga" when="Inga frågor" tone="bg-surface-3 text-fg-muted">
            Frågegränsen är densamma som gränsen för automatisk sammanslagning.
          </Rule>
        ) : (
          <Rule letter="B" title="Du får en fråga" when={`${minutes(auto)} – ${minutes(ask)}`} tone="bg-accent-soft text-accent-ink">
            Du väljer själv: samma resa eller två.
          </Rule>
        )}
        <Rule letter="C" title="Förblir två resor" when={`Längre än ${minutes(noQuestion ? auto : ask)}`} tone="bg-surface-3 text-fg-muted">
          Slås bara samman om du gör det själv.
        </Rule>
      </ol>
    </div>
  );
}

function withCategoryRules(changes: SettingsChanges): SettingsChanges {
  return { ...changes, merge: { ...(changes.merge ?? {}), by_category: true } };
}

/**
 * 3. Sammanslagning av resor – olika gränser för tjänsteresor och privata resor.
 *
 * `merge.by_category` (backend/app/services/trips/merge.py) avgör regelverket. En äldre
 * installation har `false` (det tidigare sättet) tills sektionen sparas; sparningen skickar
 * därför alltid `by_category: true`, och sparraden är aktiv även utan andra ändringar.
 */
export function MergeSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS, (num) => {
    const errors: Record<string, string> = {};
    const auto = num("merge.auto_max_minutes");
    const ask = num("merge.ask_max_minutes");
    if (auto != null && ask != null && ask < auto) errors["merge.ask_max_minutes"] = ASK_ERROR;
    const pauto = num("merge.private_auto_max_minutes");
    const pask = num("merge.private_ask_max_minutes");
    if (pauto != null && pask != null && pask < pauto) errors["merge.private_ask_max_minutes"] = PRIVATE_ASK_ERROR;
    return errors;
  });
  const merge = data.settings.merge ?? {};
  // Saknas nyckeln (äldre backend) gäller inget nytt regelverk – visa inga nya texter då.
  const supportsCategories = "by_category" in merge;
  const legacy = merge.by_category === false;
  const selfDriving = data.settings.automation?.mode === "self_driving";

  // Formuläret som sparas: alltid med by_category: true, och "ändrat" så länge det tidigare sättet gäller.
  const effective: SettingsForm = supportsCategories
    ? {
        ...form,
        dirty: form.dirty || legacy,
        changes: withCategoryRules(form.changes),
        previous: { ...form.previous, merge: { ...(form.previous.merge ?? {}), by_category: merge.by_category } },
      }
    : form;
  const save = useSectionSave(effective, "Reglerna för sammanslagning är sparade.");
  useReportDirty("sammanslagning", form.dirty);

  const enabled = !form.has("merge.enabled") || form.bool("merge.enabled");
  const auto = form.number("merge.auto_max_minutes");
  const ask = form.number("merge.ask_max_minutes");
  const pauto = form.number("merge.private_auto_max_minutes");
  const pask = form.number("merge.private_ask_max_minutes");
  const std = (path: string, unit: string) => {
    const v = form.defaultOf(path);
    return typeof v === "number" ? ` Standard: ${formatNumber(v, 1, 0)} ${unit}.` : "";
  };
  const savedAuto = typeof merge.auto_max_minutes === "number" ? formatNumber(merge.auto_max_minutes, 1, 0) : "…";
  const savedAsk = typeof merge.ask_max_minutes === "number" ? formatNumber(merge.ask_max_minutes, 1, 0) : "…";
  const hasPrivate = form.has("merge.private_auto_max_minutes");

  return (
    <section aria-labelledby="settings-sammanslagning">
      <SectionIntro id="settings-sammanslagning" title="Sammanslagning av resor">
        {supportsCategories
          ? "Bilen delar upp en resa varje gång du stannar. Här bestämmer du hur korta stopp som ska räknas som en och samma resa – olika för tjänsteresor och privata resor."
          : "Bilen delar upp en resa varje gång du stannar. Här bestämmer du hur korta stopp som ska räknas som en och samma resa – till exempel när du tankar eller hämtar något på vägen."}
      </SectionIntro>

      {legacy ? (
        <div className="mb-4" data-testid="merge-legacy">
          <Note>
          {selfDriving
            ? `Just nu gäller det tidigare sättet: bara stopp på högst ${savedAuto} min vid bekräftade tank- och laddstationer slås ihop, och inga frågor ställs. När du sparar gäller gränserna nedan för nya resor från och med nu. Resor som redan finns slås inte ihop i efterhand.`
            : `Just nu gäller samma gränser för alla resor, oavsett tjänst eller privat: automatiskt upp till ${savedAuto} min och fråga upp till ${savedAsk} min. När du sparar gäller gränserna nedan, uppdelade på tjänst och privat, för nya resor från och med nu.`}
          </Note>
        </div>
      ) : null}

      <FormCard onSubmit={save.save} aria-label="Sammanslagning av resor">
        <BoolRow
          form={form}
          path="merge.enabled"
          id="merge-enabled"
          label="Slå samman resor med korta stopp"
          hint="Avstängt lämnas alla resor som de är. Du kan fortfarande slå samman resor själv i körjournalen."
        />
        <div className={cn("divide-y divide-border", !enabled && "opacity-60")}>
          {hasPrivate ? <GroupHeading>Tjänsteresor</GroupHeading> : null}
          <NumberRow
            form={form}
            path="merge.auto_max_minutes"
            id="merge-auto"
            label="Slå samman automatiskt vid stopp på högst"
            unit="min"
            decimal
            hint={`${hasPrivate ? "Två tjänsteresor med kortare stopp än så" : "Kortare stopp än så"} räknas som samma resa utan att du behöver göra något.${std("merge.auto_max_minutes", "min")}`}
          />
          <NumberRow
            form={form}
            path="merge.ask_max_minutes"
            id="merge-ask"
            label="Fråga mig vid stopp på högst"
            unit="min"
            decimal
            hint={`Stopp mellan de två gränserna ger en fråga i Granskning: samma resa eller två?${std("merge.ask_max_minutes", "min")}`}
          />
          <RuleStrip auto={auto} ask={ask} label={hasPrivate ? "Tjänsteresor" : "Så här behandlas ett stopp"} />

          {hasPrivate ? (
            <>
              <GroupHeading>Privata resor</GroupHeading>
              <NumberRow
                form={form}
                path="merge.private_auto_max_minutes"
                id="merge-private-auto"
                label="Slå samman automatiskt vid stopp på högst"
                unit="min"
                decimal
                hint={`Två privata resor med kortare stopp än så räknas som samma resa.${std("merge.private_auto_max_minutes", "min")}`}
              />
              <NumberRow
                form={form}
                path="merge.private_ask_max_minutes"
                id="merge-private-ask"
                label="Fråga mig vid stopp på högst"
                unit="min"
                decimal
                hint={`Samma värde som gränsen ovan betyder att du aldrig får en fråga om privata resor.${std("merge.private_ask_max_minutes", "min")}`}
              />
              <RuleStrip auto={pauto} ask={pask} label="Privata resor" />
            </>
          ) : null}

          <div className="px-5 py-4">
            <h3 className="text-[13px] font-medium text-fg">Så här avgörs ett stopp</h3>
            {supportsCategories ? (
              <ul className="mt-2 list-disc space-y-1.5 pl-5 text-xs leading-relaxed text-fg-muted marker:text-fg-subtle" data-testid="merge-how">
                <li>Två tjänsteresor i rad: tjänstegränserna gäller.</li>
                <li>Två privata resor i rad: privatgränserna gäller.</li>
                <li>En tjänsteresa och en privat, eller en resa som inte är avgjord än: slås aldrig ihop automatiskt och ingen fråga ställs.</li>
                <li>Stopp hemma eller vid en privat plats slås aldrig ihop.</li>
                <li>Resorna slås ihop när båda är avgjorda – av dig, av en regel eller av importen. Det gäller även i självgående läge.</li>
                <li>Frågor hamnar i Granskning, och i telefonen om notiser om sammanslagning är på (bara för resor från de senaste två dygnen).</li>
                <li>Antagna kundbesök som ännu inte är bekräftade slås inte ihop.</li>
                <li>En sammanslagning går alltid att ångra, och privat körning blir aldrig tjänsteresa genom att resor slås samman.</li>
              </ul>
            ) : (
              <p className="mt-2 text-xs leading-relaxed text-fg-muted">
                En sammanslagning går alltid att ångra, och privat körning görs aldrig om till tjänsteresa genom att resor slås samman.
              </p>
            )}
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
            hint={`Så mycket får mätarställningen skilja mellan två resor som hör ihop. Bilen avrundar till hela kilometer, så en skillnad på 1 km räknas alltid som avrundning och ger aldrig något ärende.${std("merge.odometer_tolerance_km", "km")}`}
          />
        </Advanced>
        <SaveBar
          dirty={effective.dirty}
          invalid={form.firstError}
          error={save.error}
          saving={save.saving}
          onReset={form.reset}
          onDefaults={form.differsFromDefaults ? form.applyDefaults : undefined}
          submitLabel={legacy && !form.dirty ? "Spara och använd gränserna" : "Spara"}
        />
      </FormCard>
    </section>
  );
}
