"use client";

import { formatNumber } from "@/lib/format";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { useReportDirty } from "./dirty-context";
import { Advanced, BoolRow, ChoiceCards, FormCard, NumberRow, SectionIntro, SettingsSaveBar } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

/**
 * Nycklarna rate_per_mil, show_amount, layout och journal_scope är nya i backend –
 * varje inställning visas bara om nyckeln finns i svaret från GET /settings.
 */
const FIELDS: FieldSpecs = {
  "export.rate_per_mil": { type: "number", name: "Ersättningen per mil", min: 0, max: 1000 },
  "export.show_amount": { type: "bool" },
  "export.show_via": { type: "bool" },
  "export.layout": { type: "choice", options: ["simple", "detailed"] },
  "general.journal_scope": { type: "choice", options: ["business_only", "complete"] },
  "export.include_private_addresses": { type: "bool" },
  "export.include_change_history": { type: "bool" },
};

/** 2. Milersättning och export. */
export function ReimbursementSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS);
  const save = useSectionSave(form, "Milersättning och export är sparade.");
  useReportDirty("ersattning", form.dirty);

  const defaultRate = form.defaultOf("export.rate_per_mil");
  const defaultLayout = form.defaultOf("export.layout");
  const defaultScope = form.defaultOf("general.journal_scope");
  const hasAdvanced = form.has("export.include_private_addresses") || form.has("export.include_change_history");

  return (
    <section aria-labelledby="settings-ersattning">
      <SectionIntro id="settings-ersattning" title="Milersättning och export">
        Hur tjänsteresorna räknas om till ersättning och hur underlaget till arbetsgivaren ser ut.
      </SectionIntro>

      <FormCard onSubmit={save.save} aria-label="Milersättning och export">
        <NumberRow
          form={form}
          path="export.rate_per_mil"
          id="exp-rate"
          label="Ersättning per mil"
          unit="kr/mil"
          decimal
          hint={
            typeof defaultRate === "number"
              ? `Beloppet du får per körd mil i tjänsten. Standardvärdet ${formatNumber(defaultRate, 2, 0)} kr är Skatteverkets skattefria schablon för egen bil.`
              : "Beloppet du får per körd mil i tjänsten."
          }
        />
        <BoolRow
          form={form}
          path="export.show_amount"
          id="exp-amount"
          label="Visa belopp i kronor"
          hint="Räknar ut ersättningen (mil × ersättning per mil) på översikten och i exporten. Stäng av om du bara vill redovisa antal mil."
        />
        {form.has("export.show_via") ? (
          <BoolRow
            form={form}
            path="export.show_via"
            id="exp-via"
            label="Visa mellanstopp (via) i underlaget"
            hint="En resa som slagits samman av flera delsträckor visar stoppen på vägen som ”via …” under målet. Stäng av om underlaget bara ska visa start och mål."
          />
        ) : null}
        {form.has("export.layout") ? (
          <ChoiceCards
            legend="Exportens utseende"
            name="export-layout"
            value={form.text("export.layout")}
            onChange={(v) => form.set("export.layout", v)}
            options={[
              {
                value: "simple",
                title: "Enkel",
                isDefault: defaultLayout === "simple",
                description: "Ett blad med periodens tjänsteresor: datum, tid, från, till, ärende, mätarställning och km. Det arbetsgivaren behöver för milersättningen.",
              },
              {
                value: "detailed",
                title: "Detaljerad",
                isDefault: defaultLayout === "detailed",
                description: "Hela arbetsboken med sex blad – översikt, körjournal, månadssammanställning, bränsle, datakvalitet och ändringshistorik.",
              },
            ]}
          />
        ) : null}
        {form.has("general.journal_scope") ? (
          <ChoiceCards
            legend="Vad redovisas?"
            name="journal-scope"
            value={form.text("general.journal_scope")}
            onChange={(v) => form.set("general.journal_scope", v)}
            options={[
              {
                value: "business_only",
                title: "Endast tjänsteresor",
                isDefault: defaultScope === "business_only",
                description: "Egen bil med milersättning. Luckor mellan resorna är privat körning och behöver inte redovisas.",
              },
              {
                value: "complete",
                title: "Fullständig körjournal",
                isDefault: defaultScope === "complete",
                description: "Alla resor redovisas och stäms av mot mätarställningen, till exempel för förmånsbil.",
              },
            ]}
          />
        ) : null}
        {hasAdvanced ? (
          <Advanced summary="detaljerad export">
            <BoolRow
              form={form}
              path="export.include_private_addresses"
              id="exp-private"
              label="Ta med adresser för privata resor"
              hint="Avstängt visas privata resor bara som ”Privat resa” med ort. Gäller den detaljerade exporten och topplistorna på översikten."
            />
            <BoolRow
              form={form}
              path="export.include_change_history"
              id="exp-history"
              label="Ta med ändringshistorik"
              hint="Bladet ”Källa och ändringshistorik” i den detaljerade exporten visar vilka ändringar som gjorts och av vem."
            />
          </Advanced>
        ) : null}
        <SettingsSaveBar form={form} save={save} />
      </FormCard>
    </section>
  );
}
