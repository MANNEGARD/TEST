"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, Check, CircleDashed, Home, MapPinOff, MapPinned, Merge, Sparkles, X } from "lucide-react";
import { toast } from "sonner";
import { MapPlaceholder } from "@/components/map/map-placeholder";
import { useMapSettings } from "@/components/map/use-map-settings";
import { CategoryPill } from "@/components/trips/category-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDate, formatMil, formatNumber } from "@/lib/format";
import type { Place, PlaceInput } from "@/lib/places-types";
import { useLookupAddress, useUpdatePlace } from "@/lib/queries/places";
import { LocationTypeSelect, PlaceAvatar, typeLabel, typeText } from "./location-type";
import { PlaceAdvanced, type AdvancedSection } from "./place-advanced";
import { PositionPicker, type LatLon } from "./position-picker";

const COORDINATE_SOURCE: Record<string, string> = {
  user: "satt av dig",
  api: "från bilens position",
  geocoded: "ungefärlig position ur adressen",
  demo: "demodata",
};

function parseDecimal(text: string): number | null {
  const t = text.trim().replace(/\s/g, "").replace(",", ".");
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : NaN;
}

function coordText(n: number): string {
  return formatNumber(n, 6, 0);
}

function suggestionText(reason: string | null): string {
  // Backend inleder med "Förslag: <typ>." – typen visas redan separat.
  return (reason ?? "").replace(/^Förslag:\s*[^.]+\.\s*/, "");
}

