"use client";

import * as React from "react";
import { ExternalLink, Info, LogIn, RefreshCw, Unplug } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ErrorState } from "@/components/ui/error-state";
import { FieldError, Input } from "@/components/ui/input";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDateTime, plural } from "@/lib/format";
import type { MicrosoftStatus } from "@/lib/integrations-types";
import {
  useMicrosoftCalendars,
  useMicrosoftConnect,
  useMicrosoftDemo,
  useMicrosoftDisconnect,
  useMicrosoftSync,
  useSaveMicrosoftCredentials,
  useSelectMicrosoftCalendars,
} from "@/lib/queries/integrations";
import { ConfirmButton, CopyField, DemoBlock, DemoVehicleSwitch, Field, Note, notifyError, Section, SecretField, StatusNote } from "./shared";

/** "Synka nu" med kvitto – används både på kortet och i panelen. */
export function useMicrosoftSyncAction() {
  const sync = useMicrosoftSync();
  return {
    isPending: sync.isPending,
    run: () =>
      sync.mutate(undefined, {
        onSuccess: (r) => {
          if (r.status === "throttled") {
            toast.warning("Microsoft begränsar anropen just nu.", { description: r.retry_at ? `Nästa försök tidigast ${formatDateTime(r.retry_at)}.` : undefined });
            return;
          }
          const changed = (r.created ?? 0) + (r.updated ?? 0) + (r.removed ?? 0);
          toast.success(changed > 0 ? `Kalendern är synkad – ${plural(changed, "ändrad händelse", "ändrade händelser")}.` : "Kalendern är synkad – inget nytt.", {
            description: r.trips_with_match ? `${plural(r.trips_with_match, "resa har", "resor har")} en kalenderträff.` : undefined,
          });
        },
        onError: notifyError,
      }),
  };
}

