"use client";

import * as React from "react";
import Link from "next/link";
import { Check, FlaskConical, Info, ShieldCheck, Trash2, Upload, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { FieldError, Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { errorMessage } from "@/lib/api";
import type { AiProviderName, AiStatus } from "@/lib/integrations-types";
import { useConfigureAi, useRemoveAi, useTestAi } from "@/lib/queries/integrations";
import { ConfirmButton, Field, Note, notifyError, Section, SecretField, StatusNote } from "./shared";

export const AI_PROVIDER_LABELS: Record<AiProviderName, string> = {
  none: "Ingen",
  anthropic: "Anthropic",
  openai: "OpenAI",
};

/** "Testa" med kvitto – används både på kortet och i panelen. */
export function useAiTestAction() {
  const test = useTestAi();
  return {
    isPending: test.isPending,
    run: () =>
      test.mutate(undefined, {
        onSuccess: (r) => toast.success("Nyckeln och modellen fungerar.", { description: r.model ? `Svar från ${r.model}.` : undefined }),
        onError: notifyError,
      }),
  };
}

function PrivacyFlag({ label, sent }: { label: string; sent: boolean }) {
  return (
    <li className="flex items-center justify-between gap-3 px-3 py-2">
      <span>{label}</span>
      <span className={sent ? "inline-flex items-center gap-1 font-medium text-fg" : "inline-flex items-center gap-1 text-fg-muted"}>
        {sent ? <Check className="size-3.5" aria-hidden /> : <X className="size-3.5" aria-hidden />}
        {sent ? "Skickas" : "Skickas inte"}
      </span>
    </li>
  );
}

export function AiPanel({ status }: { status: AiStatus }) {
  const save = useConfigureAi();
  const remove = useRemoveAi();
  const test = useAiTestAction();
  const [provider, setProvider] = React.useState<AiProviderName>(status.provider_name);
  const [model, setModel] = React.useState(status.model);
  const [apiKey, setApiKey] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const modelField = React.useId();

  const defaultModel = provider === "none" ? "" : status.default_models[provider];
  const hasSavedKey = provider !== "none" && status.has_key[provider];
  const dirty = provider !== status.provider_name || apiKey.trim() !== "" || (provider !== "none" && model.trim() !== status.model);
  const anyKey = status.has_key.anthropic || status.has_key.openai;

  const chooseProvider = (next: AiProviderName) => {
    setProvider(next);
    setApiKey("");
    setError(null);
    // Förifyll leverantörens standardmodell (eller den sparade modellen för den valda leverantören).
    setModel(next === "none" ? "" : next === status.provider_name ? status.model : status.default_models[next]);
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const chosenModel = model.trim();
    save.mutate(
      // Tom modell = följ standardmodellen, även om den byts i en senare version.
      { provider, model: provider === "none" || chosenModel === defaultModel ? "" : chosenModel, api_key: apiKey.trim() || undefined },
      {
        onSuccess: (s) => {
          setApiKey("");
          setModel(s.model);
          if (provider === "none") toast.success("AI är avstängt.", { description: "Reglerna klassar resorna precis som tidigare." });
          else toast.success("Sparat.", { description: "Välj Testa för att verifiera nyckeln mot leverantören." });
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <>
      <Note tone="accent" icon={<Info aria-hidden />} title="AI är valfritt">
        <p>
          Reglerna och det du själv har bekräftat klassar resorna – allt fungerar utan AI. En AI-tjänst används bara för att ge bättre förslag på kategori och syfte när
          reglerna inte räcker, och du godkänner alltid själv.
        </p>
      </Note>
      <StatusNote
        status={status.status}
        error={status.last_error}
        at={status.last_error_at}
        reauthTitle="Leverantören godkände inte nyckeln"
        reauthHint="Ange en ny API-nyckel och testa igen. Tills dess används bara reglerna."
      />

      <Section title="Leverantör">
        <form onSubmit={submit} className="space-y-3" noValidate>
          <SegmentedControl
            ariaLabel="AI-leverantör"
            size="lg"
            className="w-full [&>button]:flex-1"
            value={provider}
            onChange={chooseProvider}
            options={(["none", "anthropic", "openai"] as const).map((p) => ({ value: p, label: AI_PROVIDER_LABELS[p] }))}
          />
          {provider === "none" ? (
            <p className="text-[13px] leading-relaxed text-fg-muted">Ingen AI-tjänst används. Resorna klassas av reglerna, dina platser och det du har bekräftat tidigare.</p>
          ) : (
            <>
              <Field label="Modell" htmlFor={modelField} hint={`Standard: ${defaultModel}. Lämna som den är om du inte vill använda en annan modell.`}>
                <Input id={modelField} value={model} onChange={(e) => setModel(e.target.value)} placeholder={defaultModel} autoComplete="off" spellCheck={false} className="font-mono" />
              </Field>
              <SecretField
                label={`API-nyckel för ${AI_PROVIDER_LABELS[provider]}`}
                saved={hasSavedKey}
                value={apiKey}
                onChange={setApiKey}
                hint="Lagras krypterat och visas aldrig igen."
                required
              />
            </>
          )}
          <FieldError>{error}</FieldError>
          <div className={provider === "none" && !dirty ? "hidden" : "flex flex-wrap items-center gap-2"}>
            <Button type="submit" variant={dirty ? "primary" : "secondary"} loading={save.isPending} disabled={!dirty}>
              Spara
            </Button>
            {provider !== "none" ? (
              <Button onClick={test.run} loading={test.isPending} disabled={dirty || status.provider_name === "none"} data-testid="ai-test">
                <FlaskConical aria-hidden /> Testa
              </Button>
            ) : null}
            {provider !== "none" && dirty ? <span className="text-xs text-fg-muted">Spara först, testa sedan.</span> : null}
          </div>
        </form>
      </Section>

      <Section title="Vad som skickas" description="Gäller bara när en leverantör är vald. Varje förfrågan innehåller så lite som möjligt.">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-lg border border-border p-3">
            <p className="flex items-center gap-1.5 text-xs font-semibold text-fg">
              <Upload className="size-3.5 text-fg-muted" aria-hidden /> Skickas
            </p>
            <p className="mt-1.5 text-[13px] leading-relaxed text-fg-muted">{status.privacy.sends}</p>
          </div>
          <div className="rounded-lg border border-ok/30 bg-ok-soft/50 p-3">
            <p className="flex items-center gap-1.5 text-xs font-semibold text-ok-ink">
              <ShieldCheck className="size-3.5" aria-hidden /> Skickas aldrig
            </p>
            <p className="mt-1.5 text-[13px] leading-relaxed text-fg">{status.privacy.never_sends}</p>
          </div>
        </div>
        <ul className="divide-y divide-border rounded-lg border border-border text-[13px]">
          <PrivacyFlag label="Kalenderhändelsers rubrik" sent={status.privacy.send_calendar_subjects} />
          <PrivacyFlag label="Kalenderhändelsers brödtext" sent={status.privacy.send_calendar_body} />
          <PrivacyFlag label="Gatuadresser" sent={status.privacy.send_street_addresses} />
        </ul>
        <p className="text-xs text-fg-muted">
          De tre valen ändras under{" "}
          <Link href="/settings" className="font-medium text-accent-ink underline-offset-2 hover:underline">
            Inställningar
          </Link>
          .
        </p>
      </Section>

      {anyKey ? (
        <Section title="Ta bort nyckel">
          <ConfirmButton
            icon={<Trash2 aria-hidden />}
            label="Ta bort API-nyckel"
            confirmLabel="Ja, ta bort"
            description="Alla sparade API-nycklar raderas och AI stängs av. Reglerna fortsätter klassa resorna. Nyckeln måste anges igen om du vill använda AI senare."
            loading={remove.isPending}
            onConfirm={() =>
              remove.mutate(undefined, {
                onSuccess: () => {
                  setProvider("none");
                  setModel("");
                  setApiKey("");
                  toast.success("API-nyckeln är borttagen.", { description: "AI är avstängt – reglerna används." });
                },
                onError: notifyError,
              })
            }
          />
        </Section>
      ) : null}
    </>
  );
}