/** Sidopanel för att redigera en plats. `placeId = null` stänger panelen. */
export function PlaceSheet({
  placeId,
  places,
  onClose,
  onOpenPlace,
  onCloseAutoFocus,
}: {
  placeId: string | null;
  places: Place[];
  onClose: () => void;
  onOpenPlace: (id: string) => void;
  /** Lämnar tillbaka fokus till raden/knappen som öppnade panelen. */
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const place = placeId ? places.find((p) => p.id === placeId) : undefined;
  return (
    <Sheet open={!!place} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side="right"
        className="w-[min(480px,100vw)] gap-0 overflow-hidden p-0"
        onOpenAutoFocus={(e) => {
          // Fokusera panelen, inte namnfältet – annars fälls tangentbordet upp direkt på mobil.
          e.preventDefault();
          (e.currentTarget as HTMLElement | null)?.focus();
        }}
        onCloseAutoFocus={onCloseAutoFocus}
      >
        {place ? <PlaceEditor key={place.id} place={place} places={places} onClose={onClose} onOpenPlace={onOpenPlace} /> : null}
      </SheetContent>
    </Sheet>
  );
}

function PlaceEditor({
  place,
  places,
  onClose,
  onOpenPlace,
}: {
  place: Place;
  places: Place[];
  onClose: () => void;
  onOpenPlace: (id: string) => void;
}) {
  const update = useUpdatePlace();
  const lookup = useLookupAddress();
  const mapSettings = useMapSettings();
  const initialPos: LatLon | null = place.latitude != null && place.longitude != null ? { lat: place.latitude, lon: place.longitude } : null;

  const [name, setName] = React.useState(place.name);
  const [type, setType] = React.useState(place.type);
  const [address, setAddress] = React.useState(place.address ?? "");
  const [lookupNote, setLookupNote] = React.useState<string | null>(null);
  const [company, setCompany] = React.useState(place.company ?? "");
  const [notes, setNotes] = React.useState(place.notes ?? "");
  const [radius, setRadius] = React.useState(String(place.radius_m));
  const [pos, setPos] = React.useState<LatLon | null>(initialPos);
  const [latText, setLatText] = React.useState(initialPos ? coordText(initialPos.lat) : "");
  const [lonText, setLonText] = React.useState(initialPos ? coordText(initialPos.lon) : "");
  const [error, setError] = React.useState<string | null>(null);
  const [homeConflict, setHomeConflict] = React.useState<string | null>(null);
  const [advanced, setAdvanced] = React.useState<AdvancedSection | null>(null);
  const [mergeTarget, setMergeTarget] = React.useState("");

  const suggestion = !place.confirmed && place.suggested_type ? { type: place.suggested_type, name: place.suggested_name } : null;
  const suggestionApplied = !!suggestion && type === suggestion.type && (!suggestion.name || name === suggestion.name);
  const otherHome = places.find((p) => p.type === "home" && p.confirmed && p.id !== place.id && p.is_demo === place.is_demo);
  const context = React.useMemo(
    () => places.filter((p) => p.id !== place.id && p.latitude != null && p.longitude != null).map((p) => ({ lat: p.latitude as number, lon: p.longitude as number })),
    [places, place.id],
  );

  const radiusValue = parseDecimal(radius);
  const lat = parseDecimal(latText);
  const lon = parseDecimal(lonText);
  const coordsPartial = (lat === null) !== (lon === null);
  const coordsInvalid =
    (lat !== null && (Number.isNaN(lat) || Math.abs(lat) > 90)) || (lon !== null && (Number.isNaN(lon) || Math.abs(lon) > 180));
  const problems: string[] = [];
  if (!name.trim()) problems.push("Ange ett namn på platsen.");
  if (radiusValue === null || Number.isNaN(radiusValue) || !Number.isInteger(radiusValue) || radiusValue < 30 || radiusValue > 5000)
    problems.push("Geofence-radien ska vara ett heltal mellan 30 och 5 000 meter.");
  if (coordsInvalid) problems.push("Koordinaterna är ogiltiga. Latitud ska ligga mellan −90 och 90, longitud mellan −180 och 180.");
  else if (coordsPartial) problems.push("Ange både latitud och longitud, eller lämna båda tomma.");

  const patch: PlaceInput = {};
  if (name.trim() !== place.name) patch.name = name.trim();
  if (type !== place.type) patch.type = type;
  if (address.trim() !== (place.address ?? "")) patch.address = address.trim() || null;
  if (company.trim() !== (place.company ?? "")) patch.company = company.trim();
  if (notes.trim() !== (place.notes ?? "")) patch.notes = notes.trim();
  if (radiusValue !== null && radiusValue !== place.radius_m) patch.radius_m = radiusValue;
  if (pos && (pos.lat !== initialPos?.lat || pos.lon !== initialPos?.lon)) {
    patch.latitude = pos.lat;
    patch.longitude = pos.lon;
  } else if (!pos && initialPos) patch.clear_coordinates = true;
  const dirty = Object.keys(patch).length > 0;
  // Att spara en obekräftad plats är att ta ställning till den.
  if (!place.confirmed) patch.confirmed = true;

  const pick = (p: LatLon) => {
    setPos(p);
    setLatText(coordText(p.lat));
    setLonText(coordText(p.lon));
  };
  const typeCoord = (which: "lat" | "lon", text: string) => {
    const nextLat = which === "lat" ? text : latText;
    const nextLon = which === "lon" ? text : lonText;
    if (which === "lat") setLatText(text);
    else setLonText(text);
    const a = parseDecimal(nextLat);
    const b = parseDecimal(nextLon);
    if (a === null && b === null) setPos(null);
    else if (a !== null && b !== null && !Number.isNaN(a) && !Number.isNaN(b) && Math.abs(a) <= 90 && Math.abs(b) <= 180) setPos({ lat: a, lon: b });
  };
  // "Hämta adressen från kartan": bara den sparade positionen slås upp, och bara efter ett tryck.
  const positionSaved = !!initialPos && patch.latitude === undefined && !patch.clear_coordinates;
  const fetchAddress = () => {
    setError(null);
    setLookupNote(null);
    lookup.mutate(place.id, {
      onSuccess: (r) => {
        setAddress(r.address);
        setLookupNote("Adressen är ifylld från kartan. Kontrollera den och spara.");
      },
      onError: (err) => setError(errorMessage(err)),
    });
  };
  // Att spara bekräftar platsen, och en bekräftad plats behöver en adress i underlaget.
  // Som underlagets kontroll (place_text.needs_address): bostaden och bekräftade platser måste ha
  // gatuadress och ort. En plats som sparas här blir alltid bekräftad, så det gäller alla.
  const addressMissing = !address.trim();
  const cityMissing =
    !!address.trim() &&
    address.split(",").filter((part) => part.trim()).length < 2 &&
    !(address.trim() === (place.address ?? "").trim() && place.city);

  // Adressuppslaget (1.3.0) gäller den sparade adressen – inte en ändrad i fältet.
  const addressUnchanged = address.trim() === (place.address ?? "").trim();
  const lookupPending = addressUnchanged && place.position_lookup === "pending";
  const lookupFailed = addressUnchanged && (place.position_lookup === "not_found" || place.position_lookup === "ambiguous");

  const clearPosition = () => {
    setPos(null);
    setLatText("");
    setLonText("");
  };

  const applySuggestion = () => {
    if (!suggestion) return;
    setType(suggestion.type);
    setHomeConflict(null);
    if (suggestion.name) {
      setName(suggestion.name);
      if (suggestion.type === "customer" && !company.trim()) setCompany(suggestion.name);
    }
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setHomeConflict(null);
    if (problems.length) {
      setError(problems[0]);
      return;
    }
    void update.mutateAsync({ id: place.id, patch }).then(
      (saved) => {
        toast.success(place.confirmed ? "Platsen är sparad." : `${saved.name} är bekräftad.`);
        onClose();
      },
      (err) => {
        if (err instanceof ApiError && err.code === "home_exists") setHomeConflict(err.detail);
        else setError(err instanceof ApiError && err.fields.length ? `${err.detail} (${err.fields.map((f) => f.field).join(", ")})` : errorMessage(err));
      },
    );
  };

  const stats = place.stats;
  const purposes = stats.top_purposes ?? [];
  const headerAddress = place.address && place.address !== place.name ? place.address : null;

  return (
    <>
      <SheetHeader className="shrink-0 gap-3 border-b border-border px-5 py-4 pr-12">
        <div className="flex items-start gap-3">
          <PlaceAvatar type={place.type} confirmed={place.confirmed} className="mt-0.5" />
          <div className="min-w-0">
            <SheetTitle className="break-words">{place.name}</SheetTitle>
            <SheetDescription className="break-words">
              {typeText(place.type)}
              {headerAddress ? ` · ${headerAddress}` : place.city && !place.name.includes(place.city) ? ` · ${place.city}` : ""}
            </SheetDescription>
          </div>
        </div>
        {!place.confirmed || place.is_demo ? (
          <div className="flex flex-wrap gap-1.5">
            {!place.confirmed ? (
              <Badge tone="undecided">
                <CircleDashed aria-hidden /> Obekräftad
              </Badge>
            ) : null}
            {place.is_demo ? <Badge tone="warn">Demo</Badge> : null}
          </div>
        ) : null}
      </SheetHeader>

      <div className="scrollbar-thin flex flex-1 flex-col gap-5 overflow-y-auto px-5 py-5">
        <section aria-label="Besök" className="rounded-lg bg-surface-2/70 px-4 py-3 text-sm">
          {place.visit_count > 0 ? (
            <>
              <dl className="grid grid-cols-3 gap-3">
                <div>
                  <dt className="text-xs text-fg-muted">Besök</dt>
                  <dd className="mt-0.5 num font-medium">{formatNumber(place.visit_count, 0)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-fg-muted">Körsträcka hit</dt>
                  <dd className="mt-0.5 num font-medium">{formatMil(stats.km)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-fg-muted">Senast</dt>
                  <dd className="mt-0.5 num font-medium">{formatDate(place.last_visited_at)}</dd>
                </div>
              </dl>
              {stats.most_common_category || purposes.length ? (
                <div className="mt-3 space-y-2 border-t border-border pt-3">
                  {stats.most_common_category ? (
                    <p className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
                      Oftast <CategoryPill category={stats.most_common_category} size="sm" />
                      <span className="num">
                        {formatNumber(stats.work ?? 0, 0)} arbete · {formatNumber(stats.private ?? 0, 0)} privat
                      </span>
                    </p>
                  ) : null}
                  {purposes.length ? (
                    <ul className="space-y-0.5 text-[13px]" aria-label="Vanligaste syften">
                      {purposes.slice(0, 3).map((p) => (
                        <li key={p.text} className="flex items-baseline justify-between gap-3">
                          <span className="min-w-0 truncate">{p.text}</span>
                          <span className="shrink-0 num text-xs text-fg-muted">{formatNumber(p.count, 0)}×</span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}
              {place.address ? (
                <Link
                  href={`/journal?q=${encodeURIComponent(place.address)}`}
                  className="mt-3 inline-flex items-center gap-1 rounded-sm text-[13px] font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
                >
                  Visa resorna i körjournalen <ArrowRight className="size-3.5" aria-hidden />
                </Link>
              ) : null}
            </>
          ) : (
            <p className="text-fg-muted">Inga resor hit ännu.</p>
          )}
        </section>

        {suggestion ? (
          <div className="flex items-start gap-2.5 rounded-lg border border-accent/30 bg-accent-soft/60 p-3 text-sm">
            <Sparkles className="mt-0.5 size-4 shrink-0 text-accent-ink" aria-hidden />
            <div className="min-w-0 flex-1 space-y-2">
              <p>
                <span className="font-medium">
                  Förslag: {typeLabel(suggestion.type)}
                  {suggestion.name ? ` – ${suggestion.name}` : ""}
                </span>
                <span className="block text-fg-muted">{suggestionText(place.suggestion_reason)}</span>
              </p>
              {suggestionApplied ? (
                <p className="inline-flex items-center gap-1.5 text-xs font-medium text-accent-ink">
                  <Check className="size-3.5" aria-hidden /> Ifyllt nedan – spara för att bekräfta
                </p>
              ) : (
                <Button size="sm" onClick={applySuggestion}>
                  Använd förslaget
                </Button>
              )}
            </div>
          </div>
        ) : null}

        <form id="place-form" onSubmit={submit} className="grid gap-4" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="place-name">Namn</Label>
            <Input id="place-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} required autoComplete="off" aria-invalid={!name.trim() || undefined} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="place-type">Typ</Label>
            <LocationTypeSelect
              id="place-type"
              value={type}
              invalid={!!homeConflict}
              onChange={(t) => {
                setType(t);
                setHomeConflict(null);
              }}
            />
            {homeConflict ? (
              <div role="alert" className="space-y-2 rounded-md bg-err-soft px-3 py-2.5 text-sm text-err-ink">
                <p className="flex items-center gap-1.5 font-medium">
                  <Home className="size-4 shrink-0" aria-hidden /> Du har redan ett hem
                </p>
                <p>
                  {otherHome ? (
                    <>
                      <strong className="font-medium">{otherHome.name}</strong> är redan markerad som hem, och bara en plats kan vara det.
                    </>
                  ) : (
                    homeConflict
                  )}{" "}
                  Byt typ på den andra platsen först – eller slå ihop platserna om det är samma ställe.
                </p>
                {otherHome ? (
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" onClick={() => onOpenPlace(otherHome.id)}>
                      Öppna {otherHome.name}
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => {
                        setMergeTarget(otherHome.id);
                        setAdvanced("merge");
                        window.requestAnimationFrame(() => document.getElementById("place-merge")?.scrollIntoView({ block: "nearest" }));
                      }}
                    >
                      <Merge aria-hidden /> Slå ihop platserna
                    </Button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="place-address">
              Adress <span className="font-normal text-fg-muted">(gatuadress och ort – står i underlaget)</span>
            </Label>
            <Input
              id="place-address"
              data-testid="place-address"
              value={address}
              onChange={(e) => {
                setAddress(e.target.value);
                setLookupNote(null);
              }}
              maxLength={300}
              autoComplete="off"
              placeholder="t.ex. Gatan 1, 123 45 Orten"
              aria-describedby="place-address-hint"
              aria-invalid={addressMissing || undefined}
            />
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
              <Button
                size="sm"
                variant="secondary"
                onClick={fetchAddress}
                loading={lookup.isPending}
                disabled={!positionSaved || place.is_demo}
                data-testid="place-address-lookup"
              >
                <MapPinned aria-hidden /> Hämta adressen från kartan
              </Button>
              {!positionSaved && !place.is_demo ? <span className="text-xs text-fg-muted">Sätt ut och spara positionen först.</span> : null}
            </div>
            <FieldHint id="place-address-hint">
              {type === "home" ? "Bostaden står med sin adress i underlaget, aldrig som ”Hem”. " : "Underlaget skriver platsens namn och adress. "}
              Knappen skickar platsens position till karttjänsten (standard OpenStreetMap) en gång för att slå upp adressen; inget skickas utan att
              du trycker.
            </FieldHint>
            {lookupNote ? (
              <p role="status" className="text-xs text-ok-ink">
                {lookupNote}
              </p>
            ) : addressMissing || cityMissing ? (
              <p className="text-xs text-warn-ink" data-testid="place-address-missing">
                {addressMissing ? "Adress saknas" : "Orten saknas – skriv den efter ett kommatecken"} – står platsen i en tjänsteresa skapas månadens
                underlag inte förrän {addressMissing ? "den" : "orten"} är ifylld.
              </p>
            ) : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="place-company">
              Företag <span className="font-normal text-fg-muted">(valfritt)</span>
            </Label>
            <Input id="place-company" value={company} onChange={(e) => setCompany(e.target.value)} maxLength={200} autoComplete="off" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="place-notes">
              Anteckning <span className="font-normal text-fg-muted">(valfritt)</span>
            </Label>
            <Textarea id="place-notes" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000} />
          </div>

          <fieldset className="space-y-2.5">
            <div className="flex items-center justify-between gap-2">
              <legend className="float-left text-[13px] font-medium">Position</legend>
              {pos ? (
                <Button variant="ghost" size="xs" onClick={clearPosition}>
                  <X aria-hidden /> Ta bort position
                </Button>
              ) : null}
            </div>
            {mapSettings.loading ? (
              <Skeleton className="h-56 w-full rounded-lg" />
            ) : mapSettings.enabled && mapSettings.tilesUrl ? (
              <PositionPicker value={pos} radius={radiusValue && !Number.isNaN(radiusValue) ? radiusValue : place.radius_m} onChange={pick} tilesUrl={mapSettings.tilesUrl} context={context} />
            ) : (
              <MapPlaceholder
                title="Kartor är avstängda"
                description="Kartvisning är avstängd i sekretessinställningarna. Du kan ange koordinater för hand nedan."
                className="h-40 min-h-0"
              />
            )}
            <p className="flex items-center gap-1.5 text-xs text-fg-muted" aria-live="polite">
              {pos ? (
                <>
                  <span className="num text-fg">
                    {formatNumber(pos.lat, 5, 5)}, {formatNumber(pos.lon, 5, 5)}
                  </span>
                  {patch.latitude === undefined && place.coordinate_source ? <span>· {COORDINATE_SOURCE[place.coordinate_source] ?? place.coordinate_source}</span> : null}
                </>
              ) : lookupPending ? (
                <>
                  <MapPinned className="size-3.5 shrink-0" aria-hidden />
                  <span data-testid="place-position-lookup">
                    <span className="font-medium text-fg">Positionen slås upp.</span> DriveLog slår upp adressen på kartan i bakgrunden, lite i
                    taget. Du kan också sätta ut platsen själv.
                  </span>
                </>
              ) : lookupFailed ? (
                <>
                  <MapPinOff className="size-3.5 shrink-0 text-warn-ink" aria-hidden />
                  <span data-testid="place-position-lookup">
                    <span className="font-medium text-fg">Adressen hittades inte på kartan.</span> Kartan hittade inte adressen eller gav flera
                    möjliga platser. Sätt ut platsen på kartan ovan, eller ange koordinaterna, och spara.
                  </span>
                </>
              ) : (
                <>
                  <MapPinOff className="size-3.5 shrink-0" aria-hidden />
                  <span>
                    <span className="font-medium text-fg">Ingen position satt.</span> Platsen känns igen på adressen.
                  </span>
                </>
              )}
            </p>
            <div className="grid grid-cols-3 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="place-lat" className="text-xs font-normal text-fg-muted">
                  Latitud
                </Label>
                <Input id="place-lat" inputMode="decimal" value={latText} onChange={(e) => typeCoord("lat", e.target.value)} placeholder="59,3293" autoComplete="off" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="place-lon" className="text-xs font-normal text-fg-muted">
                  Longitud
                </Label>
                <Input id="place-lon" inputMode="decimal" value={lonText} onChange={(e) => typeCoord("lon", e.target.value)} placeholder="18,0686" autoComplete="off" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="place-radius" className="text-xs font-normal text-fg-muted">
                  Geofence (m)
                </Label>
                <Input id="place-radius" type="number" inputMode="numeric" min={30} max={5000} step={10} value={radius} onChange={(e) => setRadius(e.target.value)} />
              </div>
            </div>
            <FieldHint>Resor som slutar inom geofencen kopplas till platsen, även om adressen skiljer sig.</FieldHint>
          </fieldset>
          <FieldError>{error}</FieldError>
        </form>

        <PlaceAdvanced
          place={place}
          places={places}
          open={advanced}
          onOpenChange={setAdvanced}
          mergeTarget={mergeTarget}
          onMergeTarget={setMergeTarget}
          onMerged={(keepId) => (keepId === place.id ? setAdvanced(null) : onOpenPlace(keepId))}
          onDeleted={onClose}
        />
      </div>

      <div className="flex shrink-0 items-center justify-end gap-2 border-t border-border bg-surface px-5 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        <Button variant="ghost" onClick={onClose}>
          Avbryt
        </Button>
        <Button type="submit" form="place-form" variant="primary" loading={update.isPending} disabled={place.confirmed && !dirty}>
          {place.confirmed ? "Spara" : "Spara och bekräfta"}
        </Button>
      </div>
    </>
  );
}