function CredentialsForm({ status, onDone }: { status: MicrosoftStatus; onDone: () => void }) {
  const save = useSaveMicrosoftCredentials();
  const [clientId, setClientId] = React.useState(status.config.client_id ?? "");
  const [secret, setSecret] = React.useState("");
  const [tenant, setTenant] = React.useState(status.config.tenant ?? "common");
  const [error, setError] = React.useState<string | null>(null);
  const idField = React.useId();
  const tenantField = React.useId();

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    save.mutate(
      { client_id: clientId.trim(), client_secret: secret.trim() || undefined, tenant: tenant.trim() || "common" },
      {
        onSuccess: () => {
          setSecret("");
          toast.success("Uppgifterna är sparade och krypterade.", { description: "Fortsätt med att ansluta Microsoft-kontot." });
          onDone();
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} className="space-y-3" noValidate>
      <CopyField label="Redirect-URI" value={status.redirect_uri} hint="Lägg in adressen som Redirect URI (plattform Web) i appregistreringen." />
      <Field label="Applikations-ID (client ID)" htmlFor={idField}>
        <Input id={idField} value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="off" spellCheck={false} placeholder="00000000-0000-0000-0000-000000000000" className="font-mono" required />
      </Field>
      <SecretField label="Klienthemlighet" saved={!!status.has_secrets.client_secret} value={secret} onChange={setSecret} hint="Värdet (Value) under Certificates & secrets – inte Secret ID." required />
      <Field label="Tenant" htmlFor={tenantField} hint="Lämna som common för både privata konton och arbetskonton, eller ange organisationens tenant-id.">
        <Input id={tenantField} value={tenant} onChange={(e) => setTenant(e.target.value)} autoComplete="off" spellCheck={false} className="font-mono" />
      </Field>
      <FieldError>{error}</FieldError>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" loading={save.isPending} disabled={clientId.trim().length < 8}>
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

function CalendarPicker() {
  const calendars = useMicrosoftCalendars(true);
  const save = useSelectMicrosoftCalendars();
  // Lokalt val så att kryssrutorna svarar direkt; servern är sanningen efter sparning.
  const [local, setLocal] = React.useState<Set<string> | null>(null);

  if (calendars.isLoading) return <SkeletonBlock rows={3} />;
  if (calendars.isError) return <ErrorState compact error={calendars.error} onRetry={() => void calendars.refetch()} title="Kalendrarna kunde inte hämtas" />;
  const list = calendars.data ?? [];
  if (list.length === 0) return <p className="text-[13px] text-fg-muted">Inga kalendrar hittades på kontot.</p>;

  const selected = local ?? new Set(list.filter((c) => c.selected).map((c) => c.id));
  const toggle = (id: string, checked: boolean) => {
    const next = new Set(selected);
    if (checked) next.add(id);
    else next.delete(id);
    setLocal(next);
    save.mutate([...next], {
      onSuccess: () => toast.success(next.size ? `${plural(next.size, "kalender används", "kalendrar används")}.` : "Ingen kalender är vald – standardkalendern används."),
      onError: (err) => {
        setLocal(null);
        notifyError(err);
      },
    });
  };

  return (
    <ul className="divide-y divide-border rounded-lg border border-border">
      {list.map((c) => {
        const id = `ms-cal-${c.id}`;
        return (
          <li key={c.id} className="flex items-center gap-3 px-3 py-2.5">
            <Checkbox id={id} checked={selected.has(c.id)} onCheckedChange={(v) => toggle(c.id, v === true)} />
            <label htmlFor={id} className="min-w-0 flex-1 cursor-pointer">
              <span className="block truncate text-sm">{c.name}</span>
              {c.owner ? <span className="block truncate text-xs text-fg-muted">{c.owner}</span> : null}
            </label>
            {c.is_default ? (
              <Badge tone="outline" size="sm">
                Standard
              </Badge>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export function OutlookPanel({ status }: { status: MicrosoftStatus }) {
  const connect = useMicrosoftConnect();
  const disconnect = useMicrosoftDisconnect();
  const demo = useMicrosoftDemo();
  const sync = useMicrosoftSyncAction();
  const [redirecting, setRedirecting] = React.useState(false);

  const hasApp = !!status.config.client_id && !!status.has_secrets.client_secret;
  const [editing, setEditing] = React.useState(!hasApp);
  const connected = status.status === "connected";
  const hasAccount = connected || status.status === "error";
  const isDemo = status.status === "demo";
  // Bortkoppling raderar tokens och kalenderhändelser; appens uppgifter behålls av backend.
  const canDisconnect = hasAccount || status.status === "needs_reauth";

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
        reauthTitle="Anslut Microsoft-kontot igen"
        reauthHint="Åtkomsten har gått ut eller återkallats. Kalendern synkas inte förrän du ansluter på nytt."
      />

      <Section
        step={1}
        done={hasApp}
        title="Appregistrering hos Microsoft"
        description={
          editing ? (
            <>
              Registrera en app i{" "}
              <a href="https://entra.microsoft.com" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-2 hover:underline">
                Microsoft Entra <ExternalLink className="size-3" aria-hidden />
              </a>{" "}
              med de delegerade behörigheterna {status.required_permissions.join(" och ")}. Hemligheten krypteras och visas aldrig igen.
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
            Applikations-ID <span className="font-mono break-all text-fg">{status.config.client_id}</span> · klienthemligheten är sparad.
          </p>
        )}
      </Section>

      <Section
        step={2}
        done={connected}
        title="Anslut Microsoft-kontot"
        description={
          connected
            ? status.config.account
              ? `Anslutet som ${status.config.account}.`
              : "Kontot är anslutet."
            : "Du skickas till Microsoft för att logga in och godkänna att DriveLog får läsa kalendern. Inget skrivs i kalendern."
        }
      >
        <div className="flex flex-wrap items-center gap-2">
          <Button variant={connected ? "secondary" : "primary"} onClick={startConnect} loading={connect.isPending || redirecting} disabled={!hasApp} data-testid="microsoft-connect">
            <LogIn aria-hidden /> {connected || status.status === "needs_reauth" ? "Anslut på nytt" : "Anslut Outlook"}
          </Button>
          {!hasApp ? <span className="text-xs text-fg-muted">Spara uppgifterna i steg 1 först.</span> : null}
        </div>
      </Section>

      <Section
        step={3}
        done={hasAccount && status.selected_calendar_ids.length > 0}
        title="Välj kalendrar"
        description={
          hasAccount
            ? "Välj vilka kalendrar som får ge resorna sammanhang. Väljer du ingen används standardkalendern."
            : "Kalendrarna visas här när kontot är anslutet."
        }
        action={
          hasAccount ? (
            <Button size="sm" onClick={sync.run} loading={sync.isPending}>
              <RefreshCw aria-hidden /> Synka nu
            </Button>
          ) : null
        }
      >
        {hasAccount ? (
          <>
            <CalendarPicker />
            <p className="text-xs text-fg-muted">{plural(status.stored_events, "kalenderhändelse är sparad", "kalenderhändelser är sparade")} i DriveLog.</p>
          </>
        ) : null}
      </Section>

      <Section title="Bra att veta">
        <Note icon={<Info aria-hidden />}>
          <p>
            Ett möte nära resans ankomst, på en plats som stämmer med målet, tyder på tjänsteresa och ger ett förslag på syfte. DriveLog läser bara rubrik, tid och plats
            {status.stores_body_preview ? " samt en kort förhandsvisning av brödtexten" : ""} – kalendern är en ledtråd, aldrig ett facit.
          </p>
        </Note>
      </Section>

      <DemoBlock
        enabled={isDemo}
        pending={demo.isPending}
        description={
          isDemo
            ? "Påhittade möten har lagts in vid demobilens ankomster till kontor och kunder. Ingen riktig kalender används, och Outlook visas aldrig som anslutet på grund av demodata."
            : "Se hur kalenderträffar ser ut utan Microsoft-konto. Demoläget lägger påhittade möten på ett separat demofordon – din riktiga körjournal påverkas inte."
        }
        onToggle={(enable) =>
          demo.mutate(enable, {
            onSuccess: (r) =>
              toast.success(
                enable
                  ? r.events_created
                    ? `Demoläget är på – ${plural(r.events_created, "påhittat möte", "påhittade möten")} har lagts in.`
                    : "Demoläget är på."
                  : "Demoläget är avstängt och demomötena är borttagna.",
                { description: enable && !r.events_created ? "Slå på Volvos demoläge och simulera en resa till kontoret eller en kund, så skapas möten att matcha mot." : undefined },
              ),
            onError: notifyError,
          })
        }
      >
        <DemoVehicleSwitch />
      </DemoBlock>

      {canDisconnect ? (
        <Section title="Koppla bort">
          <ConfirmButton
            icon={<Unplug aria-hidden />}
            label="Koppla bort Outlook"
            confirmLabel="Ja, koppla bort"
            description="Tokens och alla sparade kalenderhändelser raderas, och kalenderträffarna på resorna försvinner. Resorna finns kvar, och appens uppgifter i steg 1 behålls så att du kan ansluta igen."
            loading={disconnect.isPending}
            onConfirm={() =>
              disconnect.mutate(undefined, {
                onSuccess: () => toast.success("Outlook är bortkopplat. Tokens och kalenderhändelser är raderade."),
                onError: notifyError,
              })
            }
          />
        </Section>
      ) : null}
    </>
  );
}
