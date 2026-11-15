"use client";

/** Gemensamma byggstenar för integrationsvyn: status, sektioner, fält och demoläge. */

import * as React from "react";
import Link from "next/link";
import { AlertTriangle, Check, CheckCircle2, CircleDashed, CircleDot, FlaskConical, KeyRound, Lock } from "lucide-react";
import { toast } from "sonner";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmInline } from "@/components/ui/confirm-inline";
import { copyText, CopyButton } from "@/components/ui/copy-button";
import { FieldHint, Input, Label } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { IntegrationStatusCode } from "@/lib/integrations-types";
import { cn } from "@/lib/utils";
import { useVehicle } from "@/lib/vehicle";

// ----------------------------------------------------------------------------- status

/** Samma etiketter som backend (routes/integrations.py → STATUS_LABELS). */
export const STATUS_LABELS: Record<IntegrationStatusCode, string> = {
  not_configured: "Inte konfigurerad",
  configured: "Konfigurerad – inte ansluten",
  demo: "Demoläge",
  connected: "Ansluten",
  needs_reauth: "Behöver anslutas på nytt",
  error: "Fel",
};

const STATUS_TONE: Record<IntegrationStatusCode, NonNullable<BadgeProps["tone"]>> = {
  not_configured: "neutral",
  configured: "accent",
  demo: "warn",
  connected: "ok",
  needs_reauth: "warn",
  error: "err",
};

const STATUS_ICON: Record<IntegrationStatusCode, React.ComponentType<{ className?: string }>> = {
  not_configured: CircleDashed,
  configured: CircleDot,
  demo: FlaskConical,
  connected: CheckCircle2,
  needs_reauth: KeyRound,
  error: AlertTriangle,
};

