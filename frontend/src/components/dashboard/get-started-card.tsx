"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, Car, CheckCircle2, Circle, Home, Loader2, MapPin, PartyPopper, Smartphone, X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { FieldHint, Input, Label } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { useHaStatus, useVolvoStatus } from "@/lib/queries/integrations";
import { useHomeFromVehicle, useLocationMeta, type HomeFromVehicleResult } from "@/lib/queries/places";
import { useSettings } from "@/lib/queries/settings";
import { readStorage, writeStorage } from "@/lib/utils";

/** Kortet har visats med något steg kvar i den här webbläsaren. */
const SEEN_KEY = "drivelog.getStarted.seen";
/** Bekräftelsen "Klart – resten sköter sig självt" har visats. */
const DONE_KEY = "drivelog.getStarted.doneShown";
/** En position äldre än så här visas med datum och en fråga om bilen stod hemma då. */
const OLD_POSITION_MS = 12 * 60 * 60 * 1000;

type StepState = "done" | "todo" | "error";

/** "kl. 21:14" i dag, annars "3/11 kl. 21:14". */
export function positionTime(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? `kl. ${formatTime(d)}` : `${d.getDate()}/${d.getMonth() + 1} kl. ${formatTime(d)}`;
}

function StepIcon({ state }: { state: StepState }) {
  if (state === "done") return <CheckCircle2 className="size-5 shrink-0 text-ok-ink" aria-hidden />;
  return <Circle className="size-5 shrink-0 text-fg-subtle" aria-hidden />;
}

function Step({
  id,
  n,
  state,
  icon,
  title,
  status,
  children,
}: {
  id: string;
  n: number;
  state: StepState;
  icon: React.ReactNode;
  title: string;
  status: string;
  children?: React.ReactNode;
}) {
  return (
    <li data-testid={`get-started-step-${id}`} data-done={state === "done" ? "true" : "false"} className="flex gap-3 py-3 first:pt-0 last:pb-0">
      <StepIcon state={state} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-fg">
            <span className="sr-only">Steg {n}:</span>
            <span className="text-fg-subtle [&_svg]:size-4" aria-hidden>
              {icon}
            </span>
            {title}
          </span>
          <Badge tone={state === "done" ? "ok" : state === "error" ? "err" : "warn"} size="sm">
            {status}
          </Badge>
        </div>
        {children ? <div className="mt-1.5 text-[13px] text-fg-muted">{children}</div> : null}
      </div>
    </li>
  );
}

interface Marked {
  result: HomeFromVehicleResult;
  /** Positionen var äldre än OLD_POSITION_MS när bostaden markerades. */
  old: boolean;
}

