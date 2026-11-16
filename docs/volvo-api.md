# Volvo Cars-integrationen

DriveLog kan läsa data från din Volvo via Volvo Cars officiella utvecklar-API:er och
rekonstruera resor ur dem. Integrationen är **endast läsande** – DriveLog skickar aldrig
kommandon till bilen (lås, klimat m.m.).

> Uppgifterna nedan är kontrollerade mot developer.volvocars.com i november 2026.
> Volvo ändrar ofta villkor och endpoints – kontrollera alltid utvecklarportalen.

## Kort sammanfattning

| | |
|---|---|
| API:er som används | Connected Vehicle v2, Location v1, Energy v2 |
| Inloggning | OAuth 2.0 authorization code + PKCE (S256) med Volvo ID |
| Resejournal i API:t | **Finns inte** – resor rekonstrueras ur mätarställning och position |
| Position | Uppdateras först **när en resa avslutats** – ingen live-position |
| Förbrukning per resa | **Finns inte i API:t** – DriveLog räknar fram en uppskattning ur trippmätaren, bilens snittförbrukning och tanknivån (1.3.0) |
| Gränser | 100 anrop/minut per Volvo ID och klient, 10 000 anrop/dygn per API |
| Behörighetens livslängd | Max ca 3 månader – därefter måste du logga in igen |
| Viktigt datum | Gamla Volvo ID-servern (`volvoid.eu.volvocars.com`) slutar fungera **2027-01-31** |

## 1. Vad du behöver

1. **Ett Volvo ID** – samma konto som i Volvo Cars-appen, och bilen ska vara kopplad
   till kontot (du som ägare/huvudanvändare).
2. **Ett utvecklarkonto** på <https://developer.volvocars.com> (logga in med ditt Volvo ID
   och godkänn utvecklarvillkoren).
3. **En bil som stöds**: bilar med Volvo On Call (ungefär årsmodell 2010–2024) eller
   Google built-in (årsmodell 2020 och senare). Location-API:t fungerar endast i EMEA.
   Äldre "klassiska" laddhybrider (t.ex. V90 T8 MY2021 utan Google) har begränsat stöd
   i Energy-API:t.
4. En DriveLog-installation som nås på en fast adress (`DRIVELOG_BASE_URL`), eftersom
   Volvo skickar tillbaka webbläsaren dit efter inloggningen.

## 2. Registrera och publicera applikationen

1. Gå till *Your apps* i utvecklarportalen och skapa en ny applikation.
2. Notera **VCC API Key** (primär nyckel) – den skickas i headern `vcc-api-key`.
3. Publicera applikationen med **Dynamic App Publish** (självbetjäning). Vid publiceringen
   anger du:
   * **Redirect URI** – exakt:
     ```
     {DRIVELOG_BASE_URL}/api/v1/integrations/volvo/callback
     ```
     t.ex. `https://drivelog.example.se/api/v1/integrations/volvo/callback`.
     DriveLog visar den exakta adressen på integrationssidan (fältet `redirect_uri` i
     `GET /api/v1/integrations/volvo/status`). Adressen måste matcha tecken för tecken.
   * **Scopes** – kryssa i följande (endast läsbehörigheter):

     | Scope | Används till |
     |---|---|
     | `openid` | Inloggning (krävs av Location-API:t) |
     | `conve:vehicle_relation` | Lista bilarna på kontot, modell/årsmodell |
     | `conve:odometer_status` | Mätarställning |
     | `conve:fuel_status` | Tanknivå |
     | `conve:battery_charge_level` | Batterinivå |
     | `conve:trip_statistics` | Bilens snittförbrukning, trippmätare, räckvidd |
     | `conve:engine_status` | Motor igång/avstängd |
     | `conve:diagnostics_workshop` | Serviceinformation (provas, pollas inte) |
     | `conve:command_accessibility` | Om bilen är uppkopplad (provas, pollas inte) |
     | `location:read` | Senast rapporterad position |
     | `energy:state:read` | Laddstatus och elräckvidd |
     | `energy:capability:read` | Vilka energivärden bilen stöder |

     Saknas ett scope fungerar resten ändå; DriveLog visar den endpointen som "stöds inte".
4. Efter publiceringen får du **Client ID** (nya klienter börjar med `vid-dc-`) och
   **Client secret**.

## 3. Anslutningsflödet i DriveLog

