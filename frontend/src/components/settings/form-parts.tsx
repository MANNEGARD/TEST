"use client";

/**
 * Byggstenar för inställningssidan: sektionsrubrik, rader (etikett + förklaring + kontroll),
 * valkort, reglage, hopfälld "Avancerat"-del och sparraden längst ner i varje formulär.
 */

import * as React from "react";
import { ChevronRight, Info, RotateCcw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import type { SettingsForm } from "./use-settings-form";

// ----------------------------------------------------------------------------- sektion

/** Rubrik och ingress för en sektion (h2 – sidans h1 är "Inställningar"). */
export function SectionIntro({ id, title, badge, children }: { id: string; title: string; badge?: string; children?: React.ReactNode }) {
  return (
    <header className="mb-5">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <h2 id={id} className="text-lg font-semibold tracking-tight text-fg">
          {title}
        </h2>
        {badge ? <Badge size="sm">{badge}</Badge> : null}
      </div>
      {children ? <p className="mt-1 max-w-2xl text-sm leading-relaxed text-fg-muted">{children}</p> : null}
    </header>
  );
}

/** Kort med rader avdelade av tunna linjer. Med `onSubmit` blir kortet ett formulär (Enter sparar). */
export function FormCard({
  onSubmit,
  children,
  className,
  ...props
}: {
  onSubmit?: () => void;
  children: React.ReactNode;
  className?: string;
  "aria-label"?: string;
}) {
  if (!onSubmit) {
    return (
      <Card className={cn("divide-y divide-border", className)} {...props}>
        {children}
      </Card>
    );
  }
  return (
    <Card className={className}>
      <form
        noValidate
        className="divide-y divide-border"
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit();
        }}
        {...props}
      >
        {children}
      </form>
    </Card>
  );
}

/** Underrubrik inuti ett kort. */
export function GroupHeading({ children, description }: { children: React.ReactNode; description?: React.ReactNode }) {
  return (
    <div className="bg-surface-2/40 px-5 py-2.5">
      <h3 className="text-[13px] font-semibold text-fg">{children}</h3>
      {description ? <p className="mt-0.5 text-xs leading-relaxed text-fg-muted">{description}</p> : null}
    </div>
  );
}

// ----------------------------------------------------------------------------- rader

/**
 * En inställning: etikett och förklaring till vänster, kontrollen till höger
 * (under etiketten på smala skärmar). `stacked` lägger alltid kontrollen under.
 */
export function SettingRow({
  label,
  hint,
  htmlFor,
  error,
  children,
  stacked,
  className,
}: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  htmlFor?: string;
  error?: string | null;
  children: React.ReactNode;
  stacked?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("px-5 py-4", stacked ? "space-y-3" : "flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-8", className)}>
      <div className={cn("min-w-0", !stacked && "sm:max-w-md")}>
        <Label htmlFor={htmlFor} className="block">
          {label}
        </Label>
        {hint ? (
          <FieldHint id={htmlFor ? `${htmlFor}-hint` : undefined} className="mt-1 leading-relaxed">
            {hint}
          </FieldHint>
        ) : null}
        <FieldError className="mt-1.5">{error}</FieldError>
      </div>
      <div className={cn(!stacked && "shrink-0")}>{children}</div>
    </div>
  );
}

/** Fält med etiketten ovanför (för formulär med flera textfält i rutnät). */
export function Field({
  id,
  label,
  hint,
  error,
  children,
  className,
}: {
  id: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  error?: string | null;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0 space-y-1.5", className)}>
      <Label htmlFor={id} className="block">
        {label}
      </Label>
      {children}
      {error ? <FieldError>{error}</FieldError> : hint ? <FieldHint id={`${id}-hint`}>{hint}</FieldHint> : null}
    </div>
  );
}

/** Av/på-inställning. Hela etiketten går att klicka på. */
export function ToggleRow({
  id,
  label,
  hint,
  checked,
  onChange,
  disabled,
  className,
}: {
  id: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-4 px-5 py-4 sm:gap-8", className)}>
      <div className="min-w-0 sm:max-w-md">
        <Label htmlFor={id} className={cn("block cursor-pointer", disabled && "cursor-default text-fg-muted")}>
          {label}
        </Label>
        {hint ? (
          <FieldHint id={`${id}-hint`} className="mt-1 leading-relaxed">
            {hint}
          </FieldHint>
        ) : null}
      </div>
      <Switch id={id} checked={checked} onCheckedChange={onChange} disabled={disabled} aria-describedby={hint ? `${id}-hint` : undefined} className="mt-0.5" />
    </div>
  );
}

