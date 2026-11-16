"use client";

import * as React from "react";
import { AlertTriangle, BellRing, CheckCircle2, Play, PlugZap, Plus, Unplug } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDate, plural } from "@/lib/format";
import type { ApiTokenCreated, DeliveryMode, HaStatus, HaTestResult } from "@/lib/integrations-types";
import {
  useApiTokens,
  useConfigureHa,
  useCreateHaToken,
  useHaDemo,
  useHaDevices,
  useHaDisconnect,
  useHaStatus,
  useHaTestNotification,
  useRevokeApiToken,
  useTestHa,
  useVolvoSimulate,
  useVolvoStatus,
} from "@/lib/queries/integrations";
import { DEMO_MODE } from "@/lib/demo-hook";
import { DemoPhone } from "./demo-phone";
import { ConfirmButton, CopyButton, CopyField, DemoBlock, DemoVehicleSwitch, Fact, Facts, Field, Note, notifyError, Section, SecretField, StatusNote, TimeAgo } from "./shared";

export const DELIVERY_LABELS: Record<DeliveryMode, string> = {
  pull: "Home Assistant hämtar",
  push: "DriveLog skickar",
};

/** "Skicka testnotis" med kvitto – används både på kortet och i panelen. */
export function useTestNotificationAction() {
  const send = useHaTestNotification();
  return {
    isPending: send.isPending,
    run: () =>
      send.mutate(undefined, {
        onSuccess: (r) => (r.delivered || r.queued || r.demo ? toast.success(r.message) : toast.error(r.message)),
        onError: notifyError,
      }),
  };
}

// ----------------------------------------------------------------------------- API-tokens

