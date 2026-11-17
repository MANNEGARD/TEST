"use client";

import * as React from "react";
import { BellRing, CalendarDays, Car, FlaskConical, RefreshCw, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { PageHeader } from "@/components/ui/page-header";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { SkeletonBlock } from "@/components/ui/skeleton";
import type { ProviderKey } from "@/lib/integrations-types";
import { useAiStatus, useHaStatus, useIcsStatus, useMicrosoftStatus, useVolvoStatus } from "@/lib/queries/integrations";
import { AI_PROVIDER_LABELS, AiPanel, useAiTestAction } from "./ai-panel";
import { DELIVERY_LABELS, HaPanel, useTestNotificationAction } from "./ha-panel";
import { useIcsSyncAction } from "./ics-link";
import { IntegrationCard, type IntegrationCardData } from "./integration-card";
import { OutlookPanel, useMicrosoftSyncAction } from "./outlook-panel";
import { DemoVehicleSwitch, Note, StatusBadge } from "./shared";
import { capabilitySummary, useVolvoSyncAction, VolvoPanel } from "./volvo-panel";

const PROVIDERS: Record<ProviderKey, { name: string; tagline: string; icon: React.ReactNode }> = {
  volvo: {
    name: "Volvo Cars",
    tagline: "Hämtar mätarställning och position från bilen, så att tjänsteresorna skapas av sig själva.",
    icon: <Car />,
  },
  microsoft: {
    name: "Microsoft Outlook",
    tagline: "Läser kalendern för att föreslå syfte på tjänsteresor. Inget skrivs i kalendern.",
    icon: <CalendarDays />,
  },
  home_assistant: {
    name: "Home Assistant",
    tagline: "Frågar i mobilen om en ny resa var i tjänsten – svara med ett tryck.",
    icon: <BellRing />,
  },
  ai: {
    name: "AI-leverantör",
    tagline: "Valfri hjälp med förslag på syfte. Allt fungerar utan AI – då används reglerna.",
    icon: <Sparkles />,
  },
};

// ----------------------------------------------------------------------------- återkomst från OAuth

const RETURN_REASONS: Record<string, string> = {
  access_denied: "Inloggningen avbröts eller nekades. Inget har ändrats – försök igen när du vill.",
  denied: "Inloggningen avbröts eller nekades. Inget har ändrats – försök igen när du vill.",
  consent_required: "Samtycket gavs inte. För arbetskonton kan organisationens IT behöva godkänna appen.",
  invalid_scope: "De begärda behörigheterna godkändes inte. Kontrollera att alla behörigheter är valda för appen hos leverantören.",
  invalid_state: "Inloggningen kunde inte verifieras. Starta anslutningen på nytt härifrån.",
  state: "Inloggningen kunde inte verifieras. Starta anslutningen på nytt härifrån.",
  state_expired: "Inloggningen tog för lång tid (högst 10 minuter). Försök igen.",
  missing_code: "Inget svar kom tillbaka från inloggningen. Försök igen.",
  invalid_client: "Client ID eller hemligheten godkändes inte. Kontrollera uppgifterna och att redirect-URI:n stämmer exakt.",
  invalid_credentials: "Client ID eller hemligheten godkändes inte. Kontrollera uppgifterna och att redirect-URI:n stämmer exakt.",
  token: "Koden kunde inte bytas mot en åtkomst. Kontrollera klienthemligheten och redirect-URI:n.",
  token_error: "Koden kunde inte bytas mot en åtkomst. Kontrollera client secret och redirect-URI:n.",
  network: "Inloggningstjänsten svarade inte. Försök igen om en stund.",
  server_error: "Inloggningstjänsten rapporterade ett fel. Försök igen om en stund.",
  temporarily_unavailable: "Inloggningstjänsten är tillfälligt otillgänglig. Försök igen om en stund.",
  throttled: "För många anrop just nu. Vänta en stund och försök igen.",
  not_configured: "Uppgifterna för appen saknas. Spara dem under Konfigurera och anslut igen.",
  not_logged_in: "Du var inte inloggad i DriveLog när du kom tillbaka. Anslut igen härifrån.",
  session_required: "Anslutningen måste göras från en inloggad webbläsare.",
  internal_error: "Ett oväntat fel inträffade. Försök igen – detaljer finns i serverns logg.",
};

const RETURN_PARAMS: { param: string; provider: ProviderKey; connected: string; next: string }[] = [
  { param: "volvo", provider: "volvo", connected: "Volvo-kontot är anslutet.", next: "Välj vilken bil som ska följas." },
  { param: "microsoft", provider: "microsoft", connected: "Outlook är anslutet.", next: "Välj kalendrar och synka." },
];

/**
 * Efter inloggning hos Volvo/Microsoft skickas webbläsaren tillbaka hit med
 * ?volvo=connected|error&reason=… (eller ?microsoft=…). Visa kvitto, öppna rätt
 * panel och rensa adressen så att meddelandet inte visas igen vid omladdning.
 */
function useOAuthReturn(open: (provider: ProviderKey) => void) {
  React.useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const reason = params.get("reason") ?? "";
    let handled = false;
    for (const r of RETURN_PARAMS) {
      const outcome = params.get(r.param);
      if (!outcome) continue;
      handled = true;
      const name = PROVIDERS[r.provider].name;
      if (outcome === "connected") {
        toast.success(r.connected, { description: r.next, duration: 8000 });
      } else {
        toast.error(`${name} kunde inte anslutas.`, {
          // Okända koder visas bara om de ser ut som en felkod (inte fri text ur adressfältet).
          description: RETURN_REASONS[reason] ?? (/^[A-Za-z0-9_-]{1,40}$/.test(reason) ? `Leverantören svarade: ${reason}.` : "Försök igen."),
          duration: 12000,
        });
      }
      open(r.provider);
      params.delete(r.param);
    }
    if (!handled) return;
    params.delete("reason");
    const rest = params.toString();
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
  }, [open]);
}

