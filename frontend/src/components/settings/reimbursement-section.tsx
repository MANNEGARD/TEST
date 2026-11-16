"use client";

import * as React from "react";
import { ImagePlus, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmInline } from "@/components/ui/confirm-inline";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDate, formatNumber } from "@/lib/format";
import { useExportLogo, useRemoveExportLogo, useUploadExportLogo } from "@/lib/queries/export-logo";
import { EXPORT_LOGO_MAX_BYTES, type ExportLogo, type SettingsPayload } from "@/lib/settings-page-types";
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
  "export.show_logo": { type: "bool" },
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
        {form.has("export.show_logo") ? (
          <BoolRow
            form={form}
            path="export.show_logo"
            id="exp-logo"
            label="Visa loggan i underlaget"
            hint="Företagsloggan nedan läggs uppe till höger i underlagets sidhuvud – i underlaget du skapar och i månadens automatiska underlag. Utan uppladdad logga händer ingenting."
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

      <LogoCard showLogo={form.has("export.show_logo") ? form.bool("export.show_logo") : true} />
    </section>
  );
}

// ----------------------------------------------------------------------------- företagslogga

const LOGO_TYPES = ["image/png", "image/jpeg"];

function sizeText(bytes: number | null): string | null {
  if (bytes == null) return null;
  return bytes < 1024 * 100 ? `${formatNumber(bytes / 1024, 0)} kB` : `${formatNumber(bytes / (1024 * 1024), 1, 1)} MB`;
}

function logoFacts(logo: ExportLogo): string {
  return [
    logo.content_type === "image/jpeg" ? "JPEG" : logo.content_type === "image/png" ? "PNG" : null,
    logo.width && logo.height ? `${logo.width} × ${logo.height} px` : null,
    sizeText(logo.size_bytes),
    logo.uploaded_at ? `uppladdad ${formatDate(logo.uploaded_at)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * Företagslogga i underlaget (1.3.0). Uppladdning och borttagning gäller direkt (inte via Spara).
 * Webbläsaren kontrollerar bara filtyp och storlek för ett snabbt besked – servern kontrollerar
 * filens innehåll och mått.
 */
function LogoCard({ showLogo }: { showLogo: boolean }) {
  const logo = useExportLogo();
  const upload = useUploadExportLogo();
  const remove = useRemoveExportLogo();
  const input = React.useRef<HTMLInputElement>(null);
  const [problem, setProblem] = React.useState<string | null>(null);
  const info = logo.data;

  const pick = () => {
    setProblem(null);
    input.current?.click();
  };

  const onFile = (file: File | undefined) => {
    if (input.current) input.current.value = "";
    if (!file) return;
    if (!LOGO_TYPES.includes(file.type)) {
      setProblem("Välj en bild i formatet PNG eller JPEG.");
      return;
    }
    if (file.size > EXPORT_LOGO_MAX_BYTES) {
      setProblem("Bilden är för stor. Välj en bild på högst 1 MB.");
      return;
    }
    upload.mutate(file, {
      onSuccess: () => {
        setProblem(null);
        toast.success(info?.has_logo ? "Loggan är bytt." : "Loggan är uppladdad.");
      },
      onError: (err) => setProblem(errorMessage(err)),
    });
  };

  return (
    <Card className="mt-6" aria-labelledby="exp-logo-title" role="region" data-testid="export-logo">
      <div className="px-5 py-4">
        <h3 id="exp-logo-title" className="text-[13px] font-medium text-fg">
          Företagslogga i underlaget
        </h3>
        <p className="mt-1 max-w-xl text-xs leading-relaxed text-fg-muted">
          Valfritt. Loggan läggs diskret uppe till höger i underlagets sidhuvud och skalas så att tabellen och utskriften inte påverkas. PNG eller JPEG, högst 1 MB.
        </p>

        <input
          ref={input}
          type="file"
          accept="image/png,image/jpeg"
          className="sr-only"
          tabIndex={-1}
          aria-hidden
          data-testid="export-logo-input"
          onChange={(e) => onFile(e.target.files?.[0])}
        />

        <div className="mt-4">
          {logo.isError && !info ? (
            <ErrorState error={logo.error} onRetry={() => logo.refetch()} title="Det gick inte att hämta loggan" />
          ) : !info ? (
            <div role="status" aria-label="Laddar loggan" className="flex items-center gap-4">
              <Skeleton className="h-16 w-40" />
              <Skeleton className="h-9 w-32" />
              <span className="sr-only">Laddar…</span>
            </div>
          ) : info.has_logo && info.url ? (
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
              {/* Vit bakgrund som i Excel, även i mörkt tema – så syns loggan som den blir i filen. */}
              <div className="flex h-20 w-full max-w-60 shrink-0 items-center justify-center rounded-md border border-border bg-white p-2">
                {/* eslint-disable-next-line @next/next/no-img-element -- bilden kommer från servern (eller demots minne), inte från next/image */}
                <img src={info.url} alt="Uppladdad företagslogga" className="max-h-full max-w-full object-contain" data-testid="export-logo-preview" />
              </div>
              <div className="min-w-0 flex-1 space-y-2">
                <p className="text-xs text-fg-muted num">{logoFacts(info)}</p>
                {!showLogo ? (
                  <p className="text-xs font-medium text-warn-ink" data-testid="export-logo-off">
                    Loggan visas inte i underlaget just nu – slå på ”Visa loggan i underlaget” ovan.
                  </p>
                ) : null}
                <div className="flex flex-wrap items-start gap-2">
                  <Button size="sm" variant="secondary" onClick={pick} loading={upload.isPending} disabled={remove.isPending}>
                    <Upload aria-hidden /> Byt logga
                  </Button>
                  <ConfirmInline
                    label="Ta bort"
                    confirmLabel="Ja, ta bort loggan"
                    icon={<Trash2 aria-hidden />}
                    ariaLabel="Ta bort företagsloggan"
                    description="Loggan tas bort från servern. Underlag du skapar efter det får ingen logga; filer som redan är skapade ändras inte."
                    loading={remove.isPending}
                    onConfirm={() =>
                      remove.mutate(undefined, {
                        onSuccess: () => {
                          setProblem(null);
                          toast.success("Loggan är borttagen.");
                        },
                        onError: (err) => toast.error(errorMessage(err)),
                      })
                    }
                  />
                </div>
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-start gap-3 rounded-md border border-dashed border-border-strong px-4 py-4 sm:flex-row sm:items-center">
              <ImagePlus className="size-5 shrink-0 text-fg-subtle" aria-hidden />
              <p className="flex-1 text-xs leading-relaxed text-fg-muted">Ingen logga uppladdad. Underlaget skapas som vanligt utan logga.</p>
              <Button size="sm" variant="secondary" onClick={pick} loading={upload.isPending} data-testid="export-logo-upload">
                <Upload aria-hidden /> Ladda upp logga
              </Button>
            </div>
          )}
        </div>

        {problem ? (
          <p role="alert" className="mt-3 text-xs font-medium text-err-ink" data-testid="export-logo-error">
            {problem}
          </p>
        ) : null}
      </div>
    </Card>
  );
}
