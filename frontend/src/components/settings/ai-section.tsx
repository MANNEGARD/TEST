"use client";

import Link from "next/link";
import { ArrowRight, Sparkles } from "lucide-react";
import { formatPercent } from "@/lib/format";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { BoolRow, FormCard, GroupHeading, Note, SectionIntro, SettingRow, SettingsSaveBar, Slider } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

/** Gränsvärden enligt backend/app/services/settings.py (_BOUNDS). */
const AUTO_MIN = 0.5;
const STEP = 0.05;

const FIELDS: FieldSpecs = {
  "ai.auto_threshold": { type: "number", name: "Gränsen för automatisk klassning", min: AUTO_MIN, max: 1 },
  "ai.review_threshold": { type: "number", name: "Gränsen för förslag", min: 0, max: 1 },
  "ai.strict_mode": { type: "bool" },
  "ai.use_llm_for_purpose": { type: "bool" },
  "ai.send_calendar_subjects": { type: "bool" },
  "ai.send_calendar_body": { type: "bool" },
  "ai.send_street_addresses": { type: "bool" },
};

const PROVIDER_LABELS: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI" };

function Zone({ tone, title, range, children }: { tone: string; title: string; range: string; children: React.ReactNode }) {
  return (
    <li className="flex gap-2.5">
      <span className={cn("mt-1 size-2.5 shrink-0 rounded-full", tone)} aria-hidden />
      <p className="text-[13px] leading-relaxed">
        <span className="font-medium text-fg">{title}</span> <span className="font-mono text-xs text-fg-muted">({range})</span>
        <span className="text-fg-muted"> → {children}</span>
      </p>
    </li>
  );
}