1. **Integrationer → Volvo**: fyll i Client ID, Client secret och VCC API-nyckel
   (`PUT /credentials`). Hemligheterna krypteras direkt (Fernet) och visas aldrig igen –
   gränssnittet ser bara *att* de finns. Lämnar du ett hemligt fält tomt behålls det sparade
   värdet. Inloggningsserver kan väljas: `new` (standard, `auth.volvocars.com`) eller
   `legacy` (gamla servern, endast fram till 2027-01-31).
2. **Anslut med Volvo ID** (`POST /connect`): DriveLog skapar ett engångs-`state` och en
   PKCE-verifierare (S256), giltiga i 10 minuter, och skickar dig till Volvos inloggning.
   Endast en hash av `state` sparas; verifieraren lagras krypterad.
3. Volvo skickar tillbaka webbläsaren till `/callback`. DriveLog kontrollerar `state`
   (engångs, rätt användare, inte för gammalt) och byter koden mot tokens med HTTP Basic
   (`client_id:client_secret`). Du hamnar på `/integrations?volvo=connected` eller
   `/integrations?volvo=error&reason=…` (t.ex. `invalid_state`, `access_denied`,
   `invalid_client`).
4. DriveLog hämtar bilarna på kontot. **Välj bil** (`POST /select-vehicle`) och vilket
   fordon i DriveLog den motsvarar. VIN visas maskerat (`YV1•••••••••1234`).
   Därefter provas varje endpoint en gång, och resultatet visas per bil:
   *stöds*, *stöds inte* (403/404), *ingen data* (t.ex. ingen position) eller *okänt*.
5. Insamlingen sker sedan automatiskt i bakgrunden (`collector.poll_due`), som standard var
   5:e minut (inställningen `volvo.poll_interval_seconds`, minst 120 s). `POST /sync`
   hämtar direkt.
6. **Koppla bort** (`POST /disconnect`) raderar alla hemligheter och tokens. Fordon,
   observationer och resor finns kvar.

### Tokens

* Access token förnyas automatiskt strax innan `expires_in` löper ut (ofta 3599 s men kan
  vara 300 s). Refresh tokens **roterar**: den nya sparas och committas omedelbart.
* Ett `401` från API:t ger exakt en förnyelse och ett nytt försök.
* `invalid_grant` (behörigheten återkallad eller äldre än ca 3 månader) ger status
  **needs_reauth** – logga in igen. DriveLog visar när nästa omloggning senast behövs
  (`reauth_due_at`).

## 4. Testtoken-läge

