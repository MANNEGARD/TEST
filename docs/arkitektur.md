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
| `trips/` | Journalresor, segment, sammanslagning, dataluckor, tjänsteresor, granskningsärenden, uträknad starttid (`start_time.py`) |
| `places/` | Platsregister, adressnormalisering, geofence, förslag, geokodning mot OpenStreetMap (`geocode.py`) |
| `classify/` | Regelmotor, syftesförslag, AI-lager, fabriceringsskydd, självgående läge (`autopilot.py`) |
| `learning/` | Mönster, användarbeslut, ångra |
| `calendar/` | Microsoft Graph: OAuth, synk, matchning |
| `volvo/` | OAuth, API-klient, insamlare, resedetektering, demoläge |
| `ha/` | Notismotor, svarshantering, Home Assistant-klient, antagande- och månadsnotiser (`notices.py`), larm (`alerts.py`) |
| `stats/`, `export/` | Dashboard, milersättning, rapporter, avvikelser, Excel (enkel och detaljerad), månadsskiftet (`month_close.py`) och signerade nedladdningslänkar (`links.py`) |

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
(`start_earliest`, `start_latest`). Gränssnittet visar då "okänd" och exporten skriver bara
sluttiden ("–08:52"). Räcker bilens egna avläsningar räknas starttiden fram inom gränserna
(`estimated`, med grunden i `raw_trips.start_estimate_basis`); den visas med "≈" och
felmarginal och skrivs "ca 07:35" i exporten. En starttid som användaren anger sparas i
`logical_trips.start_time_override` och ger kvaliteten `exact`; den fysiska resan ändras
aldrig.

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
spårbarhet); användarens beslut flyttas över vid entydig träff. Motsvarar den rekonstruerade
resan flera importerade resor står ägarens tidigare beslut i klartext i ett ärende
(`IMPORT_PROBLEM`, `data.previous_category`/`previous_purpose`), och en öppen fråga i
telefonen om den ersatta resan avbryts direkt.

Vid ett uppehåll i hämtningen (1.3.0) sparas när hämtningen låg nere och hur många km mätaren
ökade under tiden (`raw_trips.collection_gap`: `{"from", "to", "odometer_km"}`). Texten byggs
av `volvo.detection.collection_gap_text` – lång i webben och i ärendet "Uppehåll i
hämtningen", kort i frågan i telefonen. En resa som kan vara flera körningar blir aldrig en
tyst preliminär klassning (`requires_review`), och ärendet om det försvinner inte för att
motorn föreslår privat – bara när ägaren själv (eller importen) har sagt privat.

## Sammanslagning

Regelmotorn (`trips/merge.py`) går igenom fordonets resor i tidsordning och jämför varje par
av på varandra följande resor. Det finns två regelverk (`merge.by_category`).

**Kategoriregler (1.3.0, standard för nya installationer).** Bara två *avgjorda* resor av samma
kategori slås ihop – avgjord av ägaren, en regel eller importen (godkänd eller importerad, en
kategori, inget oavgjort, inte bara antagen med vetotid). Tjänsteresor följer
`auto_max_minutes`/`ask_max_minutes`, privata resor `private_auto_max_minutes`/
`private_ask_max_minutes`. En tjänsteresa och en privat, eller en resa som inte är avgjord,
slås aldrig ihop automatiskt och ger ingen fråga. Stopp hemma eller vid en bekräftad privat
plats slås aldrig ihop. Samma regler i manuellt och självgående läge, utan särregel för
tankstopp. Eftersom kategorin måste vara känd körs sammanslagningen **efter** klassning,
resekedjor och autopilot i `pipeline.after_trips_changed`, och dessutom efter ägarens beslut:
i `feedback.after_decisions` och som ett eget steg i omräkningen i bakgrunden
(`learning.recompute`), direkt efter autopiloten. Frågor därifrån går till telefonen som i
pipelinen. När en befintlig installation går över sätts `merge.category_rules_since`; resor som
fanns före den slås aldrig ihop och ger inga frågor i efterhand. En sammanslagen resa delas
inte upp om kategorin ändras senare ("Dela upp" finns).

**Det tidigare regelverket** (befintliga installationer tills ägaren sparar sektionen; migreringen
0004 sätter `by_category = false`) körs först i pipelinen, som före 1.3.0:

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
- **Privata stopp.** Ett stopp hemma eller vid en bekräftad privat plats slås aldrig ihop när
  någon av resorna kommer från bilen, i något läge.
- **Självgående läge (tidigare regelverket).** För resor från bilen gäller bara: stopp på högst
  15 minuter vid en bekräftad tank- eller laddstation slås ihop. Inga frågor ställs. Platsens
  bekräftade typ avgör, aldrig kartans förslag.

## Ta bort resor

Importerade och manuellt inlagda resor kan tas bort (`trips/removal.py`, `DELETE /trips/{id}`
och `POST /trips/bulk-delete`). Borttagning är makulering: originalposten får `voided_at` och
skälet "Borttagen av användaren", journalresan blir `voided` och borttagningen står i
ändringshistoriken. Resor från bilen, sammanslagna resor och resor i en tjänsteresa nekas med en
begriplig text. I anropet stängs resans ärenden och frågor, kalenderträffarna tas bort och
resans inlärning ångras (egna regler finns kvar); luckor, mönster, förslag och övriga ärenden
räknas om i bakgrunden efter svaret, som efter ett beslut i Granskning. Importen jämför bara
mot resor som inte är makulerade, så en borttagen importerad resa kommer tillbaka – som den
står i filen och bedömd på nytt – när samma resa importeras igen (`summary.rows_restored`).