/** Textfält med enhet till höger i fältet ("min", "kr/mil" …). */
export const UnitInput = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement> & { unit?: string; wrapperClassName?: string }>(
  ({ unit, className, wrapperClassName, ...props }, ref) => (
    <div className={cn("relative w-full sm:w-36", wrapperClassName)}>
      <Input ref={ref} className={cn("text-right font-mono", unit ? "pr-[4.25rem]" : undefined, className)} autoComplete="off" {...props} />
      {unit ? (
        <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-xs text-fg-subtle" aria-hidden>
          {unit}
        </span>
      ) : null}
    </div>
  ),
);
UnitInput.displayName = "UnitInput";

/** Sifferinställning kopplad till ett fält i formuläret. */
export function NumberRow({
  form,
  path,
  id,
  label,
  hint,
  unit,
  decimal,
}: {
  form: SettingsForm;
  path: string;
  id: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  unit?: string;
  /** Tillåt decimaler (visar decimaltangentbord på mobil). */
  decimal?: boolean;
}) {
  if (!form.has(path)) return null;
  const error = form.error(path);
  return (
    <SettingRow label={label} hint={hint} htmlFor={id} error={error}>
      <UnitInput
        id={id}
        unit={unit}
        inputMode={decimal ? "decimal" : "numeric"}
        value={form.text(path)}
        onChange={(e) => form.set(path, e.target.value)}
        aria-invalid={error ? true : undefined}
        aria-describedby={hint ? `${id}-hint` : undefined}
      />
    </SettingRow>
  );
}

/** Av/på-inställning kopplad till ett fält i formuläret. */
export function BoolRow({
  form,
  path,
  id,
  label,
  hint,
  disabled,
}: {
  form: SettingsForm;
  path: string;
  id: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  disabled?: boolean;
}) {
  if (!form.has(path)) return null;
  return <ToggleRow id={id} label={label} hint={hint} checked={form.bool(path)} onChange={(v) => form.set(path, v)} disabled={disabled} />;
}

// ----------------------------------------------------------------------------- valkort

export interface ChoiceOption<T extends string> {
  value: T;
  title: string;
  description: React.ReactNode;
  /** Visar märket "Standard". */
  isDefault?: boolean;
}