/** 4. Förslag och AI – hur säker klassningen måste vara och vad som får lämna servern. */
export function AiSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS, (num) => {
    const auto = num("ai.auto_threshold");
    const review = num("ai.review_threshold");
    return auto != null && review != null && review > auto
      ? { "ai.review_threshold": "Gränsen för förslag får inte vara högre än gränsen för automatisk klassning." }
      : {};
  });
  const save = useSectionSave(form, "Inställningarna för förslag och AI är sparade.");
  useReportDirty("forslag", form.dirty);

  const auto = form.number("ai.auto_threshold") ?? 0.85;
  const review = form.number("ai.review_threshold") ?? 0.6;
  const strict = form.bool("ai.strict_mode");
  const provider = String(data.settings.ai?.provider ?? "none");
  const hasThresholds = form.has("ai.auto_threshold") && form.has("ai.review_threshold");

  // Reglagen håller ordningen själva: gränsen för förslag kan aldrig ligga över den automatiska.
  const setAuto = (v: number) => {
    form.set("ai.auto_threshold", String(v));
    if (review > v) form.set("ai.review_threshold", String(v));
  };
  const setReview = (v: number) => form.set("ai.review_threshold", String(Math.min(v, auto)));

  const pct = (v: number) => formatPercent(v);

  return (
    <section aria-labelledby="settings-forslag">
      <SectionIntro id="settings-forslag" title="Förslag och AI">
        Varje ny resa får ett förslag på Arbete eller Privat med en säkerhet i procent. Här bestämmer du hur säkert förslaget måste vara
        för att gälla utan att du behöver titta på det.
      </SectionIntro>

      <FormCard onSubmit={save.save} aria-label="Förslag och AI">
        {hasThresholds ? (
          <>
            <div className="px-5 py-4">
              <h3 className="text-[13px] font-medium text-fg">Så här hanteras ett förslag</h3>
              <div className="mt-3 flex h-2.5 overflow-hidden rounded-full bg-surface-3" aria-hidden>
                <span className="bg-undecided" style={{ width: `${review * 100}%` }} />
                <span className="border-x border-surface bg-accent" style={{ width: `${Math.max(0, auto - review) * 100}%` }} />
                <span className={strict ? "bg-accent" : "bg-ok"} style={{ width: `${Math.max(0, 1 - auto) * 100}%` }} />
              </div>
              <ul className="mt-3.5 space-y-2">
                <Zone tone={strict ? "bg-accent" : "bg-ok"} title="Hög säkerhet" range={`minst ${pct(auto)}`}>
                  {strict ? "blir ett förslag som du godkänner (strikt läge)." : "klassas automatiskt. Resan märks som preliminär tills du godkänt den."}
                </Zone>
                <Zone tone="bg-accent" title="Medelhög säkerhet" range={`${pct(review)}–${pct(auto)}`}>
                  blir ett förslag som du godkänner eller ändrar med ett klick.
                </Zone>
                <Zone tone="bg-undecided" title="Låg säkerhet" range={`under ${pct(review)}`}>
                  inget förslag. Resan hamnar i Granskning och du väljer själv.
                </Zone>
              </ul>
            </div>
            <SettingRow
              stacked
              htmlFor="ai-auto"
              label={
                <span className="flex items-baseline justify-between gap-3">
                  Klassa automatiskt från
                  <span className="font-mono text-sm">{pct(auto)}</span>
                </span>
              }
              hint="Högre värde ger färre automatiska klassningar och fler förslag att godkänna."
              error={form.error("ai.auto_threshold")}
            >
              <Slider id="ai-auto" min={AUTO_MIN} max={1} step={STEP} value={auto} onChange={setAuto} valueText={`${Math.round(auto * 100)} procent`} describedBy="ai-auto-hint" />
            </SettingRow>
            <SettingRow
              stacked
              htmlFor="ai-review"
              label={
                <span className="flex items-baseline justify-between gap-3">
                  Ge förslag från
                  <span className="font-mono text-sm">{pct(review)}</span>
                </span>
              }
              hint="Under den här gränsen lämnas resan utan förslag i stället för att gissa."
              error={form.error("ai.review_threshold")}
            >
              <Slider id="ai-review" min={0} max={1} step={STEP} value={review} onChange={setReview} valueText={`${Math.round(review * 100)} procent`} describedBy="ai-review-hint" />
            </SettingRow>
          </>
        ) : null}
        <BoolRow
          form={form}
          path="ai.strict_mode"
          id="ai-strict"
          label="Strikt läge"
          hint="Inget klassas automatiskt. Allt som regler eller AI föreslår väntar på ditt godkännande."
        />

        <GroupHeading description="Gäller bara när en AI-leverantör är vald. Utan AI bygger förslagen på regler och dina tidigare val – allt fungerar ändå.">
          Vad får skickas till AI-leverantören?
        </GroupHeading>
        <div className="px-5 py-4">
          <Note tone={provider === "none" ? "info" : "accent"} icon={<Sparkles />}>
            <p>
              {provider === "none" ? (
                <>
                  <span className="font-medium text-fg">Ingen AI-leverantör är vald.</span> Ingenting skickas utanför din server.
                </>
              ) : (
                <>
                  <span className="font-medium">Vald leverantör: {PROVIDER_LABELS[provider] ?? provider}.</span> Platsens namn och ort,
                  veckodag, klockslag, sträcka och dina tidigare syften skickas alltid. Koordinater, chassinummer, hemadress och privata
                  platsers namn skickas aldrig.
                </>
              )}
            </p>
            <Link
              href="/integrations"
              className="mt-1.5 inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
            >
              {provider === "none" ? "Välj leverantör under Integrationer" : "Hantera leverantören under Integrationer"}
              <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          </Note>
        </div>
        <BoolRow
          form={form}
          path="ai.use_llm_for_purpose"
          id="ai-purpose"
          label="Låt AI föreslå syfte"
          hint="För tjänsteresor formulerar AI ett förslag på syfte när reglerna inte räcker. Avstängt används bara dina tidigare syften."
        />
        <BoolRow
          form={form}
          path="ai.send_calendar_subjects"
          id="ai-cal-subject"
          label="Kalenderhändelsers rubrik"
          hint="Rubrik, tid och plats för möten som ligger nära resan – det som oftast avgör syftet."
        />
        <BoolRow
          form={form}
          path="ai.send_calendar_body"
          id="ai-cal-body"
          label="Kalenderhändelsers brödtext"
          hint="Början av mötets beskrivning. Kräver också att brödtexten sparas, se sektionen Kalender."
        />
        <BoolRow
          form={form}
          path="ai.send_street_addresses"
          id="ai-street"
          label="Gatuadresser"
          hint="Avstängt skickas bara platsens namn och ort, aldrig gata och nummer."
        />
        <SettingsSaveBar form={form} save={save} defaults />
      </FormCard>
    </section>
  );
}
