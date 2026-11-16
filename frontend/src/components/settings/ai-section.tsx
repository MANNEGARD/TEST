"use client";

import Link from "next/link";
import { ArrowRight, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { formatNumber, formatPercent } from "@/lib/format";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useReportDirty } from "./dirty-context";
import { BoolRow, FormCard, GroupHeading, Note, SectionIntro, SettingRow, SettingsSaveBar, Slider } from "./form-parts";
import { sectionHref } from "./sections";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

/** Gränsvärden enligt backend/app/services/settings.py (_BOUNDS). */
const AUTO_MIN = 0.5;
const STEP = 0.05;
/** ai.about_work: högst 1000 tecken (settings.py). */
const ABOUT_MAX = 1000;

type ContextLevel = "full" | "minimal";

/** Radiotexterna (byggkontraktet 6.E och tilläggets 3.J) – säger exakt vad som lämnar servern. */
const FULL_ITEMS = [
  "Namn och gatuadress för start och mål – även hemmet och privata platser – och kartans namn och typ för okända ställen.",
  "Dagens alla resor med tider, platser, kategorier och ärenden, resan före och efter, och historiken för samma mål och rutt.",
  "Dina regler och platsanteckningar, arbetsgivarens namn och det du skriver under Om mitt arbete.",
  "Kalenderbokningarna för dagen och, vid resor till en annan ort, hela vistelsen och de närmaste dagarna – med rubrik, plats, beskrivning och deltagarnas namn (kollega eller extern, och den externa e-postdomänen). Privata bokningar skickas bara som tid, utan text.",
];
const FULL_NEVER =
  "E-postadresser, koordinater, chassinummer, nycklar, lösenord och kalenderlänken skickas aldrig, och koder och lösenord i bokningar och anteckningar tas bort före sändningen.";
const MINIMAL_TEXT =
  "Bara platsens namn och ort, veckodag, klockslag, sträcka, regelmotorns signaler och dina tidigare ärenden. Hemmet och privata platser skickas som ”Hemmet” och ”Privat plats”. Kalenderrubrik, brödtext och gatuadresser styrs av valen nedan och skickas då som de står i kalendern, utan att koder eller länkar tas bort.";

