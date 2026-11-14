# Arkitektur och tekniska beslut

## Översikt

```
 Webbläsare / PWA ──HTTPS──► Caddy ──► web (Next.js)
                                  └──► api (FastAPI) ──► PostgreSQL
 Home Assistant ──Bearer-token──────────► │
                                          ├── Redis ◄── worker + beat (Celery)
 Volvo API ◄── worker (insamling) ────────┤
 Microsoft Graph ◄── worker (kalender) ───┤
 AI-leverantör ◄── worker (klassificering)┘
```

- **Backend:** Python 3.13, FastAPI, SQLAlchemy 2 (synkron), Alembic, Pydantic.
- **Bakgrundsjobb:** Celery med Redis. Jobb kvitteras efter körning (`acks_late`) och är
  idempotenta, så de tål omstart. I utveckling/test körs de direkt i processen.
- **Frontend:** Next.js (App Router), TypeScript, Tailwind, TanStack Query, Recharts, MapLibre GL.
- **Home Assistant** är en integration – ingen backend och ingen databas. Webbappen fungerar
  fullt ut utan den.

Koden är uppdelad i tydligt separerade moduler under `backend/app/services/`:

| Modul | Ansvar |
| --- | --- |
| `importer/` | Tolkning av Volvo-exporter, förhandsgranskning, bekräfta, ångra |
| `trips/` | Journalresor, segment, sammanslagning, dataluckor, tjänsteresor, granskningsärenden |
| `places/` | Platsregister, adressnormalisering, geofence, förslag |
| `classify/` | Regelmotor, syftesförslag, AI-lager, fabriceringsskydd |
| `learning/` | Mönster, användarbeslut, ångra |
| `calendar/` | Microsoft Graph: OAuth, synk, matchning |
| `volvo/` | OAuth, API-klient, insamlare, resedetektering, demoläge |
| `ha/` | Notismotor, svarshantering, Home Assistant-klient |
| `stats/`, `export/` | Dashboard, rapporter, avvikelser, Excel |

## Tre nivåer av resedata

Det viktigaste beslutet i datamodellen är att originaluppgifter, fysiska resor och
journalposter är tre skilda saker:

| Nivå | Tabell | Innehåll |
| --- | --- | --- |
| Rådata | `vehicle_observations` | Varje svar från fordons-API:t, med både hämtningstid och bilens egen tidsstämpel |
| Fysisk resa | `raw_trips` | En importerad eller identifierad körning i originalskick. Ändras aldrig av sammanslagning |
| Journalresa | `logical_trips` + `trip_segments` | Det som visas i körjournalen. Består av ett eller flera segment som pekar på fysiska resor |

All kilometerredovisning sker per **segment**. En fysisk resa tillhör exakt en aktiv
journalresa, så en sträcka kan aldrig räknas två gånger. Ett segment kan ha egen kategori,
och en fysisk resa kan delas i delsträckor (t.ex. 30 km tjänst + 12 km privat) vars summa
måste vara lika med den uppmätta sträckan.

### Datakällor och kvalitet

Varje fysisk resa bär sin källa och kvalitet:

| Fält | Värden |
| --- | --- |
| `source` | `volvo_import` (verifierad ur Volvos körjournal), `volvo_api` (rekonstruerad ur fordonsrapporter), `manual`, `gap_fill` (sammanfattad lucka), `demo` |
| `start_time_quality` / `end_time_quality` | `exact`, `reported`, `estimated`, `bounded`, `unknown` |
| `distance_quality` | `measured`, `odometer`, `estimated`, `user` |
| `fuel_quality` / `energy_quality` | `verified`, `estimated`, `unknown` |
| `odometer_source`, `position_source` | varifrån mätarställning och position kommer |
| `is_complete`, `possible_missed_trips`, `quality_flags` | luckor och osäkerheter |

När källan saknar en faktisk starttid lagras **ingen** starttid – bara kända gränser
(`start_earliest`, `start_latest`). Gränssnittet visar då "okänd" och exporten lämnar cellen
tom med en notering.

## Resedetektering ur Volvo-data

Volvos API har ingen resejournal och rapporterar positionen först när en körning avslutats
(se [Volvo API](volvo-api.md)). Insamlaren hämtar därför mätarställning, position, tanknivå,
batterinivå och motorstatus med jämna mellanrum och sparar varje svar som en observation.
En tillståndsmaskin jämför observationerna:

1. Senaste stabila läge är *ankaret*.
2. Ökad mätarställning eller ändrad position betyder rörelse.
3. När läget varit oförändrat tillräckligt länge (eller motorn rapporteras avstängd) skapas en
   preliminär fysisk resa från ankaret till det nya läget.