/** Statusmärke. "Ansluten" (grön) visas ENDAST när API:t säger status "connected". */
export function StatusBadge({ status, className }: { status: IntegrationStatusCode; className?: string }) {
  const Icon = STATUS_ICON[status] ?? CircleDashed;
  return (
    <Badge tone={STATUS_TONE[status] ?? "neutral"} className={className} data-status={status}>
      <Icon aria-hidden />
      {STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

/** Relativ tid med exakt klockslag som title. */
export function TimeAgo({ value, empty = "Aldrig" }: { value: string | null | undefined; empty?: string }) {
  if (!value) return <span className="text-fg-subtle">{empty}</span>;
  return (
    <time dateTime={value} title={formatDateTime(value)}>
      {formatRelative(value)}
    </time>
  );
}

export function notifyError(err: unknown): void {
  toast.error(errorMessage(err));
}

// ----------------------------------------------------------------------------- layout

/** Ett steg/avsnitt i en konfigurationspanel. `step` ger en numrerad markör som bockas av med `done`. */
export function Section({
  title,
  description,
  step,
  done,
  action,
  children,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  step?: number;
  done?: boolean;
  action?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("border-t border-border pt-5 first:border-t-0 first:pt-0", className)}>
      <div className="flex items-start gap-3">
        {step != null ? (
          <span
            className={cn(
              "mt-px flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold",
              done ? "bg-ok-soft text-ok-ink" : "bg-surface-3 text-fg-muted",
            )}
          >
            {done ? <Check className="size-3" strokeWidth={3} aria-label="Klart" /> : step}
          </span>
        ) : null}
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-fg">{title}</h3>
          {description ? <p className="mt-1 text-[13px] leading-relaxed text-fg-muted">{description}</p> : null}
        </div>
        {action}
      </div>
      {children ? <div className={cn("mt-3 space-y-3", step != null && "sm:pl-8")}>{children}</div> : null}
    </section>
  );
}

const NOTE_TONE = {
  info: "border-border bg-surface-2/60 text-fg-muted",
  accent: "border-accent/30 bg-accent-soft/60 text-fg",
  ok: "border-ok/30 bg-ok-soft text-ok-ink",
  warn: "border-warn/40 bg-warn-soft text-warn-ink",
  err: "border-err/30 bg-err-soft text-err-ink",
} as const;

/** Informationsruta med ikon. */
export function Note({
  tone = "info",
  icon,
  title,
  children,
  className,
  role,
}: {
  tone?: keyof typeof NOTE_TONE;
  icon?: React.ReactNode;
  title?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
  role?: "status" | "alert";
}) {
  return (
    <div role={role} className={cn("flex items-start gap-2.5 rounded-lg border p-3 text-[13px] leading-relaxed", NOTE_TONE[tone], className)}>
      {icon ? <span className="mt-0.5 shrink-0 [&_svg]:size-4">{icon}</span> : null}
      <div className="min-w-0 flex-1 space-y-1.5">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children}
      </div>
    </div>
  );
}

/** Senaste felet från integrationen – visas överst i panelen när det finns. */
export function LastErrorNote({ error, at }: { error: string | null; at?: string | null }) {
  if (!error) return null;
  return (
    <Note tone="err" icon={<AlertTriangle aria-hidden />} title="Senaste fel" role="status">
      <p className="break-words">{error}</p>
      {at ? (
        <p className="text-xs opacity-80">
          <TimeAgo value={at} />
        </p>
      ) : null}
    </Note>
  );
}

/**
 * Överst i varje panel: antingen "anslut på nytt" (med felet som förklaring) eller
 * senaste felet. Aldrig båda – ett besked räcker.
 */
export function StatusNote({
  status,
  error,
  at,
  reauthTitle,
  reauthHint,
}: {
  status: IntegrationStatusCode;
  error: string | null;
  at?: string | null;
  reauthTitle: string;
  reauthHint: string;
}) {
  if (status !== "needs_reauth") return <LastErrorNote error={error} at={at} />;
  return (
    <Note tone="warn" icon={<KeyRound aria-hidden />} title={reauthTitle} role="status">
      <p>{reauthHint}</p>
      {error ? <p className="text-xs break-words opacity-80">Senaste fel: {error}</p> : null}
    </Note>
  );
}

/** Etikett/värde-rader (översiktskort och sammanfattningar). */
export function Facts({ children, className }: { children: React.ReactNode; className?: string }) {
  return <dl className={cn("space-y-2.5 text-[13px]", className)}>{children}</dl>;
}

export function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-3">
      <dt className="text-fg-muted">{label}</dt>
      <dd className="min-w-0 text-fg">{children}</dd>
    </div>
  );
}

// ----------------------------------------------------------------------------- fält

export function Field({
  label,
  htmlFor,
  hint,
  aside,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: React.ReactNode;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={htmlFor}>{label}</Label>
        {aside}
      </div>
      {children}
      {hint ? <FieldHint>{hint}</FieldHint> : null}
    </div>
  );
}

/**
 * Fält för en hemlighet (nyckel, token). Sparade värden visas aldrig – bara att de
 * finns. Tomt fält = behåll det sparade värdet.
 */
export function SecretField({
  label,
  saved,
  value,
  onChange,
  hint,
  placeholder,
  required,
}: {
  label: string;
  saved: boolean;
  value: string;
  onChange: (value: string) => void;
  hint?: React.ReactNode;
  placeholder?: string;
  required?: boolean;
}) {
  const id = React.useId();
  return (
    <Field
      label={label}
      htmlFor={id}
      hint={hint}
      aside={
        saved ? (
          <Badge tone="ok" size="sm">
            <Lock aria-hidden /> Sparad
          </Badge>
        ) : null
      }
    >
      <Input
        id={id}
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={saved ? "Sparad – lämna tomt för att behålla" : placeholder}
        required={required && !saved}
        className="font-mono"
      />
    </Field>
  );
}

export { copyText, CopyButton };

