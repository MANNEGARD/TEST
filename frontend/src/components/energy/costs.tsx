"use client";

import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { DescriptionItem, DescriptionList } from "@/components/ui/dl";
import { FieldError, Input, Label } from "@/components/ui/input";
import { QualityValue } from "@/components/trips/quality-value";
import { errorMessage } from "@/lib/api";
import type { CostSettings, EnergySummary } from "@/lib/energy-types";
import { formatCurrency, formatNumber, plural } from "@/lib/format";
import { useSaveCostSettings } from "@/lib/queries/energy";
import { useSettings } from "@/lib/queries/settings";
import { numberField, parseNumber } from "./form";

export function hasCosts(s: EnergySummary): boolean {
  return s.refuels.cost != null || s.charging.cost != null || s.estimated_costs.fuel != null || s.estimated_costs.electricity != null;
}

/** Kostnader för perioden – visas bara när det finns belopp eller ett pris att räkna med. */
export function CostsCard({ summary: s }: { summary: EnergySummary }) {
  const est = s.estimated_costs;
  const recorded = s.refuels.cost != null || s.charging.cost != null ? (s.refuels.cost ?? 0) + (s.charging.cost ?? 0) : null;
  return (
    <Card data-testid="energy-costs">
      <CardHeader>
        <div>
          <CardTitle>Kostnader</CardTitle>
          <CardDescription>Betalt enligt dina tankningar och laddningar, och en uppskattning ur förbrukningen.</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        <DescriptionList className="sm:grid-cols-2 lg:grid-cols-4">
          {s.refuels.cost != null ? (
            <DescriptionItem label="Bensin, betalt">
              <span className="num text-base font-medium">{formatCurrency(s.refuels.cost)}</span>
              <span className="mt-0.5 block text-xs text-fg-muted">
                {plural(s.refuels.count, "tankning", "tankningar")}
                {s.refuels.average_price_per_liter != null ? <> · snitt <span className="num">{formatNumber(s.refuels.average_price_per_liter, 2, 2)} kr/l</span></> : null}
              </span>
            </DescriptionItem>
          ) : null}
          {s.charging.cost != null ? (
            <DescriptionItem label="Laddning, betalt">
              <span className="num text-base font-medium">{formatCurrency(s.charging.cost)}</span>
              <span className="mt-0.5 block text-xs text-fg-muted">{plural(s.charging.count, "laddning", "laddningar")}</span>
            </DescriptionItem>
          ) : null}
          {recorded != null && s.refuels.cost != null && s.charging.cost != null ? (
            <DescriptionItem label="Betalt totalt">
              <span className="num text-base font-medium">{formatCurrency(recorded)}</span>
            </DescriptionItem>
          ) : null}
          {est.fuel != null ? (
            <DescriptionItem label="Bensin, uppskattat">
              <QualityValue mono className="text-base font-medium" value={formatCurrency(est.fuel)} quality="estimated" explanation={est.note} />
              <span className="mt-0.5 block text-xs text-fg-muted">Uppmätt förbrukning × ditt literpris</span>
            </DescriptionItem>
          ) : null}
          {est.electricity != null ? (
            <DescriptionItem label="El, uppskattat">
              <QualityValue mono className="text-base font-medium" value={formatCurrency(est.electricity)} quality="estimated" explanation={est.note} />
              <span className="mt-0.5 block text-xs text-fg-muted">Uppmätt förbrukning × ditt elpris</span>
            </DescriptionItem>
          ) : null}
        </DescriptionList>
      </CardContent>
    </Card>
  );
}

const PRICE_ERROR = "Ange priset som ett tal mellan 0 och 1 000, eller lämna fältet tomt.";

function validPrice(n: number | null): boolean {
  return n === null || (!Number.isNaN(n) && n > 0 && n <= 1000);
}

/** Hopfälld sektion: pris per liter/kWh som används för den uppskattade kostnaden. */
export function PriceSettings() {
  const settings = useSettings();
  const costs = settings.data?.settings.costs as Partial<CostSettings> | undefined;
  const fuel = typeof costs?.fuel_price_per_liter === "number" ? costs.fuel_price_per_liter : null;
  const el = typeof costs?.electricity_price_per_kwh === "number" ? costs.electricity_price_per_kwh : null;
  const meta = [fuel != null ? `${formatNumber(fuel, 2, 2)} kr/l` : null, el != null ? `${formatNumber(el, 2, 2)} kr/kWh` : null].filter(Boolean).join(" · ");
  return (
    <CollapsibleSection title="Bränsle- och elpris" meta={meta || "Inget pris angivet"} id="energy-prices">
      {settings.data ? <PriceForm key={`${fuel}|${el}`} fuel={fuel} el={el} /> : <p className="text-sm text-fg-muted">Laddar…</p>}
    </CollapsibleSection>
  );
}

function PriceForm({ fuel, el }: { fuel: number | null; el: number | null }) {
  const save = useSaveCostSettings();
  const [fuelText, setFuelText] = React.useState(numberField(fuel));
  const [elText, setElText] = React.useState(numberField(el));
  const [error, setError] = React.useState<string | null>(null);
  const fuelValue = parseNumber(fuelText);
  const elValue = parseNumber(elText);
  const dirty = fuelValue !== fuel || elValue !== el;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!validPrice(fuelValue) || !validPrice(elValue)) {
      setError(PRICE_ERROR);
      return;
    }
    setError(null);
    save.mutate(
      { fuel_price_per_liter: fuelValue, electricity_price_per_kwh: elValue },
      {
        onSuccess: () => toast.success(fuelValue == null && elValue == null ? "Priserna är borttagna." : "Priserna är sparade."),
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <p className="max-w-2xl text-sm leading-relaxed text-fg-muted">
        Ange ungefär vad du betalar, så visas en uppskattad kostnad: uppmätt förbrukning × pris. Lämna tomt om du inte vill ha någon uppskattning.
      </p>
      <div className="grid max-w-md grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="price-fuel">Bensin (kr/l)</Label>
          <Input id="price-fuel" inputMode="decimal" value={fuelText} onChange={(e) => setFuelText(e.target.value)} placeholder="t.ex. 18,50" aria-invalid={!validPrice(fuelValue)} autoComplete="off" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="price-el">El (kr/kWh)</Label>
          <Input id="price-el" inputMode="decimal" value={elText} onChange={(e) => setElText(e.target.value)} placeholder="t.ex. 2,20" aria-invalid={!validPrice(elValue)} autoComplete="off" />
        </div>
      </div>
      <FieldError>{error}</FieldError>
      <Button type="submit" variant="primary" size="sm" loading={save.isPending} disabled={!dirty}>
        Spara priser
      </Button>
    </form>
  );
}