I utvecklarportalen kan du skapa en kortlivad **testtoken** (för en demobil eller "My Volvo
car"). Den kan klistras in i DriveLog (`POST /test-token`) tillsammans med VCC API-nyckeln
för att prova integrationen utan att publicera en app.

* Läget är tydligt märkt i gränssnittet (`test_token_mode`).
* Ingen automatisk förnyelse – när token går ut blir statusen *needs_reauth*.
* Tokenens livslängd är inte dokumenterad av Volvo; räkna med minuter till någon timme.
* Status blir *ansluten* först när ett riktigt anrop (fordonslistan) har lyckats.

## 5. Endpoints som används

Bas-URL `https://api.volvocars.com`. Headers: `authorization: Bearer …`,
`vcc-api-key: …`, `accept: application/json`.

| Endpoint | Pollas | Innehåll |
|---|---|---|
| `GET /connected-vehicle/v2/vehicles` | vid anslutning | Bilar på kontot |
| `GET /connected-vehicle/v2/vehicles/{vin}` | vid anslutning | Modell, årsmodell, drivmedel, regnr (kan saknas) |
| `…/vehicles/{vin}/odometer` | ja | Mätarställning (heltal km) |
| `/location/v1/vehicles/{vin}/location` | ja | GeoJSON-punkt `[lon, lat, höjd]` + tidsstämpel |
| `…/vehicles/{vin}/fuel` | ja | Tanknivå (l), batterinivå (%) |
| `…/vehicles/{vin}/statistics` | ja | Snittförbrukning, trippmätare (`tripMeterAutomatic`), snittfart (`averageSpeedAutomatic`, sparas från 1.1.0), räckvidd |
| `…/vehicles/{vin}/engine-status` | ja | `RUNNING` / `STOPPED` |
| `/energy/v2/vehicles/{vin}/state` | ja | Batterinivå, elräckvidd, laddstatus |
| `/energy/v2/vehicles/{vin}/capabilities` | provas | Vilka energivärden som stöds |
| `…/vehicles/{vin}/diagnostics`, `…/command-accessibility` | provas | Service, uppkoppling |

Värden kan komma som tal **eller** sträng (t.ex. `"heading": "129"`) – DriveLog tolkar båda.

### Felhantering och gränser

* **Rate limiting på klientsidan**: högst 90 anrop per glidande minut (gränsen är 100) och
  en dygnsbudget på 9 000 anrop per API (gränsen är 10 000). Förbrukningen visas i
  `status.quota`.
* **429**: inget omedelbart nytt försök. Insamlingen pausas (`backoff_until`) med
  exponentiell backoff + slumpmässig spridning (60 s, 120 s, 240 s … max 1 h);
  `Retry-After` respekteras om Volvo skickar den (det är inte dokumenterat). Under pausen
  görs inga anrop alls.
* **5xx och nätverksfel**: högst två nya försök med backoff och jitter.
* **403/404** på en enskild endpoint betyder att bilen/behörigheten saknar stöd – det är ett
  kapabilitetsbesked, inte ett fel. Endpointen provas igen efter 24 timmar. En endpoint som
  tidigare har fungerat räknas som "stöds inte" först efter tre sådana svar i rad, så att ett
  tillfälligt fel hos Volvo inte stänger av insamlingen.
  `404` från Location betyder "ingen position".
* Tokens, API-nyckel och VIN loggas aldrig (VIN maskeras även i HTTP-bibliotekets loggrader
  och tas bort ur lagrade råsvar).

## 6. Hur resor rekonstrueras

Varje hämtning blir en **observation** med både hämtningstid och bilens egna tidsstämplar
per värde. Identiskt innehåll ger ingen ny rad (bara `last_seen_at`/`seen_count`
uppdateras). Ändrad mätarställning sparas också som mätarställningshistorik (källa `api`).

Resedetekteringen (`detection.process_vehicle`) är en tillståndsmaskin:

* **Ankare** = senaste stabila läge. **Rörelse** = mätarställningen ökat minst
  `min_odometer_delta_km` (1 km) eller positionen flyttats minst `position_threshold_m`
  (150 m).
* Resan anses avslutad när läget varit oförändrat i `settle_minutes` (10 min) – eller när
  bilen rapporterat avstängd motor efter en ny positionsrapport, bekräftat av en till hämtning.
  Har bara mätarställningen eller bara positionen hunnit ändras väntar systemet tre gånger så
  länge (30 min), eftersom det andra värdet ofta kommer med fördröjning – annars skulle samma
  körning kunna bli två resor.
* Varje ny positionsrapport inom en rörelse delar upp den i delresor.
* **Sträcka** = skillnad i mätarställning (`distance_quality = odometer`).
* **Sluttid** = bilens positionstidsstämpel (`reported`) om den är rimlig, annars en märkt
  uppskattning (`estimated`/`bounded`).
* **Starttid hittas aldrig på.** Gränserna `start_earliest`–`start_latest` räknas alltid
  fram. Räcker bilens egna avläsningar räknas starttiden fram inom dem
  (`start_time_quality = estimated`, grunden i `start_estimate_basis`):
  * Senaste möjliga start snävas in av en rapport om att motorn går och av första
    mätarökningen. En rapport om avstängd motor används aldrig (laddhybriden kan köra på el).
  * Två mätaravläsningar under färd med 10–130 km/h, när fönstret är högst 15 minuter:
    starten räknas bakåt från farten. Marginalen är halva fönstret.
  * Annars, när fönstret är högst 10 minuter: fönstrets mitt, med högst 5 minuters marginal.
  * Bilens trippmätare och snittfart (`tripMeterAutomatic`, `averageSpeedAutomatic`) är byggt
    som en tredje väg men **avstängt** tills det har prövats mot en riktig bil att snittfarten
    räknas över förfluten tid och att trippmätaren nollställs vid resans start.
  * Tiden avrundas till hel minut och ligger alltid inom gränserna. Räcker inget av detta
    lämnas `started_at` tomt (`start_time_quality = bounded`).

  Appen visar en uträknad tid som "≈ 07:35 (±4 min)" och underlaget som "ca 07:35". En tid
  du själv anger märks "angiven av dig".
* **Möjliga missade resor** flaggas (`possible_missed_trips`, ofullständig resa och ett
  granskningsärende) när mätarsträckan är längre än `detour_factor` × fågelväg +
  `detour_slack_km`, när mätaren ökat men positionen är oförändrad (rundresa), eller efter
  ett insamlingsuppehåll längre än `max_unobserved_minutes`.
* **Uppehåll i hämtningen** (1.3.0): ligger hämtningen nere (Volvos API, nätet, servern) medan
  bilen kör, ser DriveLog bara att mätaren har ökat när hämtningen är tillbaka. Resan flaggas
  `collection_gap`, och när hämtningen låg nere och hur många km mätaren ökade sparas i
  `raw_trips.collection_gap` (`{"from": …, "to": …, "odometer_km": 95.0}`; `TripOut.collection_gap`
  i API:t). Resan får aldrig ett automatiskt beslut; webben, ärendet "Uppehåll i hämtningen"
  och frågan i telefonen säger till exempel "Hämtningen från Volvo låg nere mellan mån 5/10
  08:10 och 12:40. Under tiden ökade mätaren med 95 km, så resan kan vara flera körningar."
  Lösningen är att importera Volvos körjournal för dagen: de importerade originalresorna
  ersätter den rekonstruerade resan utan dubbletter (eller dela upp kilometrarna i tjänst och
  privat). Larmet "Hämtningen från Volvo fungerar inte" (efter 6 timmar) påminner om samma sak.
* **Bränsle och el per resa** (1.3.0, `services/volvo/consumption.py`) räknas fram ur bilens
  egna avläsningar och märks som uppskattade (`estimated`, "≈" i appen):
  1. *Trippmätare × snitt:* avläsningen före och efter resan, trippmätaren har inte nollställts
     emellan och har ökat ungefär lika mycket som resan är lång (±2 km eller 15 %).
     Förbrukningen är `(snitt_efter × tripp_efter − snitt_före × tripp_före) / 100` – så får två
     resor i samma trippmätarperiod var sin del.
  2. *Nollställd trippmätare* (efter ca 4 timmars stillestånd): trippmätaren efter resan ≈
     resans längd → `snitt × tripp / 100`.
  3. *Nivåskillnad i tanken* när trippmätaren inte räcker. En ökning på mer än 1 l mellan två
     avläsningar under resan är en tankning, och då lämnas värdet okänt. Batteriets nivå används
     inte, eftersom den användbara kapaciteten inte är känd.
  Orimliga värden (negativa, mer än 100 l, mer än 60 l eller 60 kWh per 100 km) kastas. Räcker
  inget lämnas värdet okänt (`unknown`, "–"). Bara resor som DriveLog har skapat ur bilens
  avläsningar de senaste 14 dygnen räknas; importerade resor och värden som redan finns rörs
  aldrig, och inte heller resor i en månad vars underlag redan är skapat.
* **Felsökningsfil** (1.3.0): *Ladda ner felsökningsfil* under *Integrationer → Volvo*
  (`GET /api/v1/volvo/diagnostics?days=14`, 1–31 dygn) ger bilens avläsningar, hämtningarnas
  utfall (status och HTTP-kod) och de resor som byggdes av dem som JSON – utan VIN,
  registreringsnummer, positioner, adresser, platsnamn, rådata, feltexter och nycklar. Id:n
  är löpnummer (`obs-1`, `trip-1`). Filen är till för den som hjälper dig felsöka.
* **Platsen:** ligger stoppet vid en känd plats används den. Annars skapas en ny obekräftad
  plats (radie 75 m) vars adress slås upp i OpenStreetMap i bakgrunden – aldrig under själva
  detekteringen. Se [Användning → Adresser](anvandning.md#adresser).
* Nya resor blir oavgjorda journalresor och går genom samma efterbearbetning som övriga resor
  (sammanslagning, luckor, klassificering, notiser). I självgående läge avgörs tydliga resor
  av sig själva och resten blir en fråga, se [Användning → Självgående
  läge](anvandning.md#självgående-läge).
* Föråldrade uppgifter (äldre än `stale_after_minutes`, standard 24 h) flaggas med ett
  granskningsärende ("Fordonet har inte rapporterat nya uppgifter").

När en **importerad Volvo-körjournal** senare täcker samma mätarintervall ersätts den
rekonstruerade resan (`detection.reconcile_with_imports`): den makuleras ("Ersatt av
importerad originalresa"), användarens beslut flyttas över vid 1:1-träff om
originalresan saknar eget beslut, platserna lär sig API-koordinaterna, och oklara fall
blir granskningsärenden.

## 7. Begränsningar

* **Positionen uppdateras först vid resans slut.** Det går inte att följa bilen under färd.
  Starttiden kan bara räknas fram när mätarställning eller motorstatus har rapporterats under
  färden; annars är den okänd.
* **Ingen resejournal** i de publika API:erna. Volvo-appens körjournal kan bara fås via
  export/import.
* **Ingen förbrukning per resa i API:t.** DriveLog uppskattar den ur trippmätaren och bilens
  snitt (se ovan). Tankningar och laddningar mellan hämtningar gör nivåskillnader opålitliga,
  och elförbrukningen kan bara räknas när bilen rapporterar ett elsnitt.
* Korta resor (< 1 km) kan missas p.g.a. mätarställningens heltalsupplösning.
* Två resor tätt efter varandra utan ny positionsrapport kan se ut som en enda resa
  (flaggas som möjliga missade resor).
* 100 anrop/minut och 10 000 anrop/dygn per API. Med standardintervallet (5 min, ca 6 anrop
  per hämtning) går det åt ca 1 700 anrop/dygn per bil.
* Behörigheten gäller max ca 3 månader – logga in igen när DriveLog ber om det.
* Gamla Volvo ID-servern stängs **2027-01-31**. Använd inloggningsservern `new` och ett
  Client ID som börjar med `vid-dc-`.
* Energy v1 stängdes 2026-02-20 och Extended Vehicle API 2025-12-31; de används inte.

## 8. Inte verifierat utan riktig API-åtkomst

Följande är byggt enligt dokumentationen men **inte prövat mot en riktig bil**:

* Testtokens exakta livslängd och vilka scopes de har.
* Om läsanrop väcker bilen eller påverkar 12 V-batteriet.
* Vad `engine-status` rapporterar vid ren eldrift i en laddhybrid (DriveLog litar därför
  inte enbart på motorstatus för att avsluta en resa).
* Exakt vilka fält en V90 T8 MY2021 ("classic", utan Google) ger, och hur ofta mätarställningen
  uppdateras under färd.
* Om ett Volvo On Call-abonnemang krävs.
* Om token-endpointens felkoder och `scope` i tokensvaret ser ut exakt som i dokumentationen,
  samt om `Retry-After` skickas vid 429.
* Tidsstämplarnas noggrannhet (bilens klocka kontra serverns).
* Om `averageSpeedAutomatic` räknas över förfluten tid och om `tripMeterAutomatic` nollställs
  vid resans start. Därför är starttid ur trippmätaren fortfarande avstängd i 1.3.0. För att slå
  på den behövs verkliga avläsningsserier från bilen (minst 20 resor med känd starttid, där
  snittfarten och trippmätaren före och efter resan jämförs med den kända starttiden) och ett
  test som visar att felet är högst 5 minuter. Felsökningsfilen innehåller just de avläsningarna.
* Förbrukningens metoder mot en riktig bil: att `averageFuelConsumption` och
  `averageEnergyConsumption` avser trippmätarens sträcka, när trippmätaren nollställs, och hur
  ofta statistiken uppdateras. Uppskattningen är prövad på påhittade avläsningar och demobilen.

## 9. Demoläge

Demoläget visar hela kedjan utan Volvo-konto:

* `POST /demo/enable` skapar ett **eget demofordon** ("Demobil (påhittade data)",
  `is_demo`) och påhittade platser (Demostad, Provköping m.fl.). Inga riktiga uppgifter
  används eller skrivs över.
* En simulerad bil svarar med Volvo-formade JSON-svar som går genom **exakt samma pipeline**
  som en riktig bil: observation → resedetektering → journalresa → granskning/notiser.
* `POST /demo/simulate` kör ett scenario: `commute`, `short_stop` (slås ihop automatiskt),
  `customer_visit` (fråga om sammanslagning), `silent` (okänd starttid) eller `round_trip`
  (flaggas som möjlig datalucka). Utan scenario roteras de.
* Statusen visas som **demo** endast om ingen riktig anslutning finns. En riktig integration
  visas aldrig som ansluten på grund av demodata, och demot förbrukar ingen anropskvot.
* `POST /demo/disable` raderar **all** demodata: demofordonet, observationer, resor,
  granskningsärenden, notiser och påhittade platser.
* Bakgrundsjobbet `demo.tick` håller demobilens synk levande.

## 10. För utvecklare

Publika funktioner som bakgrundsjobb och importflöde anropar:

```python
from app.services.volvo import collector, demo, detection

collector.poll_due(db, now=None)                 # pollar alla anslutna bilar vars intervall löpt ut; committar per bil
collector.poll_vehicle(db, vehicle, now=None)     # en bil, nu (anroparen committar)
demo.tick(db, now=None)                           # demobilarnas synk; committar per användare
detection.reconcile_with_imports(db, vehicle, actor=actor)
# → därefter pipeline.after_trips_changed(...) och db.commit() hos anroparen
```

HTTP-klienterna tar en injicerbar `transport` (t.ex. `httpx.MockTransport`) för tester.