// ----------------------------------------------------------------------------- vy

export function IntegrationsView() {
  const volvo = useVolvoStatus();
  const microsoft = useMicrosoftStatus();
  const ics = useIcsStatus();
  const ha = useHaStatus();
  const ai = useAiStatus();
  const [open, setOpen] = React.useState<ProviderKey | null>(null);
  const lastOpened = React.useRef<ProviderKey | null>(null);
  const openPanel = React.useCallback((provider: ProviderKey) => {
    lastOpened.current = provider;
    setOpen(provider);
  }, []);
  useOAuthReturn(openPanel);

  const volvoSync = useVolvoSyncAction();
  const microsoftSync = useMicrosoftSyncAction();
  const icsSync = useIcsSyncAction();
  const testNotification = useTestNotificationAction();
  const aiTest = useAiTestAction();

  // ---- Volvo
  let volvoCard: IntegrationCardData | undefined;
  if (volvo.data) {
    const s = volvo.data;
    const vehicle = s.vehicles[0] ?? (s.status === "demo" ? s.demo.vehicle : null);
    const summary = vehicle ? capabilitySummary(vehicle, s.endpoints) : null;
    const canSync = (s.status === "connected" && s.vehicles.length > 0) || (s.status === "demo" && !!s.demo.vehicle);
    volvoCard = {
      status: s.status,
      successLabel: "Senast synkad",
      lastSuccessAt: s.last_success_at,
      lastError: s.last_error,
      lastErrorAt: s.last_error_at,
      capabilityLabel: vehicle ? "Bilen stöder" : "Behörigheter",
      capability:
        vehicle && summary ? (
          <>
            {summary.supported} av {summary.total} datakällor
            <span className="block truncate text-xs text-fg-muted">{vehicle.name}</span>
          </>
        ) : (
          <span className="text-fg-muted">Läser mätarställning, position och bränsle-/batterinivå. Styr aldrig bilen.</span>
        ),
      action: canSync ? (
        <Button variant="ghost" size="sm" onClick={volvoSync.run} loading={volvoSync.isPending}>
          <RefreshCw aria-hidden /> Synka nu
        </Button>
      ) : null,
    };
  }

  // ---- Outlook
  let microsoftCard: IntegrationCardData | undefined;
  const link = ics.data;
  if (microsoft.data && link?.has_link && (microsoft.data.status === "not_configured" || microsoft.data.status === "configured")) {
    // Ingen Graph-anslutning men en kalenderlänk: kortet visar länkens läge.
    microsoftCard = {
      status: link.status,
      successLabel: "Senast synkad",
      lastSuccessAt: link.last_success_at,
      lastError: link.last_error,
      lastErrorAt: link.last_error_at,
      capabilityLabel: "Källa",
      capability: <span className="block truncate">Kalenderlänk (ICS) · {link.host ?? "okänd värd"}</span>,
      action:
        link.status === "connected" ? (
          <Button variant="ghost" size="sm" onClick={icsSync.run} loading={icsSync.isPending}>
            <RefreshCw aria-hidden /> Synka nu
          </Button>
        ) : null,
    };
  } else if (microsoft.data) {
    const s = microsoft.data;
    const permissions = (s.scopes.length ? s.scopes.filter((p) => p.includes(".")) : s.required_permissions).filter((p) => p !== "offline_access");
    microsoftCard = {
      status: s.status,
      successLabel: "Senast synkad",
      lastSuccessAt: s.last_success_at,
      lastError: s.last_error,
      lastErrorAt: s.last_error_at,
      capabilityLabel: "Behörigheter",
      capability: (
        <>
          <span className="text-fg-muted">Endast läsning av kalendern</span>
          <span className="block truncate font-mono text-xs text-fg-muted">{permissions.join(", ") || "Calendars.Read"}</span>
        </>
      ),
      action:
        s.status === "connected" ? (
          <Button variant="ghost" size="sm" onClick={microsoftSync.run} loading={microsoftSync.isPending}>
            <RefreshCw aria-hidden /> Synka nu
          </Button>
        ) : null,
    };
  }

  // ---- Home Assistant
  let haCard: IntegrationCardData | undefined;
  if (ha.data) {
    const s = ha.data;
    haCard = {
      status: s.status,
      successLabel: "Senaste kontakt",
      lastSuccessAt: s.last_success_at,
      lastError: s.last_error,
      lastErrorAt: s.last_error_at,
      capabilityLabel: "Leverans",
      capability: s.demo ? (
        <span className="text-fg-muted">Simulerad telefon i webbappen – inget skickas på riktigt.</span>
      ) : (
        <>
          <span className={s.status === "not_configured" ? "text-fg-muted" : undefined}>Notiser med svarsknappar · {DELIVERY_LABELS[s.delivery_mode]}</span>
          {s.status === "not_configured" && s.delivery_mode === "pull" ? (
            <span className="block text-xs text-fg-muted">
              {s.heartbeat.fresh ? "Bortkopplad – ansluts igen när du sparar inställningarna." : "Väntar på första livstecknet från Home Assistant."}
            </span>
          ) : null}
          {s.notify_service ? <span className="block truncate font-mono text-xs text-fg-muted">notify.{s.notify_service}</span> : null}
        </>
      ),
      action:
        s.channel && (s.status === "connected" || s.demo) ? (
          <Button variant="ghost" size="sm" onClick={testNotification.run} loading={testNotification.isPending}>
            <BellRing aria-hidden /> Skicka testnotis
          </Button>
        ) : null,
    };
  }

  // ---- AI
  let aiCard: IntegrationCardData | undefined;
  if (ai.data) {
    const s = ai.data;
    aiCard = {
      status: s.status,
      successLabel: "Senast testad",
      lastSuccessAt: s.last_success_at,
      lastError: s.last_error,
      lastErrorAt: s.last_error_at,
      capabilityLabel: "Används till",
      capability: s.rule_engine_only ? (
        <span className="text-fg-muted">Används inte – reglerna klassar resorna.</span>
      ) : (
        <>
          Förslag på kategori och syfte
          <span className="block truncate font-mono text-xs text-fg-muted">
            {AI_PROVIDER_LABELS[s.provider_name]} · {s.model}
          </span>
        </>
      ),
      action:
        !s.rule_engine_only && s.status !== "not_configured" ? (
          <Button variant="ghost" size="sm" onClick={aiTest.run} loading={aiTest.isPending}>
            <FlaskConical aria-hidden /> Testa
          </Button>
        ) : null,
    };
  }

  const demoNames = [
    volvo.data?.demo.enabled ? PROVIDERS.volvo.name : null,
    microsoft.data?.status === "demo" ? PROVIDERS.microsoft.name : null,
    ha.data?.demo ? PROVIDERS.home_assistant.name : null,
  ].filter((n): n is string => !!n);

  const queries = { volvo, microsoft, home_assistant: ha, ai } as const;
  const cards: Record<ProviderKey, IntegrationCardData | undefined> = { volvo: volvoCard, microsoft: microsoftCard, home_assistant: haCard, ai: aiCard };
  const openQuery = open ? queries[open] : null;
  const openMeta = open ? PROVIDERS[open] : null;
  const openStatus = open ? cards[open]?.status : undefined;

  return (
    <>
      <PageHeader
        title="Integrationer"
        description="Koppla DriveLog till bilen, kalendern och mobilen. Allt här är valfritt – körjournalen fungerar med enbart import."
      />

      {demoNames.length > 0 ? (
        <Note tone="warn" icon={<FlaskConical aria-hidden />} title={`Demoläge är på för ${demoNames.join(", ").replace(/, ([^,]*)$/, " och $1")}`} className="mb-4" role="status">
          <p>Demodata är påhittade och ligger på ett separat demofordon. Din riktiga körjournal påverkas inte.</p>
          <DemoVehicleSwitch />
        </Note>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        {(Object.keys(PROVIDERS) as ProviderKey[]).map((key) => (
          <IntegrationCard
            key={key}
            id={key}
            icon={PROVIDERS[key].icon}
            name={PROVIDERS[key].name}
            tagline={PROVIDERS[key].tagline}
            data={cards[key]}
            isLoading={queries[key].isLoading}
            error={queries[key].error}
            onRetry={() => void queries[key].refetch()}
            onConfigure={() => openPanel(key)}
          />
        ))}
      </div>

      <Sheet open={open !== null} onOpenChange={(o) => !o && setOpen(null)}>
        <SheetContent
          className="w-[min(580px,100vw)] gap-0 overflow-hidden p-0"
          data-testid="integration-sheet"
          onOpenAutoFocus={(e) => {
            // Fokusera panelen, inte första fältet – annars fälls tangentbordet upp direkt på mobil.
            e.preventDefault();
            (e.currentTarget as HTMLElement | null)?.focus();
          }}
          onCloseAutoFocus={(e) => {
            // Panelen öppnas utan Radix-trigger – lämna tillbaka fokus till kortets knapp.
            e.preventDefault();
            if (lastOpened.current) document.getElementById(`configure-${lastOpened.current}`)?.focus();
          }}
        >
          {open && openMeta ? (
            <>
              <SheetHeader className="shrink-0 border-b border-border px-5 py-4 pr-12">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  <SheetTitle>{openMeta.name}</SheetTitle>
                  {openStatus ? <StatusBadge status={openStatus} /> : null}
                </div>
                <SheetDescription>{openMeta.tagline}</SheetDescription>
              </SheetHeader>
              <div className="scrollbar-thin flex-1 space-y-5 overflow-y-auto px-5 py-5 pb-[max(1.25rem,env(safe-area-inset-bottom))]">
                {open === "volvo" && volvo.data ? (
                  <VolvoPanel status={volvo.data} />
                ) : open === "microsoft" && microsoft.data ? (
                  <OutlookPanel status={microsoft.data} />
                ) : open === "home_assistant" && ha.data ? (
                  <HaPanel status={ha.data} />
                ) : open === "ai" && ai.data ? (
                  <AiPanel status={ai.data} />
                ) : openQuery?.isError ? (
                  <ErrorState error={openQuery.error} onRetry={() => void openQuery.refetch()} />
                ) : (
                  <SkeletonBlock rows={6} />
                )}
              </div>
            </>
          ) : null}
        </SheetContent>
      </Sheet>
    </>
  );
}
