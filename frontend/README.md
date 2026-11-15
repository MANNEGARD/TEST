# SMGD DriveLog – webbgränssnitt

Next.js 16 (App Router) + TypeScript (strict), Tailwind CSS v4, komponenter i shadcn/ui-stil
(Radix + class-variance-authority), TanStack Query, Recharts, MapLibre GL och sonner.
All data kommer från backendens REST-API (`/api/v1`, proxas av Next till FastAPI).

## Kom igång

```bash
npm install                 # kopierar även MapLibre-workern till public/vendor (postinstall)
../scripts/dev.sh           # backend :8000 (SQLite) + next dev :3000
npm run build && npm run lint
../scripts/e2e.sh           # helhetstest över alla sidor mot en ny databas (egna portar 8100/3100)
```

Miljö: `API_INTERNAL_URL` (standard `http://127.0.0.1:8000`) styr vart `/api/v1/*` proxas.
I standalone-bygget bakas värdet in vid `npm run build` (Dockerfile: `--build-arg API_INTERNAL_URL=…`).

## Struktur

```
src/app/(auth)/        /login, /setup (utan app-skal)
src/app/(app)/         inloggade sidor – layout.tsx = AuthGate → VehicleProvider → AppShell
src/app/api/healthz    hälsokontroll för Docker
src/components/ui/     baskomponenter (button, card, dialog, table, stat-card, date-range-picker …)
src/components/shell/  sidomeny, sidhuvud, mobilmeny, fordonsväljare, ⌘K-sökning, nav-items.ts
src/components/trips/  QualityValue, SourceBadge, CategoryPill, ReviewStatusBadge, CategoryToggle,
                       TripTime, trip-table, trip-card, use-trip-actions (toast + Ångra)
src/components/map/    PointMap (MapLibre), TripMap, MapPlaceholder, useMapSettings
src/components/charts/ ChartCard (diagram/tabell-växel), diagram för dashboarden
src/lib/api.ts         typad fetch-klient (CSRF, ApiError, upload, download)
src/lib/api-types.ts   GENERERAD ur openapi.json – redigera inte
src/lib/types.ts       domäntyper (genererade + handskrivna för dict-svar)
src/lib/queries/*      query-nycklar (keys.ts) och hooks per område
src/lib/format.ts      ALL formatering (datum, tid, km/mil, l, kWh, valuta)
src/lib/labels.ts      svenska etiketter för alla enum-koder
```

## Konventioner

- **Svenska** i allt användaren ser. Etiketter för koder hämtas från `lib/labels.ts`.
- **Formatering** endast via `lib/format.ts` (yyyy-MM-dd, HH:mm i användarens tidszon, decimalkomma,
  mil i nyckeltal och km i tabeller). Tidszonen sätts av `AuthGate` från `settings.general.timezone`.
- **Datakvalitet**: visa mätvärden med `<QualityValue>` (faktiskt / ≈ uppskattat / – saknas),
  tider med `<TripTime>` (okänd tid visas som "okänd", aldrig ett påhittat klockslag),
  källa med `<SourceBadge>`, kategori med `<CategoryPill>`/`<CategoryToggle>`, status med `<ReviewStatusBadge>`.
- **API**: använd `api.get/post/patch/del/upload/download` – aldrig `fetch` direkt. Fel är `ApiError`;
  visa `err.detail` (svensk text från backend) och reagera på `err.code`.
- **Data**: TanStack Query. Nycklar skapas i `lib/queries/keys.ts`. Efter mutationer som rör resor:
  `invalidateJournalData(qc)`. Fordonsberoende hooks läser `useVehicle().vehicleId` och har det i nyckeln.
- **Färger** endast via tema-tokens (`bg-surface`, `text-fg-muted`, `bg-work-soft`, `text-private-ink`, …)
  definierade i `src/app/globals.css` för ljust och mörkt tema. Kategori/status kombineras alltid med text eller ikon.
- **Siffror** har `tabular-nums` globalt (klassen `num` där det behöver förtydligas) och visas i samma proportionella typsnitt
  som texten – inte `font-mono`, som ger glapp runt decimalkommat. `font-mono` används bara för koder: tokens, ID:n, URL:er,
  tjänstenamn och filnamn. Tal i tabeller högerställs.
- **Tillgänglighet**: semantiska element, `aria-label` på ikonknappar, synlig fokus (`focus-visible:outline-ring`).
- **Användningsfall**: milersättning för egen bil – tjänsteresor är huvudsaken. Luckor i mätarställningen
  är privat körning och visas inte som fel eller varningar.

## Lägga till en sida

1. Skapa `src/app/(app)/<namn>/page.tsx` (serverkomponent med `metadata`) som renderar en klientkomponent
   i `src/components/<område>/`.
2. Börja vyn med `<PageHeader title=… description=… actions=…/>`.
3. Lägg till hooks i `src/lib/queries/<område>.ts` med nycklar från `keys.ts`.
4. Lägg till sidan i `src/components/shell/nav-items.ts` (sidomeny + "Mer" på mobil).
5. Hantera laddning (`Skeleton`), fel (`ErrorState` med "Försök igen") och tomt läge (`EmptyState`).

## Typer från OpenAPI

```bash
npm run gen:openapi   # skriver openapi.json från backend (kräver backend/.venv)
npm run gen:api       # openapi-typescript → src/lib/api-types.ts
```

Svar som backend beskriver som `dict[str, Any]` har handskrivna typer i `src/lib/types.ts` med hänvisning
till backendfilen. Uppdatera dem när backend ändras.

## PWA

`app/manifest.ts`, ikoner i `public/icons` (`npm run gen:icons` genererar PNG ur `icon.svg` med sharp) och
`public/sw.js` som bara cachar statiska filer – aldrig `/api/`-svar.