function Tokens() {
  const tokens = useApiTokens();
  const create = useCreateHaToken();
  const revoke = useRevokeApiToken();
  const [created, setCreated] = React.useState<ApiTokenCreated | null>(null);
  const active = (tokens.data ?? []).filter((t) => !t.revoked_at);

  return (
    <>
      {created ? (
        <div role="status" className="space-y-2.5 rounded-lg border border-ok/30 bg-ok-soft/50 p-3" data-testid="new-token">
          <p className="text-sm font-semibold text-fg">Din nya token</p>
          <div className="flex gap-2">
            <Input readOnly value={created.token} aria-label="Ny API-token" onFocus={(e) => e.currentTarget.select()} className="font-mono text-xs" />
            <CopyButton value={created.token} label="Kopiera token" />
          </div>
          <p className="flex items-start gap-1.5 text-xs leading-relaxed font-medium text-warn-ink">
            <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
            Kopiera den nu – den visas inte igen. DriveLog sparar bara ett hashvärde.
          </p>
          <Button size="sm" variant="ghost" className="-ml-2" onClick={() => setCreated(null)}>
            Jag har sparat den
          </Button>
        </div>
      ) : (
        <Button
          loading={create.isPending}
          data-testid="create-token"
          onClick={() =>
            create.mutate("Home Assistant", {
              onSuccess: (t) => {
                setCreated(t);
                toast.success("Token skapad – kopiera den innan du stänger panelen.");
              },
              onError: notifyError,
            })
          }
        >
          <Plus aria-hidden /> Skapa API-token för Home Assistant
        </Button>
      )}

      {tokens.isLoading ? (
        <SkeletonBlock rows={2} />
      ) : tokens.isError ? (
        <ErrorState compact error={tokens.error} onRetry={() => void tokens.refetch()} title="Tokens kunde inte hämtas" />
      ) : active.length > 0 ? (
        <ul className="divide-y divide-border rounded-lg border border-border" aria-label="Aktiva API-tokens">
          {active.map((t) => (
            <li key={t.id} className="space-y-1 px-3 py-2.5" data-testid="token-row">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="text-sm font-medium">{t.name}</span>
                <span className="font-mono text-xs text-fg-muted">{t.token_prefix}…</span>
              </div>
              <p className="text-xs text-fg-muted">
                Skapad {formatDate(t.created_at)} · senast använd <TimeAgo value={t.last_used_at} empty="aldrig" />
              </p>
              <ConfirmButton
                size="xs"
                label="Återkalla"
                confirmLabel="Ja, återkalla"
                description="Home Assistant kan inte längre svara på frågor eller hämta notiser med den här tokenen. Det går inte att ångra – skapa en ny om du behöver."
                loading={revoke.isPending && revoke.variables === t.id}
                onConfirm={() =>
                  revoke.mutate(t.id, {
                    onSuccess: () => {
                      if (created?.id === t.id) setCreated(null);
                      toast.success("Token är återkallad.");
                    },
                    onError: notifyError,
                  })
                }
              />
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

// ----------------------------------------------------------------------------- push: adress och token

function PushForm({ status }: { status: HaStatus }) {
  const save = useConfigureHa();
  const [baseUrl, setBaseUrl] = React.useState(status.base_url ?? "");
  const [token, setToken] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const urlField = React.useId();
  const dirty = baseUrl.trim() !== (status.base_url ?? "") || token.trim() !== "";

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    save.mutate(
      { base_url: baseUrl.trim(), token: token.trim() || undefined },
      {
        onSuccess: () => {
          setToken("");
          toast.success("Uppgifterna är sparade.", { description: "Välj Testa anslutning för att verifiera dem." });
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} className="space-y-3" noValidate>
      <Field label="Adress till Home Assistant" htmlFor={urlField} hint="Måste kunna nås från DriveLog-servern.">
        <Input id={urlField} type="url" inputMode="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://homeassistant.local:8123" autoComplete="off" spellCheck={false} />
      </Field>
      <SecretField
        label="Long-lived access token"
        saved={status.has_token}
        value={token}
        onChange={setToken}
        hint="Skapas i Home Assistant under din profil → Säkerhet → Långlivade åtkomsttokens. Lagras krypterat."
      />
      <FieldError>{error}</FieldError>
      <Button type="submit" variant={dirty ? "primary" : "secondary"} loading={save.isPending} disabled={!dirty || !baseUrl.trim()}>
        Spara
      </Button>
    </form>
  );
}

// ----------------------------------------------------------------------------- enhet

function DevicePicker({ status }: { status: HaStatus }) {
  const devices = useHaDevices(true);
  const save = useConfigureHa();
  const [manual, setManual] = React.useState(status.notify_service ?? "");
  const manualField = React.useId();

  const choose = (service: string) =>
    save.mutate(
      { notify_service: service },
      {
        onSuccess: (s) => toast.success(s.notify_service ? `Notiser skickas till ${s.notify_service}.` : "Ingen enhet är vald."),
        onError: notifyError,
      },
    );

  if (devices.isLoading) return <SkeletonBlock rows={1} />;
  const list = devices.data?.devices ?? [];
  const options = status.notify_service && !list.includes(status.notify_service) ? [status.notify_service, ...list] : list;

  if (options.length > 0 && !devices.isError) {
    return (
      <div className="space-y-1.5">
        <Label id="ha-device-label">Mobil enhet</Label>
        <Select value={status.notify_service ?? undefined} onValueChange={choose} disabled={save.isPending}>
          <SelectTrigger aria-labelledby="ha-device-label" className="font-mono text-[13px]">
            <SelectValue placeholder="Välj enhet" />
          </SelectTrigger>
          <SelectContent>
            {options.map((d) => (
              <SelectItem key={d} value={d} className="font-mono text-[13px]">
                notify.{d}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldHint>Telefoner med Home Assistants Companion-app. Knappar i notiser kräver en tjänst av typen notify.mobile_app_….</FieldHint>
      </div>
    );
  }

  // Ingen lista ännu (eller Home Assistant svarar inte): låt användaren ange tjänsten själv.
  return (
    <form
      className="space-y-1.5"
      onSubmit={(e) => {
        e.preventDefault();
        choose(manual.trim());
      }}
    >
      <Label htmlFor={manualField}>Mobil enhet (notify-tjänst)</Label>
      <div className="flex gap-2">
        <Input id={manualField} value={manual} onChange={(e) => setManual(e.target.value)} placeholder="notify.mobile_app_min_iphone" autoComplete="off" spellCheck={false} className="font-mono text-[13px]" />
        <Button type="submit" loading={save.isPending} disabled={manual.trim() === (status.notify_service ?? "")}>
          Spara
        </Button>
      </div>
      <FieldHint>
        {devices.isError
          ? `Enhetslistan kunde inte hämtas (${errorMessage(devices.error)}) – ange tjänsten manuellt.`
          : status.delivery_mode === "pull"
            ? "Listan fylls i när integrationen har hört av sig. Lämnar du fältet tomt används enheten som valts i Home Assistant."
            : "Listan fylls i när anslutningen till Home Assistant fungerar."}
      </FieldHint>
    </form>
  );
}

// ----------------------------------------------------------------------------- panel

export function HaPanel({ status }: { status: HaStatus }) {
  useHaStatus({ poll: true }); // livstecken och bekräftad testnotis syns utan omladdning
  const configure = useConfigureHa();
  const test = useTestHa();
  const testNotification = useTestNotificationAction();
  const disconnect = useHaDisconnect();
  const demo = useHaDemo();
  const volvo = useVolvoStatus();
  const simulate = useVolvoSimulate();
  const [testResult, setTestResult] = React.useState<HaTestResult | null>(null);

  const isDemo = status.demo;
  const mode = status.delivery_mode;
  const hb = status.heartbeat;
  const connected = status.status === "connected";
  const hasRealConnection = connected || status.status === "error" || status.status === "needs_reauth";
  // Pull-läget: "Inte konfigurerad" tills integrationen hört av sig första gången. Kommer livstecken
  // ändå (färska) är Home Assistant bortkopplat i webben – det gäller tills inställningarna sparas igen.
  const pullWaiting = !isDemo && mode === "pull" && status.status === "not_configured";
  const pullDisconnected = pullWaiting && hb.fresh;

  const reconnect = () =>
    configure.mutate(
      {},
      { onSuccess: () => toast.success("Home Assistant är anslutet igen."), onError: notifyError },
    );
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  const setMode = (next: DeliveryMode) => {
    if (next === mode) return;
    setTestResult(null);
    configure.mutate(
      { delivery_mode: next },
      { onSuccess: () => toast.success(`Leveransläge: ${DELIVERY_LABELS[next]}.`), onError: notifyError },
    );
  };

  const runTest = () =>
    test.mutate(undefined, {
      onSuccess: (r) => {
        setTestResult(r); // beskedet står kvar under knappen; toast bara som kvitto när det gick bra
        if (r.ok) toast.success(r.message);
      },
      onError: (err) => {
        setTestResult(null);
        notifyError(err);
      },
    });

  // Visas bara medan det finns en kanal att leverera genom – annars är uppgiften inaktuell.
  const confirmed = status.channel ? status.last_test_confirmed_at : null;
  const awaiting = !!status.channel && !!status.last_test_sent_at && !confirmed;

  return (
    <>
      <StatusNote
        status={status.status}
        error={status.last_error}
        at={status.last_error_at}
        reauthTitle="Home Assistant godkände inte token"
        reauthHint="Ange en ny long-lived access token och testa anslutningen igen."
      />

      {!isDemo ? (
        <>
          <Section
            title="Så levereras notiserna"
            description={
              mode === "pull"
                ? "Integrationen i Home Assistant hämtar nya frågor var 30:e sekund. Inget behöver öppnas mot Home Assistant och ingen token därifrån sparas här – det enklaste och säkraste valet."
                : "DriveLog anropar Home Assistant direkt, så notisen kommer omedelbart. Kräver att DriveLog når din Home Assistant och att en long-lived token sparas här."
            }
          >
            <SegmentedControl
              ariaLabel="Leveransläge"
              size="lg"
              className="w-full [&>button]:flex-1"
              value={mode}
              onChange={setMode}
              disabled={configure.isPending}
              options={[
                { value: "pull", label: DELIVERY_LABELS.pull },
                { value: "push", label: DELIVERY_LABELS.push },
              ]}
            />
          </Section>

          <Section
            step={1}
            title="API-token till Home Assistant"
            description="Integrationen SMGD DriveLog i Home Assistant loggar in hos DriveLog med en token. Den behövs i båda lägena, eftersom svaren på frågorna alltid skickas från Home Assistant. Token ger bara åtkomst till Home Assistant-gränssnittet."
          >
            <Tokens />
          </Section>

          {mode === "pull" ? (
            <Section
              step={2}
              done={hb.fresh && !pullDisconnected}
              title="Lägg till integrationen i Home Assistant"
              description="Installera SMGD DriveLog (via HACS eller manuellt) under Inställningar → Enheter och tjänster, och ange adressen nedan tillsammans med din token. Status blir Ansluten vid första livstecknet."
            >
              {pullDisconnected ? (
                <Note icon={<Unplug aria-hidden />} title="Bortkopplat i DriveLog" role="status">
                  <p>Integrationen i Home Assistant hör fortfarande av sig, men inga frågor skickas förrän du ansluter igen.</p>
                  <div>
                    <Button size="sm" onClick={reconnect} loading={configure.isPending} data-testid="ha-reconnect">
                      <PlugZap aria-hidden /> Anslut igen
                    </Button>
                  </div>
                </Note>
              ) : null}
              {origin ? <CopyField label="Adress till DriveLog" value={origin} /> : null}
              <div className="rounded-lg border border-border bg-surface-2/50 p-3">
                <Facts>
                  <Fact label="Livstecken">
                    {hb.received_at ? (
                      <span className="inline-flex flex-wrap items-center gap-2">
                        <TimeAgo value={hb.received_at} />
                        <Badge tone={hb.fresh ? "ok" : "neutral"} size="sm">
                          {hb.fresh ? "Aktivt" : "Tyst i över 10 min"}
                        </Badge>
                      </span>
                    ) : (
                      <span className="text-fg-muted">Inget ännu – väntar på första livstecknet från Home Assistant.</span>
                    )}
                  </Fact>
                  {hb.received_at ? (
                    <>
                      <Fact label="Home Assistant">{hb.ha_version ?? "–"}</Fact>
                      <Fact label="Integration">{hb.integration_version ?? "–"}</Fact>
                      {hb.queue_length != null ? <Fact label="Svar i kö">{plural(hb.queue_length, "svar", "svar")}</Fact> : null}
                    </>
                  ) : null}
                </Facts>
              </div>
            </Section>
          ) : (
            <Section step={2} done={!!status.base_url && status.has_token} title="Adress och token till Home Assistant">
              <PushForm status={status} />
              {hb.received_at ? (
                <p className="text-xs text-fg-muted">
                  Livstecken från integrationen: <TimeAgo value={hb.received_at} />
                  {hb.ha_version ? ` · Home Assistant ${hb.ha_version}` : ""}.
                </p>
              ) : null}
            </Section>
          )}

          <Section
            step={3}
            done={connected && !!confirmed}
            title="Testa och välj enhet"
            description="Kontrollera anslutningen, välj vilken telefon som ska få frågorna och skicka en testnotis. Tryck på Fungerar i notisen för att bekräfta att även svaren når fram."
          >
            <DevicePicker status={status} />
            <div className="flex flex-wrap gap-2">
              <Button onClick={runTest} loading={test.isPending} disabled={status.status === "not_configured"} data-testid="ha-test">
                <PlugZap aria-hidden /> Testa anslutning
              </Button>
              <Button onClick={testNotification.run} loading={testNotification.isPending} disabled={!status.channel} data-testid="ha-test-notification">
                <BellRing aria-hidden /> Skicka testnotis
              </Button>
            </div>
            {status.status === "not_configured" ? (
              <p className="text-xs text-fg-muted">
                {pullDisconnected
                  ? "Anslut igen i steg 2 för att kunna testa."
                  : mode === "pull"
                    ? "Väntar på första livstecknet från Home Assistant – testet fungerar när integrationen har hört av sig."
                    : "Spara adress och token först."}
              </p>
            ) : !status.channel ? (
              <p className="text-xs text-fg-muted">Välj en mobil enhet för att kunna skicka en testnotis.</p>
            ) : null}
            {testResult ? (
              <Note tone={testResult.ok ? "ok" : "err"} icon={testResult.ok ? <CheckCircle2 aria-hidden /> : <AlertTriangle aria-hidden />} role="status">
                <p>{testResult.message}</p>
                {testResult.ok && (testResult.ha_version || testResult.location_name) ? (
                  <p className="text-xs opacity-80">{[testResult.location_name, testResult.ha_version ? `Home Assistant ${testResult.ha_version}` : null].filter(Boolean).join(" · ")}</p>
                ) : null}
              </Note>
            ) : null}
            {confirmed ? (
              <p className="flex items-center gap-1.5 text-[13px] text-ok-ink">
                <CheckCircle2 className="size-4 shrink-0" aria-hidden />
                <span>
                  Testnotisen bekräftades <TimeAgo value={confirmed} /> – hela kedjan fungerar.
                </span>
              </p>
            ) : awaiting ? (
              <p className="text-[13px] text-fg-muted">
                Testnotis skickad <TimeAgo value={status.last_test_sent_at} /> – väntar på att du trycker Fungerar i notisen.
              </p>
            ) : null}
          </Section>
        </>
      ) : null}

      {isDemo || !hasRealConnection ? (
        <DemoBlock
          enabled={isDemo}
          pending={demo.isPending}
          description={
            isDemo
              ? "Notiserna visas på en simulerad telefon i stället för att skickas. Inga anrop görs mot någon riktig Home Assistant, och integrationen visas aldrig som ansluten i demoläget."
              : "Prova frågorna och svarsknapparna utan Home Assistant och utan iPhone. Notiserna visas på en simulerad telefon här i webbappen."
          }
          onToggle={(enable) =>
            demo.mutate(enable, {
              onSuccess: () => {
                setTestResult(null);
                toast.success(enable ? "Demoläget är på – notiserna visas på den simulerade telefonen." : "Demoläget är avstängt.");
              },
              onError: notifyError,
            })
          }
        >
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={testNotification.run} loading={testNotification.isPending} data-testid="demo-test-notification">
              <BellRing aria-hidden /> Skicka testnotis
            </Button>
            {volvo.data?.demo.enabled ? (
              <Button
                size="sm"
                loading={simulate.isPending}
                data-testid="demo-phone-simulate"
                onClick={() =>
                  simulate.mutate(null, {
                    onSuccess: (r) => toast.success("Simulerad körning klar.", { description: `${r.description}. Frågan visas på telefonen om resan behöver ett svar.` }),
                    onError: notifyError,
                  })
                }
              >
                <Play aria-hidden /> Simulera en resa
              </Button>
            ) : null}
          </div>
          {!volvo.data?.demo.enabled ? (
            <p className="text-xs leading-relaxed text-fg-muted">
              Vill du få frågor om resor? Slå på demoläget under Volvo Cars och simulera en resa – demoresorna hamnar på ett separat demofordon.
            </p>
          ) : (
            <DemoVehicleSwitch />
          )}
          <DemoPhone />
        </DemoBlock>
      ) : null}

      {DEMO_MODE && !isDemo && connected ? (
        <Section title="Telefonen (demo)">
          <p className="text-[13px] text-fg-muted">
            Så här ser notiserna ut i telefonen. Tryck på en knapp för att svara, eller på rubriken för att öppna frågan.
          </p>
          <DemoPhone />
        </Section>
      ) : null}

      {!isDemo && status.status !== "not_configured" ? (
        <Section title="Koppla bort">
          <ConfirmButton
            icon={<Unplug aria-hidden />}
            label="Koppla bort Home Assistant"
            confirmLabel="Ja, koppla bort"
            description="Adress, token och vald enhet raderas, och obesvarade frågor avbryts. API-tokens ovan återkallas separat."
            loading={disconnect.isPending}
            onConfirm={() =>
              disconnect.mutate(undefined, {
                onSuccess: () => {
                  setTestResult(null);
                  toast.success("Home Assistant är bortkopplat.");
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