function HomeStep({ done, marked, onDone }: { done: boolean; marked: Marked | null; onDone: (m: Marked) => void }) {
  const mark = useHomeFromVehicle();
  const [address, setAddress] = React.useState("");
  const inputId = React.useId();

  const submit = () => {
    mark.mutate(
      { address: address.trim() || null },
      {
        onSuccess: (r) => {
          onDone({ result: r, old: Date.now() - new Date(r.position_reported_at).getTime() > OLD_POSITION_MS });
          toast.success(`Bostaden är markerad med bilens position ${positionTime(r.position_reported_at)}.`);
        },
      },
    );
  };

  if (done) {
    if (!marked) return <>Adresser för dina resor slås upp automatiskt.</>;
    const { result, old } = marked;
    return (
      <div className="space-y-1">
        <p data-testid="get-started-home-result">
          Markerad med bilens position {positionTime(result.position_reported_at)}.{" "}
          {old ? (
            <>
              Stod bilen inte hemma då? <Link href="/places" className="font-medium text-accent-ink underline underline-offset-2">Flytta bostaden på kartan</Link>.
            </>
          ) : (
            "Adresser för dina resor slås nu upp automatiskt."
          )}
        </p>
        {!result.location.address ? (
          <p data-testid="get-started-home-address-missing" className="text-warn-ink">
            Bostadens adress saknas – den står i underlaget i stället för ordet &quot;Hem&quot;.{" "}
            <Link href={`/places?plats=${result.location.id}`} className="font-medium text-accent-ink underline underline-offset-2">
              Fyll i adressen
            </Link>
            .
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-2.5">
      <p>Då kan appen slå upp adresser för dina resor. Din hemadress skickas aldrig till karttjänsten.</p>
      <div className="max-w-md space-y-1">
        <Label htmlFor={inputId}>Adress som ska stå i underlaget (valfritt)</Label>
        <Input
          id={inputId}
          data-testid="get-started-home-address"
          value={address}
          maxLength={300}
          autoComplete="off"
          placeholder="t.ex. Gatan 1, Orten"
          onChange={(e) => setAddress(e.target.value)}
          disabled={mark.isPending}
        />
        <FieldHint>Gatuadress och ort. Står i underlaget i stället för ordet &quot;Hem&quot; och skickas aldrig vidare. Kan också fyllas i senare under Platser.</FieldHint>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Button size="sm" variant="primary" onClick={submit} disabled={mark.isPending} data-testid="get-started-home-button">
          {mark.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Home aria-hidden />}
          {mark.isPending ? "Sparar …" : "Bilen står hemma nu"}
        </Button>
        <Link href="/places" className="inline-flex items-center gap-1 font-medium text-accent-ink underline underline-offset-2 hover:no-underline">
          <MapPin className="size-3.5" aria-hidden /> Markera på kartan i stället
        </Link>
      </div>
      {mark.isError ? (
        <p role="alert" data-testid="get-started-home-error" className="text-err-ink">
          {errorMessage(mark.error)}
        </p>
      ) : null}
    </div>
  );
}

/**
 * "Kom igång" överst på översikten i självgående läge: anslut bilen, markera bostaden och
 * anslut telefonen. Försvinner när allt är klart (med en kort bekräftelse en gång) och visas
 * aldrig i läget "Jag godkänner allt själv".
 */
export function GetStartedCard() {
  const settings = useSettings();
  const selfDriving = settings.data?.settings.automation?.mode === "self_driving";
  const volvo = useVolvoStatus();
  const ha = useHaStatus();
  const meta = useLocationMeta();
  const [marked, setMarked] = React.useState<Marked | null>(null);
  // Visades kortet med något steg kvar vid ett tidigare besök (och bekräftelsen inte än)?
  const [earlier] = React.useState(() => readStorage("local", SEEN_KEY, false) && !readStorage("local", DONE_KEY, false));
  const [phase, setPhase] = React.useState<"unknown" | "incomplete" | "celebrate" | "hidden">("unknown");

  const ready = !volvo.isPending && !ha.isPending && !meta.isPending;
  const car: StepState = volvo.isError ? "error" : volvo.data?.is_connected ? "done" : "todo";
  const home: StepState = meta.isError ? "error" : meta.data?.home_has_coordinate ? "done" : "todo";
  const h = ha.data;
  const phoneReady = !!h && !h.demo && h.status === "connected" && !!h.notify_service;
  const phone: StepState = ha.isError ? "error" : phoneReady ? "done" : "todo";
  const allDone = car === "done" && home === "done" && phone === "done";
  const show = selfDriving && ready;

  // Läget följer stegen (justeras under renderingen, inte i en effekt).
  if (show) {
    if (!allDone && phase !== "incomplete") setPhase("incomplete");
    else if (allDone && phase === "incomplete") setPhase("celebrate");
    else if (allDone && phase === "unknown") setPhase(earlier ? "celebrate" : "hidden");
  }

  React.useEffect(() => {
    if (phase === "incomplete") writeStorage("local", SEEN_KEY, true);
    if (phase === "celebrate") writeStorage("local", DONE_KEY, true);
  }, [phase]);

  if (!show) return null;

  if (allDone) {
    if (phase !== "celebrate") return null;
    return (
      <Card role="status" data-testid="get-started-done" className="mb-6 flex items-center gap-3 border-ok-ink/30 bg-ok-soft px-4 py-3 text-sm text-ok-ink">
        <PartyPopper className="size-5 shrink-0" aria-hidden />
        <span className="flex-1 font-semibold">Klart – resten sköter sig självt.</span>
        <Button size="sm" variant="ghost" onClick={() => setPhase("hidden")} aria-label="Stäng">
          <X aria-hidden />
        </Button>
      </Card>
    );
  }

  const left = [car, home, phone].filter((s) => s !== "done").length;
  const retry = () => {
    if (volvo.isError) void volvo.refetch();
    if (ha.isError) void ha.refetch();
    if (meta.isError) void meta.refetch();
  };
  const demoCar = !!volvo.data?.demo?.enabled;

  return (
    <Card data-testid="get-started" className="mb-6 border-accent-ink/25 p-4 sm:p-5">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold tracking-tight text-fg">Kom igång</h2>
          <p className="text-[13px] text-fg-muted">
            Tre steg, sedan sköter körjournalen sig själv. {left === 1 ? "1 steg kvar." : `${left} steg kvar.`}
          </p>
        </div>
      </div>
      <ol className="divide-y divide-border">
        <Step
          id="car"
          n={1}
          state={car}
          icon={<Car />}
          title="Anslut bilen (Volvo)"
          status={car === "done" ? "Ansluten" : car === "error" ? "Kunde inte läsa status" : "Inte ansluten"}
        >
          {car === "done" ? (
            "Resorna hämtas från bilen av sig själv."
          ) : (
            <>
              {demoCar ? "Demobilen räknas inte – anslut din riktiga bil. " : "Då hämtas resorna från bilen av sig själv. "}
              <Link href="/integrations" className="inline-flex items-center gap-1 font-medium text-accent-ink underline underline-offset-2 hover:no-underline">
                Anslut bilen <ArrowRight className="size-3.5" aria-hidden />
              </Link>
            </>
          )}
        </Step>
        <Step
          id="home"
          n={2}
          state={home}
          icon={<Home />}
          title="Markera bostaden"
          status={home === "done" ? "Markerad" : home === "error" ? "Kunde inte läsa status" : "Inte markerad"}
        >
          <HomeStep done={home === "done"} marked={marked} onDone={setMarked} />
        </Step>
        <Step
          id="phone"
          n={3}
          state={phone}
          icon={<Smartphone />}
          title="Anslut telefonen (Home Assistant)"
          status={phone === "done" ? "Ansluten" : phone === "error" ? "Kunde inte läsa status" : h && !h.demo && h.status === "connected" ? "Ingen telefon vald" : "Inte ansluten"}
        >
          {phone === "done" ? (
            "Frågor och notiser kommer till telefonen."
          ) : (
            <>
              {h && !h.demo && h.status === "connected"
                ? "Home Assistant är anslutet – välj vilken telefon frågorna ska gå till. "
                : "Då kan du svara på de få frågor som finns direkt i telefonen. "}
              <Link href="/integrations" className="inline-flex items-center gap-1 font-medium text-accent-ink underline underline-offset-2 hover:no-underline">
                {h && !h.demo && h.status === "connected" ? "Välj telefon" : "Anslut telefonen"} <ArrowRight className="size-3.5" aria-hidden />
              </Link>
            </>
          )}
        </Step>
      </ol>
      {car === "error" || home === "error" || phone === "error" ? (
        <div role="alert" className="mt-3 flex flex-wrap items-center gap-2 text-[13px] text-err-ink">
          Statusen kunde inte hämtas just nu.
          <Button size="sm" variant="secondary" onClick={retry}>
            Försök igen
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