## Vad som redovisas: journalomfång

Inställningen `general.journal_scope` styr hur luckor i mätarställningen tolkas.

| Omfång | Betydelse |
| --- | --- |
| `business_only` (standard) | Egen bil med milersättning. Endast tjänsteresor redovisas. Kilometer mellan två redovisade resor är privat körning: inga fel, inga varningar, inga ärenden, och exporten innehåller bara tjänsteresor |
| `complete` | Fullständig körjournal, t.ex. förmånsbil. Alla resor redovisas och journalen stäms av mot mätarställningen; luckor är då något som ska redas ut |

`data_gaps` beräknas i båda lägena ur de fysiska resornas mätarställningar, oberoende av hur
resorna grupperats. I läget `complete` kan en lucka intygas som privat körning (en
sammanfattande privat post utan påhittade tider) eller lämnas som oavstämd, och öppna luckor
redovisas i den detaljerade exporten. I standardläget är luckorna bara information.

Oavsett läge slås två resor aldrig ihop automatiskt över en lucka: saknas kilometer mellan
dem har bilen körts däremellan.

## Klassificering och inlärning

Se [AI-klassificering](ai-klassificering.md). I korthet: en deterministisk regelmotor ger
signaler och ett förslag; en valfri LLM får väga in ett avidentifierat underlag men kan
aldrig ensam ge automatisk klassning; AI-förslag (`trip_classifications`, `trip_purposes`,
`trip_evidence`) lagras skilt från användarens beslut; mönster räknas om från grunden ur
bekräftade beslut, så en ångrad korrigering försvinner helt ur inlärningen.

## Självgående läge

Med `automation.mode = self_driving` (standard från 1.1.0) avgör `classify/autopilot.py`
live-resor som kommit in efter `automation.active_since`. Pipelinen kör den efter
resekedjorna och före granskningsärendena. Regeltabellen (egen regel, historik, hemresa,
pendling, privat plats och platsantagande) beskrivs i [Användning](anvandning.md#självgående-läge).
Ett automatiskt godkännande sparas med `approval_source`, `approval_basis` (skäl, regel,
relaterad resa och vetotid) och `auto_approved_at`. Det skapar ingen `LearningFeedback` och
räknas aldrig in i mönstren; ägarens beslut sätter `approval_source = user` (eller `bulk`).
Importerade och manuellt inlagda resor, resor från före `active_since` och läget `manual`
beter sig som tidigare.

Schemaläggarens `tick` kör i ordning: `autopilot.ensure_active_since`, Volvo-hämtning,
demobilen, väckning av uppskjutna ärenden, geokodning (`places.geocode.run_pending`, med HTTP
utan öppen databastransaktion: först adresser för bilens stopp och bostaden, sedan positioner
för platser som bara har en adress, högst tre per användare och körning, inom samma
dygnsgräns), frågor i telefonen (`ha.notifier.ensure_open_questions`),
leverans, schemalagda notiser, larm (`ha.alerts.run`) och månadsskiftet
(`export.month_close.run_due`). Varje steg har egen session, och ett fel i ett steg stoppar
inte de andra. Geokodningssvar cachas per ruta i `geocode_cache`; månadsskiftets läge
lagras i `month_closes`.

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

40 tabeller, versionshanterade med Alembic (`backend/alembic/versions`). JSON-fält lagras
som JSONB i PostgreSQL. Migrationerna körs automatiskt när API-containern startar.

## Enprocessläge

Används när DriveLog körs i **en enda container**: Home Assistant-tillägget (och lokalt
`scripts/dev.sh`). Det slås på med `DRIVELOG_JOBS_EAGER=true` och kombineras i drift med
en SQLite-fil och tom `DRIVELOG_REDIS_URL`.

Skillnader mot Compose-driften:

| | Compose (egen server) | Enprocessläge (tillägget) |
| --- | --- | --- |
| Databas | PostgreSQL | SQLite-fil (WAL, `busy_timeout` 30 s, `foreign_keys=ON`, `synchronous=NORMAL`) |
| Bakgrundsjobb | Celery worker via Redis | Körs direkt i API-processen, i den tråd som begär dem |
| Schema (tick varje minut, kalender var 15:e minut, underhåll 02:17) | Celery beat | Bakgrundstråd i API-processen (`app/scheduler.py`) |
| Inloggningsspärr | Redis | Minnet |
| Säkerhetskopia av databasen | `pg_dump` (`deploy/backup.sh`) | `python -m app.cli sqlite-backup <målfil>` |

Migrationerna (`alembic upgrade head`) gäller i båda lägena; `init_schema` körs aldrig i
produktion. Ett jobb som köas medan den anropande tråden har en öppen skrivtransaktion
skjuts upp tills transaktionen är avslutad (`app/jobs.py`), så att det aldrig väntar på ett
lås som tråden själv håller. Det nattliga underhållet styrs av klockslaget och tas igen
strax efter start om det inte körts på mer än ett dygn. Vid start loggas läget
("Enprocessläge: SQLite, jobb och schemaläggare i API-processen") och
`/api/v1/health/ready` visar `queue: eager` och schemaläggarens status – Redis krävs inte.

Begränsningar: **exakt en API-process** (uvicorn utan `--workers`; schemaläggaren och
inloggningsspärren finns bara i den processen) och SQLite tillåter en skrivare åt gången.
Det räcker gott för en användare med en eller ett par bilar, men läget är inte avsett för
många samtidiga användare. Långa jobb (t.ex. AI-klassificering) körs i anropet och gör att
just det anropet tar längre tid.