/** Underlagsnivån: två val under varandra (det fullständiga har en lista och behöver bredden). */
function LevelChoice({ value, onChange, defaultLevel }: { value: ContextLevel; onChange: (v: ContextLevel) => void; defaultLevel: unknown }) {
  const options: { value: ContextLevel; title: string; description: React.ReactNode }[] = [
    {
      value: "full",
      title: "Fullständigt underlag (rekommenderas)",
      description: (
        <>
          Allt som hjälper bedömningen skickas:
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {FULL_ITEMS.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
          <span className="mt-1.5 block">{FULL_NEVER}</span>
        </>
      ),
    },
    { value: "minimal", title: "Minimalt", description: MINIMAL_TEXT },
  ];
  return (
    <fieldset className="px-5 py-4">
      <legend className="float-left w-full text-[13px] font-medium text-fg">Underlag till AI-leverantören</legend>
      <div className="clear-both grid gap-2.5 pt-3">
        {options.map((o) => {
          const checked = o.value === value;
          return (
            <label
              key={o.value}
              className={cn(
                "relative flex cursor-pointer gap-3 rounded-lg border p-3.5 transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ring",
                checked ? "border-accent bg-accent-soft/50" : "border-border hover:border-border-strong hover:bg-surface-2/50",
              )}
            >
              <input type="radio" name="ai-context-level" value={o.value} checked={checked} onChange={() => onChange(o.value)} className="sr-only" />
              <span
                aria-hidden
                className={cn(
                  "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border transition-colors",
                  checked ? "border-accent bg-accent" : "border-border-strong bg-surface",
                )}
              >
                {checked ? <span className="size-1.5 rounded-full bg-accent-fg" /> : null}
              </span>
              <span className="min-w-0">
                <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium text-fg">
                  {o.title}
                  {defaultLevel === o.value ? <Badge size="sm">Standard</Badge> : null}
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-fg-muted">{o.description}</span>
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

const FIELDS: FieldSpecs = {
  "ai.auto_threshold": { type: "number", name: "Gränsen för automatisk klassning", min: AUTO_MIN, max: 1 },
  "ai.review_threshold": { type: "number", name: "Gränsen för förslag", min: 0, max: 1 },
  "ai.strict_mode": { type: "bool" },
  "ai.context_level": { type: "choice", options: ["full", "minimal"] },
  "ai.about_work": { type: "text", name: "Om mitt arbete", maxLength: ABOUT_MAX },
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
        <span className="font-medium text-fg">{title}</span> <span className="num text-xs text-fg-muted">({range})</span>
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
  // Utan nyckeln (äldre server) gäller det tidigare, minimala underlaget.
  const level: ContextLevel = form.text("ai.context_level") === "full" ? "full" : "minimal";
  const full = form.has("ai.context_level") && level === "full";
  const aboutLength = form.text("ai.about_work").length;
  const employer = String(data.settings.general?.employer_name ?? "").trim();

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
                  <span className="num text-sm">{pct(auto)}</span>
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
                  <span className="num text-sm">{pct(review)}</span>
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
          Hur mycket underlag får AI?
        </GroupHeading>
        {form.has("ai.context_level") ? (
          <LevelChoice value={level} onChange={(v) => form.set("ai.context_level", v)} defaultLevel={form.defaultOf("ai.context_level")} />
        ) : null}
        <div className="px-5 py-4">
          <Note tone={provider === "none" ? "info" : "accent"} icon={<Sparkles />}>
            <p data-testid="ai-provider-note">
              {provider === "none" ? (
                <>
                  <span className="font-medium text-fg">Ingen AI-leverantör är vald.</span> Ingenting skickas utanför din server.
                </>
              ) : full ? (
                <>
                  <span className="font-medium">Vald leverantör: {PROVIDER_LABELS[provider] ?? provider}.</span> Med fullständigt underlag skickas allt som står
                  ovan. Koordinater, chassinummer, nycklar och kalenderlänken skickas aldrig.
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
        {form.has("ai.about_work") ? (
          <SettingRow
            stacked
            htmlFor="ai-about-work"
            label={
              <span className="flex items-baseline justify-between gap-3">
                Om mitt arbete
                <span className={aboutLength > ABOUT_MAX ? "num text-xs font-normal text-err-ink" : "num text-xs font-normal text-fg-subtle"}>
                  {formatNumber(aboutLength, 0)} / {formatNumber(ABOUT_MAX, 0)}
                </span>
              </span>
            }
            hint="Beskriv kort vad du gör i jobbet, till exempel vilka kunder du besöker och vilka ärenden som är vanliga. Skickas till AI-leverantören med fullständigt underlag."
            error={form.error("ai.about_work")}
          >
            <Textarea
              id="ai-about-work"
              rows={4}
              value={form.text("ai.about_work")}
              onChange={(e) => form.set("ai.about_work", e.target.value)}
              placeholder="Jag är servicetekniker på Exempel Åkeri AB och besöker kunder i hela länet …"
              aria-describedby="ai-about-work-hint ai-about-work-employer"
              aria-invalid={form.error("ai.about_work") ? true : undefined}
            />
            <p id="ai-about-work-employer" className="text-xs text-fg-muted">
              {employer ? (
                <>
                  Arbetsgivare: <span className="font-medium text-fg">{employer}</span> – ändras under{" "}
                </>
              ) : (
                <>Ingen arbetsgivare angiven. Den anges under </>
              )}
              <Link href={sectionHref("fordon")} className="font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring">
                Fordon och förare
              </Link>
              .
            </p>
          </SettingRow>
        ) : null}
        <BoolRow
          form={form}
          path="ai.use_llm_for_purpose"
          id="ai-purpose"
          label="Låt AI föreslå syfte"
          hint="För tjänsteresor formulerar AI ett färdigt ärende i samma stil som dina tidigare. Du godkänner det med ett tryck – AI godkänner aldrig något själv."
        />
        {!full ? (
          <>
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
              hint="Början av mötets beskrivning. Kräver också att hela bokningen läses, se sektionen Kalender."
            />
            <BoolRow
              form={form}
              path="ai.send_street_addresses"
              id="ai-street"
              label="Gatuadresser"
              hint="Avstängt skickas bara platsens namn och ort, aldrig gata och nummer."
            />
          </>
        ) : null}
        <SettingsSaveBar form={form} save={save} defaults />
      </FormCard>
    </section>
  );
}
