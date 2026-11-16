"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, Download, ExternalLink, HelpCircle, Info, KeyRound, LogIn, Minus, Play, RefreshCw, Unplug, X } from "lucide-react";
import { toast } from "sonner";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage } from "@/lib/api";
import { formatDate, plural } from "@/lib/format";
import type { CapabilityState, VolvoEndpoint, VolvoLinkedVehicle, VolvoStatus } from "@/lib/integrations-types";
import {
  useDownloadVolvoDiagnostics,
  useRefreshVolvoVehicles,
  useSaveVolvoCredentials,
  useSelectVolvoVehicle,
  useVolvoAccountVehicles,
  useVolvoConnect,
  useVolvoDemo,
  useVolvoDisconnect,
  useVolvoSimulate,
  useVolvoSync,
  useVolvoTestToken,
  VOLVO_DIAGNOSTICS_DAYS,
} from "@/lib/queries/integrations";
import { useSaveSettings, useSettingsPage } from "@/lib/queries/settings-page";
import { useVehicle } from "@/lib/vehicle";
import { ConfirmButton, CopyField, DemoBlock, DemoVehicleSwitch, Field, Note, notifyError, Section, SecretField, StatusNote, TimeAgo } from "./shared";

// ----------------------------------------------------------------------------- synk

/** "Synka nu" med kvitto – används både på kortet och i panelen. */
export function useVolvoSyncAction() {
  const sync = useVolvoSync();
  return {
    isPending: sync.isPending,
    run: () =>
      sync.mutate(undefined, {
        onSuccess: (r) => {
          if (r.result.status === "throttled" || r.result.status === "error" || r.result.status === "needs_reauth") {
            toast.error(r.result.message ?? "Synken kunde inte genomföras.");
            return;
          }
          const n = r.result.new_trips ?? 0;
          toast.success(n > 0 ? `Synkad – ${plural(n, "ny resa", "nya resor")}.` : "Synkad – inga nya resor sedan sist.");
        },
        onError: notifyError,
      }),
  };
}

// ----------------------------------------------------------------------------- kapabiliteter

const CAPABILITY: Record<CapabilityState, { label: string; tone: NonNullable<BadgeProps["tone"]>; icon: React.ComponentType<{ className?: string }> }> = {
  supported: { label: "Stöds", tone: "ok", icon: Check },
  unsupported: { label: "Stöds inte", tone: "neutral", icon: X },
  no_data: { label: "Ingen data", tone: "outline", icon: Minus },
  unknown: { label: "Okänt", tone: "outline", icon: HelpCircle },
};

/** Antal datakällor som bilen stöder, t.ex. { supported: 8, total: 9 }. */
export function capabilitySummary(vehicle: VolvoLinkedVehicle, endpoints: VolvoEndpoint[]): { supported: number; total: number } {
  return {
    supported: endpoints.filter((e) => vehicle.capabilities[e.name]?.state === "supported").length,
    total: endpoints.length,
  };
}

/** Vad just den här bilen stöder, per datakälla hos Volvo. */
function CapabilityTable({ vehicle, endpoints }: { vehicle: VolvoLinkedVehicle; endpoints: VolvoEndpoint[] }) {
  return (
    <div className="space-y-2">
      <Table wrapperClassName="rounded-lg border border-border bg-surface">
        <caption className="sr-only">Vad {vehicle.name} stöder</caption>
        <THead>
          <tr>
            <TH scope="col">Datakälla</TH>
            <TH scope="col" className="w-px text-right">
              Stöd
            </TH>
          </tr>
        </THead>
        <TBody>
          {endpoints.map((e) => {
            const cap = vehicle.capabilities[e.name];
            const state = CAPABILITY[cap?.state ?? "unknown"] ?? CAPABILITY.unknown;
            const Icon = state.icon;
            return (
              <TR key={e.name} className="hover:bg-transparent">
                <TD>
                  <span className="text-[13px] text-fg">{e.label}</span>
                  {cap?.message && cap.state !== "supported" ? <span className="mt-0.5 block text-xs text-fg-muted">{cap.message}</span> : null}
                </TD>
                <TD className="text-right">
                  <Badge tone={state.tone}>
                    <Icon aria-hidden />
                    {state.label}
                  </Badge>
                </TD>
              </TR>
            );
          })}
        </TBody>
      </Table>
      <p className="text-xs text-fg-muted">
        {vehicle.probed_at ? (
          <>
            Provat <TimeAgo value={vehicle.probed_at} />.{" "}
          </>
        ) : (
          "Inte provat ännu. "
        )}
        ”Stöds inte” betyder att bilen eller appens behörigheter saknar den uppgiften – resten fungerar ändå.
      </p>
    </div>
  );
}

