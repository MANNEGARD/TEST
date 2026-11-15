"use client";

import * as React from "react";
import { AlertTriangle, Eye, EyeOff, KeyRound, Plus } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { FieldError, Input } from "@/components/ui/input";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDate, formatRelative } from "@/lib/format";
import { useApiTokens, useChangePassword, useCreateApiToken, useRevokeApiToken } from "@/lib/queries/settings-page";
import type { ApiToken, ApiTokenCreated, SettingsPayload } from "@/lib/settings-page-types";
import { ConfirmInline, CopyButton } from "./actions";
import { Field, GroupHeading, SectionIntro } from "./form-parts";

/** backend/app/security/passwords.py → MIN_PASSWORD_LENGTH */
const MIN_PASSWORD = 10;
const MAX_TOKEN_NAME = 120;

function PasswordInput({
  id,
  value,
  onChange,
  autoComplete,
  invalid,
  describedBy,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  autoComplete: string;
  invalid?: boolean;
  describedBy?: string;
}) {
  const [show, setShow] = React.useState(false);
  return (
    <div className="relative">
      <Input
        id={id}
        type={show ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        aria-invalid={invalid ? true : undefined}
        aria-describedby={describedBy}
        maxLength={1000}
        className="pr-10"
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        aria-label={show ? "Dölj lösenordet" : "Visa lösenordet"}
        aria-pressed={show}
        className="absolute inset-y-0 right-0 flex w-10 items-center justify-center rounded-r-md text-fg-subtle hover:text-fg focus-visible:outline-2 focus-visible:outline-ring [&_svg]:size-4"
      >
        {show ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
      </button>
    </div>
  );
}

function ChangePassword() {
  const change = useChangePassword();
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [repeat, setRepeat] = React.useState("");
  const [submitted, setSubmitted] = React.useState(false);
  const [serverError, setServerError] = React.useState<{ field: "current" | "next" | null; message: string } | null>(null);

  const errors = {
    current: !current ? "Ange ditt nuvarande lösenord." : null,
    next: !next ? "Ange ett nytt lösenord." : next.length < MIN_PASSWORD ? `Minst ${MIN_PASSWORD} tecken.` : next === current ? "Det nya lösenordet är samma som det nuvarande." : null,
    repeat: repeat !== next ? "Lösenorden är inte likadana." : null,
  };
  const show = (f: keyof typeof errors) => (submitted ? errors[f] : null) ?? (serverError?.field === f ? serverError.message : null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (errors.current || errors.next || errors.repeat) return;
    setServerError(null);
    change.mutate(
      { current_password: current, new_password: next },
      {
        onSuccess: () => {
          setCurrent("");
          setNext("");
          setRepeat("");
          setSubmitted(false);
          toast.success("Lösenordet är bytt. Andra inloggade enheter har loggats ut.");
        },
        onError: (err) => {
          const code = err instanceof ApiError ? err.code : null;
          const field = code === "invalid_credentials" ? "current" : code === "weak_password" ? "next" : null;
          setServerError({ field, message: errorMessage(err) });
          toast.error(errorMessage(err));
        },
      },
    );
  };

  const edit = (setter: (v: string) => void) => (v: string) => {
    setter(v);
    setServerError(null);
  };

  return (
    <form onSubmit={submit} noValidate aria-label="Byt lösenord">
      <GroupHeading description="Andra inloggade enheter loggas ut när du byter lösenord.">Byt lösenord</GroupHeading>
      <div className="grid gap-4 px-5 py-4 sm:grid-cols-2">
        <Field id="pw-current" label="Nuvarande lösenord" error={show("current")} className="sm:col-span-2 sm:max-w-[calc(50%-0.5rem)]">
          <PasswordInput id="pw-current" value={current} onChange={edit(setCurrent)} autoComplete="current-password" invalid={!!show("current")} />
        </Field>
        <Field id="pw-new" label="Nytt lösenord" hint={`Minst ${MIN_PASSWORD} tecken. En fras med flera ord är lätt att minnas.`} error={show("next")}>
          <PasswordInput id="pw-new" value={next} onChange={edit(setNext)} autoComplete="new-password" invalid={!!show("next")} describedBy="pw-new-hint" />
        </Field>
        <Field id="pw-repeat" label="Upprepa nytt lösenord" error={show("repeat")}>
          <PasswordInput id="pw-repeat" value={repeat} onChange={edit(setRepeat)} autoComplete="new-password" invalid={!!show("repeat")} />
        </Field>
      </div>
      {serverError && !serverError.field ? <FieldError className="px-5 pb-2">{serverError.message}</FieldError> : null}
      <div className="flex justify-end border-t border-border px-5 py-3">
        <Button type="submit" variant="primary" size="sm" loading={change.isPending} data-testid="change-password-submit">
          Byt lösenord
        </Button>
      </div>
    </form>
  );
}

function TokenRow({ token, onRevoked }: { token: ApiToken; onRevoked: (id: string) => void }) {
  const revoke = useRevokeApiToken();
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-2 px-5 py-3" data-testid="settings-token-row">
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="font-medium text-fg">{token.name}</span>
          <span className="font-mono text-xs text-fg-muted">{token.token_prefix}…</span>
        </p>
        <p className="mt-0.5 text-xs text-fg-muted">
          Skapad {formatDate(token.created_at)} · {token.last_used_at ? `senast använd ${formatRelative(token.last_used_at)}` : "aldrig använd"}
        </p>
      </div>
      <ConfirmInline
        size="xs"
        label="Återkalla"
        ariaLabel={`Återkalla token ${token.name}`}
        confirmLabel="Ja, återkalla"
        description="Det som använder den här tokenen, t.ex. Home Assistant, slutar fungera direkt. Det går inte att ångra – skapa en ny token om du behöver."
        loading={revoke.isPending}
        onConfirm={() =>
          revoke.mutate(token.id, {
            onSuccess: () => {
              onRevoked(token.id);
              toast.success(`Token ”${token.name}” är återkallad.`);
            },
            onError: (err) => toast.error(errorMessage(err)),
          })
        }
      />
    </li>
  );
}

function ApiTokens() {
  const tokens = useApiTokens();
  const create = useCreateApiToken();
  const [name, setName] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [created, setCreated] = React.useState<ApiTokenCreated | null>(null);
  const active = (tokens.data ?? []).filter((t) => !t.revoked_at);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const n = name.trim();
    if (!n) return setError("Ge tokenen ett namn, t.ex. Home Assistant.");
    if (n.length > MAX_TOKEN_NAME) return setError(`Högst ${MAX_TOKEN_NAME} tecken.`);
    setError(null);
    create.mutate(n, {
      onSuccess: (t) => {
        setCreated(t);
        setName("");
        toast.success("Token skapad – kopiera den nu.");
      },
      onError: (err) => setError(errorMessage(err)),
    });
  };

  return (
    <div>
      <GroupHeading description="Används av Home Assistant-integrationen för att hämta frågor och skicka svar. En token ger bara åtkomst till det gränssnittet.">
        API-tokens
      </GroupHeading>
      <div className="space-y-4 px-5 py-4">
        {created ? (
          <div role="status" className="space-y-2.5 rounded-lg border border-ok/30 bg-ok-soft/50 p-3" data-testid="settings-new-token">
            <p className="text-sm font-semibold text-fg">Ny token: {created.name}</p>
            <div className="flex gap-2">
              <Input readOnly value={created.token} aria-label="Ny API-token" onFocus={(e) => e.currentTarget.select()} className="font-mono text-xs" />
              <CopyButton value={created.token} label="Kopiera token" />
            </div>
            <p className="flex items-start gap-1.5 text-xs leading-relaxed font-medium text-warn-ink">
              <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
              Kopiera den nu – den visas inte igen. Servern sparar bara ett hashvärde.
            </p>
            <Button size="sm" variant="ghost" className="-ml-2" onClick={() => setCreated(null)}>
              Jag har sparat den
            </Button>
          </div>
        ) : (
          <form onSubmit={submit} noValidate className="flex flex-col gap-2 sm:flex-row sm:items-start" aria-label="Skapa API-token">
            <div className="min-w-0 flex-1">
              <Input
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  setError(null);
                }}
                aria-label="Namn på ny token"
                aria-invalid={error ? true : undefined}
                placeholder="Namn, t.ex. Home Assistant"
                maxLength={MAX_TOKEN_NAME + 10}
                autoComplete="off"
              />
              <FieldError className="mt-1.5">{error}</FieldError>
            </div>
            <Button type="submit" loading={create.isPending}>
              {create.isPending ? null : <Plus aria-hidden />} Skapa token
            </Button>
          </form>
        )}
      </div>
      {tokens.isError ? (
        <ErrorState compact error={tokens.error} onRetry={() => tokens.refetch()} title="Tokens gick inte att hämta" />
      ) : !tokens.data ? (
        <div className="px-5 pb-4">
          <SkeletonBlock rows={2} />
        </div>
      ) : active.length === 0 ? (
        <EmptyState compact icon={<KeyRound />} title="Inga aktiva tokens" description="Skapa en token när du kopplar Home Assistant." className="pt-2" />
      ) : (
        <ul className="divide-y divide-border border-t border-border" aria-label="Aktiva API-tokens">
          {active.map((t) => (
            <TokenRow key={t.id} token={t} onRevoked={(id) => created?.id === id && setCreated(null)} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** 10. Säkerhet – lösenord och API-tokens. */
export function SecuritySection(_props: { data: SettingsPayload }) {
  return (
    <section aria-labelledby="settings-sakerhet">
      <SectionIntro id="settings-sakerhet" title="Säkerhet">
        Ditt lösenord och de nycklar som andra system använder för att nå körjournalen.
      </SectionIntro>
      <div className="space-y-4">
        <Card>
          <ChangePassword />
        </Card>
        <Card>
          <ApiTokens />
        </Card>
      </div>
    </section>
  );
}