/** Skrivskyddat värde med kopiera-knapp (t.ex. redirect-URI). */
export function CopyField({ label, value, hint }: { label: string; value: string; hint?: React.ReactNode }) {
  const id = React.useId();
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <div className="flex gap-2">
        <Input id={id} readOnly value={value} onFocus={(e) => e.currentTarget.select()} className="bg-surface-2 font-mono text-xs" />
        <CopyButton value={value} label={`Kopiera ${label.toLowerCase()}`} />
      </div>
    </Field>
  );
}

// ----------------------------------------------------------------------------- bekräftelse

/** Bekräftelse på plats (components/ui/confirm-inline) – knappen linjerar med texten ovanför. */
export function ConfirmButton(props: Omit<React.ComponentProps<typeof ConfirmInline>, "triggerClassName" | "ariaLabel">) {
  return <ConfirmInline {...props} triggerClassName="-ml-2" />;
}

// ----------------------------------------------------------------------------- demoläge

/** Tydlig märkning av demodata – samma färg och ikon som banderollen för demofordon. */
export function DemoTag({ className }: { className?: string }) {
  return (
    <Badge tone="warn" className={cn("font-semibold tracking-wide uppercase", className)}>
      <FlaskConical aria-hidden /> Demo
    </Badge>
  );
}

/**
 * Förklarar att demoläget använder ett eget demofordon och låter användaren byta
 * till det (eller tillbaka) i fordonsväljaren.
 */
export function DemoVehicleSwitch() {
  const { vehicles, vehicle, setVehicleId } = useVehicle();
  const demo = vehicles.find((v) => v.is_demo);
  const real = vehicles.find((v) => !v.is_demo);
  if (!demo) return null;
  const active = vehicle?.id === demo.id;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px]">
      <span className="text-fg-muted">
        {active ? (
          <>
            Fordonsväljaren visar nu <span className="font-medium text-fg">{demo.name}</span>.
          </>
        ) : (
          <>
            Demoresorna hör till <span className="font-medium text-fg">{demo.name}</span> – välj den i fordonsväljaren för att se dem.
          </>
        )}
      </span>
      {active ? (
        <>
          <Button asChild variant="link" size="sm" className="h-auto px-0">
            <Link href="/journal">Öppna körjournalen</Link>
          </Button>
          {real ? (
            <Button variant="link" size="sm" className="h-auto px-0" onClick={() => setVehicleId(real.id)}>
              Byt tillbaka till {real.name}
            </Button>
          ) : null}
        </>
      ) : (
        <Button variant="link" size="sm" className="h-auto px-0" onClick={() => setVehicleId(demo.id)}>
          Byt till demobilen
        </Button>
      )}
    </div>
  );
}

/**
 * Demoläge för en integration: avstängt → kort förklaring och "Prova med demodata";
 * påslaget → tydligt märkt ruta med innehåll (`children`) och "Stäng av demoläge".
 */
export function DemoBlock({
  enabled,
  description,
  onToggle,
  pending,
  children,
}: {
  enabled: boolean;
  description: React.ReactNode;
  onToggle: (enable: boolean) => void;
  pending: boolean;
  children?: React.ReactNode;
}) {
  if (!enabled) {
    return (
      <Section title="Prova utan konto" description={description}>
        <Button onClick={() => onToggle(true)} loading={pending} data-testid="demo-enable">
          <FlaskConical aria-hidden /> Prova med demodata
        </Button>
      </Section>
    );
  }
  return (
    <section className="border-t border-border pt-5 first:border-t-0 first:pt-0">
      <div className="rounded-lg border border-warn/40 bg-warn-soft/40 p-4" data-testid="demo-block">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <DemoTag />
            <h3 className="text-sm font-semibold">Demoläget är på</h3>
          </div>
          <Button size="sm" onClick={() => onToggle(false)} loading={pending} data-testid="demo-disable">
            Stäng av demoläge
          </Button>
        </div>
        <p className="mt-2 text-[13px] leading-relaxed text-fg-muted">{description}</p>
        {children ? <div className="mt-4 space-y-4">{children}</div> : null}
      </div>
    </section>
  );
}