// ----------------------------------------------------------------------------- steg 1: utvecklarapp

function CredentialsForm({ status, onDone }: { status: VolvoStatus; onDone: () => void }) {
  const save = useSaveVolvoCredentials();
  const [clientId, setClientId] = React.useState(status.config.client_id ?? "");
  const [secret, setSecret] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [authServer, setAuthServer] = React.useState<"new" | "legacy">(status.auth_server);
  const [error, setError] = React.useState<string | null>(null);
  const idField = React.useId();

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    save.mutate(
      { client_id: clientId.trim(), client_secret: secret.trim() || undefined, vcc_api_key: apiKey.trim() || undefined, auth_server: authServer },
      {
        onSuccess: () => {
          setSecret("");
          setApiKey("");
          toast.success("Uppgifterna är sparade och krypterade.", { description: "Fortsätt med att logga in med Volvo ID." });
          onDone();
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} className="space-y-3" noValidate>
      <CopyField
        label="Redirect-URI"
        value={status.redirect_uri}
        hint="Registrera adressen exakt så här när du publicerar appen hos Volvo – den måste stämma tecken för tecken."
      />
      <Field label="Client ID" htmlFor={idField} hint="Nya klienter börjar med vid-dc-.">
        <Input id={idField} value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="off" spellCheck={false} className="font-mono" required />
      </Field>
      <SecretField label="Client secret" saved={!!status.has_secrets.client_secret} value={secret} onChange={setSecret} required />
      <SecretField
        label="VCC API-nyckel"
        saved={!!status.has_secrets.vcc_api_key}
        value={apiKey}
        onChange={setApiKey}
        hint="Primär nyckel för appen i utvecklarportalen."
        required
      />
      <details className="group text-[13px]">
        <summary className="cursor-pointer rounded-sm text-fg-muted select-none hover:text-fg focus-visible:outline-2 focus-visible:outline-ring">Avancerat: inloggningsserver</summary>
        <div className="mt-2 space-y-1.5">
          <SegmentedControl
            ariaLabel="Inloggningsserver"
            value={authServer}
            onChange={setAuthServer}
            options={[
              { value: "new", label: "Ny (standard)" },
              { value: "legacy", label: "Äldre" },
            ]}
          />
          <FieldHint>Den äldre Volvo ID-servern slutar fungera 2027-01-31. Använd den nya om du inte har en gammal klient.</FieldHint>
        </div>
      </details>
      <FieldError>{error}</FieldError>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" loading={save.isPending} disabled={!clientId.trim()}>
          Spara uppgifter
        </Button>
        {status.config.client_id ? (
          <Button variant="ghost" onClick={onDone} disabled={save.isPending}>
            Avbryt
          </Button>
        ) : null}
      </div>
    </form>
  );
}

function TestTokenForm({ status }: { status: VolvoStatus }) {
  const test = useVolvoTestToken();
  const [token, setToken] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const id = React.useId();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    test.mutate(
      { access_token: token.trim(), vcc_api_key: apiKey.trim() || undefined },
      {
        onSuccess: () => {
          setToken("");
          setApiKey("");
          toast.success("Testtoken fungerar – kontot är anslutet.", { description: "Välj vilken bil som ska följas." });
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };
  return (
    <details className="text-[13px]">
      <summary className="cursor-pointer rounded-sm text-fg-muted select-none hover:text-fg focus-visible:outline-2 focus-visible:outline-ring">Prova med en testtoken i stället</summary>
      <form onSubmit={submit} className="mt-3 space-y-3" noValidate>
        <p className="leading-relaxed text-fg-muted">
          I utvecklarportalen kan du skapa en kortlivad testtoken och prova utan att publicera en app. Den förnyas inte automatiskt – för löpande insamling behöver du
          ansluta med Volvo ID.
        </p>
        <Field label="Testtoken" htmlFor={id}>
          <Textarea id={id} value={token} onChange={(e) => setToken(e.target.value)} rows={3} spellCheck={false} autoComplete="off" className="font-mono text-xs" />
        </Field>
        <SecretField label="VCC API-nyckel" saved={!!status.has_secrets.vcc_api_key} value={apiKey} onChange={setApiKey} />
        <FieldError>{error}</FieldError>
        <Button type="submit" loading={test.isPending} disabled={token.trim().length < 20}>
          <KeyRound aria-hidden /> Anslut med testtoken
        </Button>
      </form>
    </details>
  );
}

// ----------------------------------------------------------------------------- steg 3: välj bil

function VehiclePicker({ status }: { status: VolvoStatus }) {
  const list = useVolvoAccountVehicles(true);
  const refresh = useRefreshVolvoVehicles();
  const select = useSelectVolvoVehicle();
  const { vehicles: allVehicles } = useVehicle();
  const targets = allVehicles.filter((v) => !v.is_demo);
  const [pickedRef, setPickedRef] = React.useState<string | null>(null);
  const [pickedTarget, setPickedTarget] = React.useState<string | undefined>(status.config.vehicle_id);

  if (list.isLoading) return <SkeletonBlock rows={2} />;
  if (list.isError) return <ErrorState compact error={list.error} onRetry={() => void list.refetch()} title="Fordonslistan kunde inte hämtas" />;

  const vehicles = list.data?.vehicles ?? [];
  const linkedRef = list.data?.selected_ref ?? null;
  const ref = pickedRef ?? linkedRef ?? (vehicles.length === 1 ? vehicles[0].ref : null);
  const targetId = targets.some((t) => t.id === pickedTarget) ? pickedTarget : (targets.find((v) => v.is_default) ?? targets[0])?.id;
  const unchanged = ref === linkedRef && targetId === status.config.vehicle_id;

  const onRefresh = () =>
    refresh.mutate(undefined, {
      onSuccess: (r) => toast.success(r.vehicles.length ? `Fordonslistan är uppdaterad – ${plural(r.vehicles.length, "bil", "bilar")}.` : "Inga bilar hittades på Volvo-kontot."),
      onError: notifyError,
    });

  if (vehicles.length === 0) {
    return (
      <div className="space-y-2">
        <p className="text-[13px] leading-relaxed text-fg-muted">Inga bilar har hämtats från Volvo-kontot ännu. Bilen måste vara kopplad till samma Volvo ID som i Volvo Cars-appen.</p>
        <Button size="sm" onClick={onRefresh} loading={refresh.isPending}>
          <RefreshCw aria-hidden /> Hämta fordonslistan
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <fieldset className="space-y-2">
        <legend className="sr-only">Bil på Volvo-kontot</legend>
        {vehicles.map((v) => (
          <label
            key={v.ref}
            className="flex cursor-pointer items-center gap-3 rounded-lg border border-border bg-surface p-3 transition-colors hover:border-border-strong has-[:checked]:border-accent has-[:checked]:bg-accent-soft/50 has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-ring"
          >
            <input type="radio" name="volvo-vehicle" value={v.ref} checked={ref === v.ref} onChange={() => setPickedRef(v.ref)} className="size-4 accent-accent" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">{[v.model, v.model_year].filter(Boolean).join(" ") || "Volvo"}</span>
              <span className="block truncate font-mono text-xs text-fg-muted">{[v.license_plate, v.vin_masked].filter(Boolean).join(" · ")}</span>
            </span>
            {linkedRef === v.ref ? (
              <Badge tone="ok" size="sm">
                Kopplad
              </Badge>
            ) : null}
          </label>
        ))}
      </fieldset>
      {targets.length > 1 ? (
        <div className="space-y-1.5">
          <Label id="volvo-target-label">Fordon i DriveLog</Label>
          <Select value={targetId} onValueChange={setPickedTarget}>
            <SelectTrigger aria-labelledby="volvo-target-label">
              <SelectValue placeholder="Välj fordon" />
            </SelectTrigger>
            <SelectContent>
              {targets.map((t) => (
                <SelectItem key={t.id} value={t.id}>
                  {t.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant={linkedRef ? "secondary" : "primary"}
          disabled={!ref || unchanged}
          loading={select.isPending}
          onClick={() =>
            ref &&
            select.mutate(
              { ref, vehicle_id: targetId },
              {
                onSuccess: (r) =>
                  r.probe_error
                    ? toast.warning("Bilen är kopplad, men stödet kunde inte provas.", { description: r.probe_error })
                    : toast.success("Bilen är kopplad.", { description: "Tabellen visar vad just den här bilen stöder." }),
                onError: notifyError,
              },
            )
          }
        >
          {linkedRef && unchanged ? "Bilen är kopplad" : targets.length === 1 ? `Koppla till ${targets[0].name}` : "Koppla bilen"}
        </Button>
        <Button variant="ghost" size="sm" onClick={onRefresh} loading={refresh.isPending}>
          <RefreshCw aria-hidden /> Hämta listan igen
        </Button>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------- demoläge

const SCENARIO_LABELS: Record<string, string> = {
  commute: "Pendling mellan hemmet och kontoret",
  short_stop: "Kort stopp på vägen",
  customer_visit: "Kundbesök med längre stopp",
  silent: "Okänd starttid",
  round_trip: "Rundresa utan ny position",
};

function VolvoDemo({ status }: { status: VolvoStatus }) {
  const demo = useVolvoDemo();
  const simulate = useVolvoSimulate();
  const router = useRouter();
  const { setVehicleId } = useVehicle();
  const [scenario, setScenario] = React.useState("auto");
  const chosen = status.demo.scenarios.find((s) => s.key === scenario);

  return (
    <DemoBlock
      enabled={status.demo.enabled}
      pending={demo.isPending}
      description={
        status.demo.enabled
          ? "Allt här är påhittat. Demoläget använder ett eget demofordon – din riktiga körjournal påverkas inte, och integrationen visas aldrig som ansluten på grund av demodata."
          : "Se hur insamlingen fungerar utan Volvo-konto. Demoläget skapar ett separat demofordon med påhittade data – din riktiga körjournal påverkas inte."
      }
      onToggle={(enable) =>
        demo.mutate(enable, {
          onSuccess: () =>
            toast.success(enable ? "Demoläget är på – ett separat demofordon har skapats." : "Demoläget är avstängt och all demodata är borttagen."),
          onError: notifyError,
        })
      }
    >
      <DemoVehicleSwitch />
      <div className="space-y-1.5">
        <Label id="volvo-scenario-label">Simulera en körning</Label>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Select value={scenario} onValueChange={setScenario}>
            <SelectTrigger aria-labelledby="volvo-scenario-label" className="sm:flex-1">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">Nästa scenario i tur</SelectItem>
              {status.demo.scenarios.map((s) => (
                <SelectItem key={s.key} value={s.key}>
                  {SCENARIO_LABELS[s.key] ?? s.description}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant="primary"
            loading={simulate.isPending}
            data-testid="demo-simulate"
            onClick={() =>
              simulate.mutate(scenario === "auto" ? null : scenario, {
                onSuccess: (r) => {
                  const n = r.new_trip_ids.length;
                  toast.success(n > 0 ? `Simulerad körning – ${plural(n, "ny resa", "nya resor")} i demobilens körjournal.` : "Simulerad körning – ingen ny resa skapades.", {
                    description: r.description,
                    action: {
                      label: "Visa",
                      onClick: () => {
                        setVehicleId(r.vehicle_id);
                        router.push("/journal");
                      },
                    },
                  });
                },
                onError: notifyError,
              })
            }
          >
            <Play aria-hidden /> Simulera en resa
          </Button>
        </div>
        <FieldHint>{chosen ? `${chosen.description}.` : "Scenarierna roteras: pendling, kort stopp, kundbesök, okänd starttid och rundresa."}</FieldHint>
      </div>
      {status.demo.vehicle ? (
        <details className="text-[13px]">
          <summary className="cursor-pointer rounded-sm text-fg-muted select-none hover:text-fg focus-visible:outline-2 focus-visible:outline-ring">Vad demobilen stöder</summary>
          <div className="mt-3">
            <CapabilityTable vehicle={status.demo.vehicle} endpoints={status.endpoints} />
          </div>
        </details>
      ) : null}
    </DemoBlock>
  );
}

// ----------------------------------------------------------------------------- insamlingsintervall

/** Gränser enligt backend/app/services/settings.py (volvo.poll_interval_seconds, min_poll_interval_seconds). */
const POLL_MIN_MINUTES = 2;
const POLL_MAX_MINUTES = 24 * 60;
const POLL_DEFAULT_SECONDS = 300;

/** "Hämta data var … minut" – sparas i volvo.poll_interval_seconds (PATCH /settings/volvo). */
function PollInterval() {
  const settings = useSettingsPage();
  const save = useSaveSettings();
  const volvo = settings.data?.settings.volvo as Record<string, unknown> | undefined;
  const raw = volvo?.poll_interval_seconds;
  const seconds = typeof raw === "number" ? raw : POLL_DEFAULT_SECONDS;
  const floor = typeof volvo?.min_poll_interval_seconds === "number" ? volvo.min_poll_interval_seconds : 0;
  const current = Math.round(Math.max(seconds, floor) / 60);
  const [draft, setDraft] = React.useState<string | null>(null);
  const value = draft ?? String(current);
  const minutes = Number(value.replace(",", "."));
  const invalid = !/^\d+$/.test(value.trim()) || minutes < POLL_MIN_MINUTES || minutes > POLL_MAX_MINUTES;
  const dirty = draft !== null && !invalid && minutes !== current;

  if (!settings.data) return null;

  // Kvitto utan Ångra: panelen är modal, så en knapp i toasten går inte att nå. Fältet visar det sparade värdet.
  const store = (next: number) =>
    save.mutate(
      { volvo: { poll_interval_seconds: next * 60 } },
      {
        onSuccess: () => {
          setDraft(null);
          toast.success(`DriveLog hämtar data från bilen var ${plural(next, "minut", "minuter")}.`);
        },
        onError: notifyError,
      },
    );

  return (
    <form
      className="flex flex-wrap items-end gap-x-3 gap-y-2 border-t border-border pt-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (dirty) store(minutes);
      }}
    >
      <div className="space-y-1.5">
        <Label htmlFor="volvo-poll">Hämta data var</Label>
        <div className="flex items-center gap-2">
          <Input
            id="volvo-poll"
            value={value}
            onChange={(e) => setDraft(e.target.value)}
            inputMode="numeric"
            className="w-20 text-right"
            aria-invalid={invalid || undefined}
            aria-describedby="volvo-poll-hint"
            autoComplete="off"
          />
          <span className="text-sm text-fg-muted">{minutes === 1 ? "minut" : "minuter"}</span>
        </div>
      </div>
      {dirty ? (
        <Button type="submit" size="sm" variant="secondary" loading={save.isPending}>
          Spara
        </Button>
      ) : null}
      {invalid ? (
        <FieldError className="basis-full">{`Ange ett heltal mellan ${POLL_MIN_MINUTES} och ${POLL_MAX_MINUTES} minuter.`}</FieldError>
      ) : (
        <FieldHint id="volvo-poll-hint" className="basis-full">
          {`Hur ofta bilens mätarställning och position hämtas. Kortast ${POLL_MIN_MINUTES} minuter, standard ${POLL_DEFAULT_SECONDS / 60}.`}
        </FieldHint>
      )}
    </form>
  );
}

// ----------------------------------------------------------------------------- felsökningsfil

/** "Ladda ner felsökningsfil" (1.3.0, GET /volvo/diagnostics) – en fil ägaren kan skicka vidare. */
function DiagnosticsDownload() {
  const download = useDownloadVolvoDiagnostics();
  return (
    <Section
      title="Felsökning"
      description={`Bilens avläsningar och hämtningar de senaste ${VOLVO_DIAGNOSTICS_DAYS} dygnen, utan chassinummer, registreringsnummer, positioner och adresser. Skicka filen till den som hjälper dig felsöka.`}
    >
      <Button
        size="sm"
        variant="secondary"
        loading={download.isPending}
        data-testid="volvo-diagnostics"
        onClick={() =>
          download.mutate(VOLVO_DIAGNOSTICS_DAYS, {
            onSuccess: () => toast.success("Felsökningsfilen är nedladdad."),
            onError: notifyError,
          })
        }
      >
        <Download aria-hidden /> Ladda ner felsökningsfil
      </Button>
    </Section>
  );
}

// ----------------------------------------------------------------------------- panel

export function VolvoPanel({ status }: { status: VolvoStatus }) {
  const connect = useVolvoConnect();
  const disconnect = useVolvoDisconnect();
  const sync = useVolvoSyncAction();
  const [redirecting, setRedirecting] = React.useState(false);

  const hasApp = !!status.config.client_id && !!status.has_secrets.client_secret && !!status.has_secrets.vcc_api_key;
  const [editing, setEditing] = React.useState(!hasApp);
  const connected = status.status === "connected";
  // "error" = anslutningen finns men de senaste anropen har misslyckats – fordon kan fortfarande väljas.
  const hasAccount = connected || status.status === "error";
  const linked = status.vehicles[0] ?? null;
  const hasAnything = status.status !== "not_configured" && status.status !== "demo";

  const startConnect = () =>
    connect.mutate(undefined, {
      onSuccess: ({ authorize_url }) => {
        setRedirecting(true);
        window.location.assign(authorize_url);
      },
      onError: notifyError,
    });

  return (
    <>
      <StatusNote
        status={status.status}
        error={status.last_error}
        at={status.last_error_at}
        reauthTitle="Logga in med Volvo ID igen"
        reauthHint="Behörigheten har gått ut eller återkallats (den gäller högst cirka tre månader). Inga nya data hämtas förrän du ansluter på nytt."
      />
      {status.test_token_notice ? (
        <Note tone="warn" icon={<Info aria-hidden />} role="status">
          <p>{status.test_token_notice}</p>
        </Note>
      ) : null}

      <Section
        step={1}
        done={hasApp}
        title="Utvecklarapp hos Volvo"
        description={
          editing ? (
            <>
              Skapa och publicera en egen applikation på{" "}
              <a href="https://developer.volvocars.com" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-2 hover:underline">
                developer.volvocars.com <ExternalLink className="size-3" aria-hidden />
              </a>{" "}
              och fyll i uppgifterna här. Hemligheterna krypteras och visas aldrig igen.
            </>
          ) : undefined
        }
        action={
          hasApp && !editing ? (
            <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
              Ändra
            </Button>
          ) : null
        }
      >
        {editing ? (
          <CredentialsForm status={status} onDone={() => setEditing(false)} />
        ) : (
          <p className="text-[13px] text-fg-muted">
            Client ID <span className="font-mono text-fg">{status.config.client_id}</span> · client secret och VCC API-nyckel är sparade.
          </p>
        )}
      </Section>

      <Section
        step={2}
        done={connected}
        title="Logga in med Volvo ID"
        description={
          connected
            ? status.reauth_due_at
              ? `Kontot är anslutet. Volvo kräver ny inloggning ungefär var tredje månad – senast ${formatDate(status.reauth_due_at)}.`
              : "Kontot är anslutet."
            : "Du skickas till Volvo för att logga in och godkänna att DriveLog får läsa bilens data. Endast läsbehörigheter begärs."
        }
      >
        <div className="flex flex-wrap items-center gap-2">
          <Button variant={connected ? "secondary" : "primary"} onClick={startConnect} loading={connect.isPending || redirecting} disabled={!hasApp} data-testid="volvo-connect">
            <LogIn aria-hidden /> {connected || status.status === "needs_reauth" ? "Anslut Volvo ID på nytt" : "Anslut Volvo ID"}
          </Button>
          {!hasApp ? <span className="text-xs text-fg-muted">Spara uppgifterna i steg 1 först.</span> : null}
        </div>
        {!connected ? <TestTokenForm status={status} /> : null}
      </Section>

      <Section
        step={3}
        done={!!linked}
        title="Välj bil"
        description={hasAccount ? "Välj vilken bil på Volvo-kontot som ska följas. VIN visas maskerat." : "Bilarna på kontot visas här när du har loggat in med Volvo ID."}
      >
        {hasAccount ? <VehiclePicker status={status} /> : null}
      </Section>

      {linked ? (
        <Section
          title={`Det här stöder ${linked.name}`}
          description={
            <>
              Senaste data från bilen: <TimeAgo value={linked.last_observation_at} empty="inga ännu" />
              {status.stale ? " – bilen har inte rapporterat något nytt på ett tag, vilket är normalt när den står still." : "."}
            </>
          }
          action={
            <Button size="sm" onClick={sync.run} loading={sync.isPending} disabled={!hasAccount}>
              <RefreshCw aria-hidden /> Synka nu
            </Button>
          }
        >
          <CapabilityTable vehicle={linked} endpoints={status.endpoints} />
          <PollInterval />
        </Section>
      ) : null}

      <Section title="Bra att veta">
        <Note icon={<Info aria-hidden />}>
          <p>
            Volvo rapporterar bilens position först när en körning har avslutats, och API:t har ingen resejournal. DriveLog rekonstruerar därför resorna ur mätarställning och
            position – starttiden blir ibland ungefärlig.
          </p>
          <p>
            Saknas en tjänsteresa kan du komplettera med Volvo-appens export under{" "}
            <Link href="/import" className="font-medium text-accent-ink underline-offset-2 hover:underline">
              Import
            </Link>
            .
          </p>
        </Note>
      </Section>

      {linked || status.demo.enabled ? <DiagnosticsDownload /> : null}

      <VolvoDemo status={status} />

      {hasAnything ? (
        <Section title="Koppla bort">
          <ConfirmButton
            icon={<Unplug aria-hidden />}
            label="Koppla bort Volvo"
            confirmLabel="Ja, koppla bort"
            description="Client secret, API-nyckel och tokens raderas och måste anges igen om du ansluter på nytt. Fordon och redan insamlade resor finns kvar."
            loading={disconnect.isPending}
            onConfirm={() =>
              disconnect.mutate(undefined, {
                onSuccess: () => {
                  setEditing(true);
                  toast.success("Volvo är bortkopplat. Alla sparade nycklar är raderade.");
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