/** Ett val mellan få alternativ, som kort med förklaring (radioknappar – piltangenter byter val). */
export function ChoiceCards<T extends string>({
  legend,
  hint,
  name,
  value,
  onChange,
  options,
}: {
  legend: string;
  hint?: React.ReactNode;
  name: string;
  value: T;
  onChange: (value: T) => void;
  options: ChoiceOption<T>[];
}) {
  return (
    <fieldset className="px-5 py-4">
      <legend className="float-left w-full text-[13px] font-medium text-fg">{legend}</legend>
      {hint ? <p className="clear-both pt-1 text-xs leading-relaxed text-fg-muted">{hint}</p> : null}
      <div className="clear-both grid gap-2.5 pt-3 sm:grid-cols-2">
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
              <input type="radio" name={name} value={o.value} checked={checked} onChange={() => onChange(o.value)} className="sr-only" />
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
                  {o.isDefault ? <Badge size="sm">Standard</Badge> : null}
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

// ----------------------------------------------------------------------------- reglage

/** Reglage (native range – piltangenter, Home/End och skärmläsare fungerar). */
export function Slider({
  id,
  value,
  min,
  max,
  step,
  onChange,
  valueText,
  describedBy,
}: {
  id: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  /** Klartext för skärmläsare, t.ex. "85 procent". */
  valueText: string;
  describedBy?: string;
}) {
  return (
    <input
      id={id}
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      aria-valuetext={valueText}
      aria-describedby={describedBy}
      className="block h-5 w-full cursor-pointer accent-accent focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring"
    />
  );
}

// ----------------------------------------------------------------------------- avancerat

/** Hopfälld del inuti ett kort för sådant som sällan behöver ändras. Öppnas av sig själv vid fel. */
export function Advanced({
  title = "Avancerat",
  summary,
  hasError,
  children,
}: {
  title?: string;
  /** Kort sammanfattning av innehållet, visas efter titeln. */
  summary?: React.ReactNode;
  hasError?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const [hadError, setHadError] = React.useState(false);
  if (!!hasError !== hadError) {
    setHadError(!!hasError);
    if (hasError) setOpen(true);
  }
  return (
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)} className="group/adv">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-5 py-3.5 text-[13px] font-medium text-fg-muted select-none hover:bg-surface-2/50 hover:text-fg focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-4 shrink-0 text-fg-subtle transition-transform group-open/adv:rotate-90" aria-hidden />
        <span>{title}</span>
        {summary ? <span className="min-w-0 truncate font-normal text-fg-subtle">– {summary}</span> : null}
      </summary>
      <div className="divide-y divide-border border-t border-border">{children}</div>
    </details>
  );
}

// ----------------------------------------------------------------------------- information

const NOTE_TONE = {
  info: "border-border bg-surface-2/60 text-fg-muted",
  accent: "border-accent/25 bg-accent-soft/50 text-fg",
  warn: "border-warn/40 bg-warn-soft text-warn-ink",
  err: "border-err/30 bg-err-soft text-err-ink",
} as const;

/** Informationsruta. */
export function Note({
  tone = "info",
  icon,
  children,
  className,
}: {
  tone?: keyof typeof NOTE_TONE;
  icon?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start gap-2.5 rounded-lg border p-3 text-[13px] leading-relaxed", NOTE_TONE[tone], className)}>
      <span className="mt-0.5 shrink-0 [&_svg]:size-4" aria-hidden>
        {icon ?? <Info />}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

// ----------------------------------------------------------------------------- sparrad

/**
 * Sparraden längst ner i ett formulärkort. Följer med i nederkanten av fönstret när det
 * finns osparade ändringar, så att "Spara" alltid är inom räckhåll i långa sektioner.
 */
export function SaveBar({
  dirty,
  invalid,
  error,
  saving,
  onReset,
  onDefaults,
  submitLabel = "Spara",
}: {
  dirty: boolean;
  /** Valideringsfel i formuläret – visas i raden och spärrar "Spara". */
  invalid?: string | null;
  /** Backendens felmeddelande från senaste sparförsöket (spärrar inte ett nytt försök). */
  error?: string | null;
  saving: boolean;
  onReset: () => void;
  /** Visar "Standardvärden" (fyller i standardvärdena utan att spara). */
  onDefaults?: () => void;
  submitLabel?: string;
}) {
  const message = dirty ? (invalid ?? error) : null;
  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-end gap-x-2 gap-y-2 rounded-b-lg bg-surface px-5 py-3",
        dirty && "sticky bottom-[calc(3.5rem+env(safe-area-inset-bottom))] z-10 lg:bottom-0",
      )}
    >
      <p role="status" aria-live="polite" className="mr-auto min-w-0 basis-full text-[13px] sm:flex-1 sm:basis-0">
        {message ? (
          <span className="font-medium text-err-ink">{message}</span>
        ) : dirty ? (
          <span className="inline-flex items-center gap-2 font-medium text-fg">
            <span className="size-1.5 rounded-full bg-warn" aria-hidden />
            Osparade ändringar
          </span>
        ) : (
          <span className="text-fg-subtle">Ändringar gäller först när du sparar.</span>
        )}
      </p>
      {onDefaults ? (
        <Button variant="ghost" size="sm" onClick={onDefaults} disabled={saving}>
          <RotateCcw aria-hidden /> Standardvärden
        </Button>
      ) : null}
      {dirty ? (
        <Button variant="ghost" size="sm" onClick={onReset} disabled={saving}>
          Ångra
        </Button>
      ) : null}
      <Button type="submit" variant="primary" size="sm" loading={saving} disabled={!dirty || !!invalid}>
        {submitLabel}
      </Button>
    </div>
  );
}

/** Sparrad för ett inställningsformulär (useSettingsForm + useSectionSave). */
export function SettingsSaveBar({
  form,
  save,
  defaults,
}: {
  form: SettingsForm;
  save: { saving: boolean; error: string | null };
  /** Erbjud "Standardvärden" när något avviker från standard. */
  defaults?: boolean;
}) {
  return (
    <SaveBar
      dirty={form.dirty}
      invalid={form.firstError}
      error={save.error}
      saving={save.saving}
      onReset={form.reset}
      onDefaults={defaults && form.differsFromDefaults ? form.applyDefaults : undefined}
    />
  );
}