Resan flaggas som möjligen flera körningar när mätarsträckan är mycket längre än fågelvägen,
när positionen är oförändrad men mätaren ökat, eller när insamlingen haft ett uppehåll. Sådana
resor hamnar i granskningscentret. När samma period senare importeras från Volvos körjournal
ersätter de verifierade originalresorna de rekonstruerade (som makuleras men finns kvar för
spårbarhet); användarens beslut flyttas över vid entydig träff.

## Sammanslagning

Regelmotorn (`trips/merge.py`) går igenom fordonets resor i tidsordning och jämför varje par
av på varandra följande resor:

| Stillestånd | Åtgärd |
| --- | --- |
| ≤ 15 min (regel A) | Automatisk sammanslagning |
| > 15 och ≤ 60 min (regel B) | Fråga (notis + granskningscenter). Obesvarad fråga = resorna förblir separata |
| > 60 min (regel C) | Endast manuell sammanslagning |

Gränserna är inställbara. Motorn körs om varje gång nya resor tillkommer, så en resa som
rapporteras i efterhand slås ihop retroaktivt. Dessutom gäller:

- **Originalen bevaras.** En sammanslagning skapar en ny journalresa; källresorna markeras
  som sammanslagna och återaktiveras om sammanslagningen ångras. Varje åtgärd loggas i
  `trip_merges`.
- **Ångrat är ångrat.** Ångrar du en automatisk sammanslagning, eller svarar nej, sparas
  beslutet så att motorn inte gör om det eller frågar igen.
- **Privat blir aldrig tjänst.** Har delresorna olika kategorier behåller varje delsträcka
  sin kategori, resan flaggas som blandad och kräver manuell fördelning.
- **Luckor döljs aldrig.** Saknas kilometer i mätarställningen mellan två resor slås de inte
  ihop automatiskt. En manuell sammanslagning kräver uttrycklig bekräftelse, luckans
  kilometer räknas inte in i resan, och luckan ligger kvar som egen post.
- **Okända tider.** Saknas faktisk starttid används gränserna: ryms hela intervallet inom
  regel A slås resorna ihop, annars ställs en fråga som anger intervallet. Helt okända tider
  lämnas för manuell hantering.
- **Överlapp** i tid eller mätarställning slås aldrig ihop utan blir ett avvikelseärende.

## Dataluckor

`data_gaps` beräknas ur de fysiska resornas mätarställningar och är oberoende av hur resorna
grupperats. En lucka kan lämnas öppen, markeras som medvetet oredovisad eller intygas som
privat körning – då skapas en sammanfattande privat post (utan påhittade tider) så att
mätarställningarna går att stämma av. Öppna luckor redovisas alltid i dashboard och export.

## Klassificering och inlärning

Se [AI-klassificering](ai-klassificering.md). I korthet: en deterministisk regelmotor ger
signaler och ett förslag; en valfri LLM får väga in ett avidentifierat underlag men kan
aldrig ensam ge automatisk klassning; AI-förslag (`trip_classifications`, `trip_purposes`,
`trip_evidence`) lagras skilt från användarens beslut; mönster räknas om från grunden ur
bekräftade beslut, så en ångrad korrigering försvinner helt ur inlärningen.

## Tid

All tid lagras i UTC och visas i `Europe/Stockholm` (inställbart). Importerade lokala
klockslag räknas om med hänsyn till sommar-/normaltid; klockslag som inte finns eller är
tvetydiga vid omställning flaggas på raden.

## Demoläge

Demodata lever på ett separat demofordon (`vehicles.is_demo`). Simulerade observationer går
genom exakt samma pipeline som riktiga, men blandas aldrig med verkliga fordon, märks
tydligt i gränssnittet och vattenmärks i exporten. En integration i demoläge har status
`demo` – aldrig `connected`.

## Förberett för framtiden

Datamodellen har redan användare, fordon och förare som egna entiteter med ägarskap på
varje rad, integrationer per användare och leverantör, samt utbytbara gränssnitt för
AI-leverantör, kalender och fordonsdata. Flera fordon stöds redan i API och gränssnitt.
Fler kalenderleverantörer, GPS-/OBD-källor och reseräkningsunderlag kan läggas till som nya
moduler utan att kärnan ändras.

## Databas

37 tabeller, versionshanterade med Alembic (`backend/alembic/versions`). JSON-fält lagras
som JSONB i PostgreSQL. Migrationerna körs automatiskt när API-containern startar.
