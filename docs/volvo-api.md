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
     | `conve:command_accessibility` | Om bilen är tillgänglig eller används (hämtas från 1.5.0) |
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
| `…/vehicles/{vin}/command-accessibility` | ja (från 1.5.0) | Tillgänglighet: `availabilityStatus` och vid `UNAVAILABLE` en orsak (`unavailableReason`, t.ex. `CAR_IN_USE`) – se "Bilen används" nedan |
| `…/vehicles/{vin}/diagnostics` | provas | Service |

Värden kan komma som tal **eller** sträng (t.ex. `"heading": "129"`) – DriveLog tolkar båda.

### Bilen används (command-accessibility, 1.5.0)

Ändpunkten hämtas vid varje hämtning, sist av alla. Svaret tolkas enligt Volvos
dokumenterade form `{"data": {"availabilityStatus": {"value", "unavailableReason",
"timestamp"}}}` (orsaken läses också om den ligger bredvid statusen) och sparas på
avläsningen (`vehicle_observations`):

| Kolumn | Innehåll |
|---|---|
| `accessibility_status` | Statusen som den kom (`AVAILABLE`, `UNAVAILABLE`, `UNSPECIFIED` eller annat) |
| `accessibility_reason` | Orsaken som den kom (t.ex. `CAR_IN_USE`, `NO_INTERNET`, `POWER_SAVING_MODE`) |
| `accessibility_reported_at` | Bilens egen tidsstämpel för statusen |
| `car_in_use` | Härlett: sant bara för `UNAVAILABLE` med `CAR_IN_USE`; falskt för andra lästa värden (okända räknas inte som "används"); tomt när ingen status lästes |

* Status och orsak ingår i avläsningens innehållshash: en ändrad status ger en ny
  avläsningsrad, vars hämtningstid (`fetched_at`) visar när övergången sågs. Bilens
  tidsstämpel ingår inte i hashen (en ny tidsstämpel med samma status ger ingen ny rad).
  En rad där bara statusen har ändrats är ingen rörelse för resedetekteringen.
* Statusen läses och sparas, men **ingenting är sett från den riktiga bilen** – varken om den
  svarar `CAR_IN_USE` under färd eller vad den svarar när den står. Därför får statusen bara
  förfina starttiden när den slår om i samma hämtning som motorstarten (se "Starttid ur
  motorstarten" nedan). Den sätter **aldrig ensam** en starttid och snävar aldrig ensam in
  gränserna, inte heller för en resa där motorn aldrig går (ren el). Felsökningsfilen
  (`GET /api/v1/volvo/diagnostics`) visar de fyra fälten för varje avläsning, så att det går
  att se vad bilen faktiskt svarar innan statusen får göra mer.
* Kvoten: ett anrop till per hämtning mot Connected Vehicle (vid 120 s ca 3 600 anrop/dygn
  med de fem ändpunkterna, budget 9 000).
* 403/404 gör att ändpunkten slutar hämtas på samma sätt som övriga (omprovning efter ett
  dygn). Ett fel i just den här ändpunkten (även 401) gör aldrig att hämtningen räknas som
  misslyckad eller att anslutningen kräver ny inloggning, och ett svar bara från den ger
  ingen avläsningsrad. Ett 429 här pausar dock hela insamlingen, eftersom gränsen gäller
  hela API:t.
* Ett tillfälligt fel (fel eller 429) i ändpunkten när allt annat är oförändrat räknas som
  en dubblett av förra raden: ingen extra rad, men förra radens `last_seen_at` flyttas fram
  även över den hämtning där statusen inte lästes. Ändrades något annat samtidigt blir det
  en ny rad där statusen står som okänd (den förs aldrig över). En ändrad status syns
  alltså med säkerhet först på den rad där den lästes.
* Demobilen svarar `UNAVAILABLE`/`CAR_IN_USE` medan motorn går och `AVAILABLE` annars
  (påhittat förlopp).

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
* **Ankomst med kompletta uppgifter stängs direkt (1.5.0).** Många bilar (bland dem ägarens)
  rapporterar mätarställning och position bara när tändningen slås av. Kommer en ny
  positionsrapport (flyttad position) och är mätarvärdet avläst vid ankomsten – bilens
  tidsstämpel för mätarvärdet högst 2 minuter från positionens – och är mätarsträckan minst
  fågelvägen (1 km marginal för heltalsmätaren), stängs resan vid den hämtning som först ser
  ankomsten. Motorstatus och "sedd två gånger" krävs inte. En mätarökning efter ankomsten
  räknas bara med om bilen inte har rapporterat en ny motorstart som värdet kan vara avläst
  efter. Det gäller också mycket korta resor där mätaren inte hann öka (sträckan uppskattas då
  ur fågelvägen och märks).
* Annars gäller väntan från före 1.5.0 oförändrad: resan anses avslutad när läget varit
  oförändrat i `settle_minutes` (10 min) – eller när bilen rapporterat avstängd motor efter en
  ny positionsrapport, bekräftat av en till hämtning. Har bara mätarställningen eller bara
  positionen hunnit ändras väntar systemet tre gånger så länge (30 min), eftersom det andra
  värdet ofta kommer med fördröjning – annars skulle samma körning kunna bli två resor. Den
  nya motorn stänger aldrig en rörelse med ankomst senare än den gamla väntan hade gjort.
* **Släpande mätarvärde efter att resan stängts (1.5.0).** Kommer ett högre mätarvärde utan ny
  positionsrapport och utan ny motorstart, och är det avläst senast vid ankomsten (eller, utan
  tidsstämpel, högst 2 km medan den gamla väntan ännu inte hade stängt), förs det till den redan
  skapade resan: slutmätarställning och sträcka rättas (även på en klassad, godkänd eller
  sammanslagen resa – beslutet står kvar), och rättningen syns i resans historik som *Resan
  ändrades*. Ingår resan i en månad vars underlag redan är skapat märker underlagets avtryck
  ändringen som vilken annan ändring som helst. Samma sak gäller när mätarsträckan vid
  ankomsten var kortare än fågelvägen (mätarvärdet bevisligen gammalt) och ökningen ryms inom
  omvägsgränsen. Annars blir ökningen som förut en egen rörelse. På samma sätt förs en position
  som kommer efter att en resa utan ankomst ("rundresa") har stängts till den resan, när
  positionens tidsstämpel ligger inom 2 minuter från resans sluttid och bilen inte har startat
  igen. Så räknas samma körning aldrig två gånger.
* **Frågan till telefonen kommer inte tidigare än förut:** tidigast när ankomsten först setts
  plus `settle_minutes`, så att en fortsatt körning efter ett kort stopp hinner slås ihop
  först. Lunchregelns egen väntan gäller som förut.
* Varje ny positionsrapport inom en rörelse delar upp den i delresor.
* **Sträcka** = skillnad i mätarställning (`distance_quality = odometer`).
* **Sluttid** = bilens positionstidsstämpel (`reported`) om den är rimlig, annars en märkt
  uppskattning (`estimated`/`bounded`).
* **Starttid ur motorstarten (1.5.0).** Bilen rapporterar "motorn igång" direkt, med egen
  tidsstämpel, även när mätarställning och position bara rapporteras när tändningen slås av.
  Efterbearbetningen (`start_time.fill_from_engine`, steget `pipeline.start_engine`, före
  uppskattningen ur sträckan) letar för resor med okänd eller uppskattad starttid upp den
  **sista** övergången från en avläsning med motorn av till en med motorn igång
  före ankomsten, utan att bilen setts med motorn av emellan och efter resans tidigaste gräns.
  * Ligger bilens tidsstämpel (`engine_reported_at`) mellan de två hämtningarna (±2 minuters
    klockskillnad) blir den starttiden, avrundad till hel minut (metod `engine_start`).
  * Annars, när hämtningarna ligger högst 6 minuter isär (hämtning var 2:a–5:e minut): minuten
    mitt emellan dem (metod `poll_midpoint`, högst 3 minuter fel). Den är uträknad och märks
    `estimated` med felmarginal (visas med "≈", "ca" i underlaget); bara bilens tidsstämpel
    märks `reported`. Avläsningar där motorstatusen inte kunde läsas räknas inte.
  * Längre fönster: ingen starttid, men `start_latest` snävas in till den första avläsningen
    med motorn igång.
  * Statusen "bilen används" (`car_in_use`): slår den om i **samma hämtning** som motorstarten
    (avläsningen vid övergången visar både motorn igång och `CAR_IN_USE`) blir den tidigaste
    av bilens två tidsstämplar inom fönstret starttiden, fortfarande metod `engine_start` och
    kvalitet `reported`. Grunden anger då `start_signal` (`engine`, `car_in_use` eller
    `engine_and_car_in_use`) och `in_use_reported_at`, och appen skriver "Bilen rapporterade
    själv att den började användas 06:14" när statusens tid gav starttiden. I alla andra fall
    lämnas statusen och resultatet blir exakt detsamma som utan den; grunden får
    `status_ignored` och felsökningsfilen steget `car_in_use_ignored_<kod>`:
    `no_engine_start` (statusen slog om men motorn gick aldrig före ankomsten, t.ex. hela
    resan på el – ingen starttid ur statusen), `engine_later` (statusen slog om en eller flera
    hämtningar före motorstarten – motorstarten räknas, eftersom en bil som stått med statusen
    påslagen inte går att skilja från en start på el) och `stop` (motorn rapporterades
    avstängd med ny tidsstämpel medan statusen låg kvar). Ingenting av detta är prövat mot den
    riktiga bilen.
  * Rimlighet: tiden ligger aldrig före den tidigaste gränsen eller efter ankomsten, och inte
    på eller efter senaste möjliga start (t.ex. första mätarökningen). Snittfarten från
    motorstarten får vara högst dubbla tabellfarten för sträckan, aldrig över 130 km/h (under
    3 km 50 km/h, under 10 km 70, under 30 km 100, därefter 130). En laddhybrid som startar på
    el kan rapportera motorstarten en bit in i resan; en snabbare tid används därför bara som
    senaste möjliga start, och uppskattningen ur sträckan tar vid.
  * Tiden får kvalitet `reported` (bilens egen tid: utan "≈" i appen och utan "ca" i
    underlaget) och grunden sparas i `start_estimate_basis` (metod, de två hämtningarna,
    bilens tidsstämpel, fönstrets längd och snittfarten). Den räknas som känd av kalendern,
    pendlingskedjan och AI-underlaget.
  * Den ligger fast: steget rör aldrig en resa som redan har en sådan tid, och varken
    uppskattningen ur sträckan, omräkningar eller sammanslagningar ändrar den. Bara ägarens
    angivna starttid gäller före; en import av Volvos journal ersätter resan som förut.
  * I efterhand: resor de senaste 14 dygnen (räknat från senaste avläsningen) med okänd eller
    uppskattad starttid rättas när avläsningarna finns kvar, med en rad i resans historik
    (`trip.start_time_engine`). Aldrig resor där ägaren angett starttiden, importerade resor
    eller resor som finns i ett underlag som redan är skapat (eller som skulle flytta till en
    annan månad med ett skapat underlag). Från 1.5.0 är en resa skyddad bara om den finns i
    filen: månaden har ett skapat underlag (`exported`/`stale`) och delresan skapades
    (`raw_trips.created_at`) senast när filen skapades (`export_jobs.finished_at`, som sätts efter
    att raderna lästs, annars `created_at`; okänt → skyddad).
    En resa som tillkommit efter filen – t.ex. när månadens fil skapats i förväg – får starttid
    och förbrukning som vanligt, och månaden blir inaktuell som vid varje ny resa
    (`month_close.protection`, samma regel för K4 och förbrukningen).
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
* **Bränsle och el per resa** (1.3.0, 1.5.0, `services/volvo/consumption.py`) räknas fram ur
  bilens egna avläsningar och märks som uppskattade (`estimated`, "≈" i appen). Från 1.5.0
  räknas varje metod som går att använda fram med både ett värde och en **felgräns**, och den
  med minst felgräns väljs:
  1. *Automatiska trippmätaren × snitt:* avläsningen före och efter resan, trippmätaren
     (`tripMeterAutomatic`) har inte nollställts emellan och har ökat ungefär lika mycket som
     resan är lång (±2 km eller 15 %). Snittet är `averageFuelConsumptionAutomatic` resp.
     `averageEnergyConsumptionAutomatic` – samma period som trippmätaren. Förbrukningen är
     `(snitt_efter × tripp_efter − snitt_före × tripp_före) / 100` – så får två resor i samma
     trippmätarperiod var sin del. Snitten har en decimal: felgränsen är
     `0,05 × (tripp_före + tripp_efter) / 100`; är den mer än 25 % av värdet avstår metoden.
     Nollställd trippmätare (tripp efter ≈ resans längd): `snitt × tripp / 100`.
  2. *Manuella trippmätaren × dess snitt* (1.5.0): `averageFuelConsumption` /
     `averageEnergyConsumption` gånger `tripMeterManual`, med samma regler. Snitten utan
     "Automatic" används bara med den manuella trippmätaren. Om ägarens bil rapporterar dem är
     inte känt; koden klarar båda fallen.
  3. *Nivåskillnad.* Ägarens bil rapporterar nivåerna bara när tändningen slås av. Nivån FÖRE
     resan är därför den senast rapporterade medan bilen stod still – oavsett hur gammal
     rapporten är (för 194-kilometersresan drygt 19 timmar) – och nivån EFTER den som
     rapporterades vid ankomsten. "Före" hittas utan känd starttid: den sista avläsningen före
     ankomsten vars nivå rapporterades före resans senast möjliga start och vars mätarställning
     inte har ökat förbi resans start.
     * Bensin: nettot i tanken. Har nivån ökat (mer än 1 l mellan två rapporter, eller totalt)
       är det en tankning och värdet lämnas okänt. Oförändrad nivå ger aldrig 0 l.
     * Regeln "minst 5 l/100 km när bara läget före och efter finns" gäller från 1.5.0 bara bilar
       utan batteri. En laddhybrid drar ofta lite bensin, och en dold tankning ger ett för lågt
       netto bara om den var mindre än resans förbrukning; vanligen är den större, och då blir
       nettot negativt och okänt. För en laddhybrid skulle regeln nästan bara kasta riktiga värden.
     * El (1.5.0): `(procent före − procent efter) × batteriets användbara kapacitet`. "Före" efter
       en laddning är nivån när laddningen var klar. Laddades batteriet under resan (nivån ökar,
       laddstatus `CHARGING` efter "före", eller nivån vid ankomsten rapporterades efter
       ankomsten under laddning), eller pågick laddningen när "före" rapporterades, lämnas elen
       okänd. Värdet är **netto ur batteriet**: bilen kan ha laddat batteriet med motorn eller
       bromsarna under färd.
  Säger nivåskillnaden emot den valda metoden utanför felgränserna behålls metoden med minst
  felgräns (färddatorns snitt, som i 1.3.0) och nivåskillnaden antecknas som avvikelse. Nivån
  efter måste ha resans slutmätarställning och vara rapporterad efter starten, och nivån före
  vara rapporterad vid resans startmätarställning, annars lämnas värdet okänt (så hamnar inte
  liter på fel resa när en rapport uteblir). Orimliga värden (negativa, mer än tanken/batteriet rymmer,
  mer än 60 l eller 60 kWh per 100 km) kastas. Räcker inget lämnas värdet okänt (`unknown`, "–").
  **Precisionen:** Volvos API ger tanknivån i hela liter och batterinivån i hela procent. Före och
  efter kan då vara fel med en halv enhet var, så nivåskillnaden är riktig på ±1 l respektive
  ±1 procentenhet × kapaciteten: "12 l ± 1 l" betyder 11–13 l. Det går inte att komma förbi med
  nivåerna ensamma; har en bil decimaler används de, och DriveLog avrundar aldrig själv.
  **Grunden** sparas per delresa (`raw_trips.fuel_basis` / `energy_basis`: metod, värdena före
  och efter, felgränsen, kapaciteten och anteckningar) och visas i resans detaljer. Bara resor
  som DriveLog har skapat ur bilens avläsningar de senaste 14 dygnen räknas; importerade resor
  och värden som redan finns rörs aldrig, och inte heller resor som finns i ett underlag som redan
  är skapat (1.5.0: en resa som tillkommit efter att månadens fil skapades finns inte i filen och
  fylls i som vanligt). Resor som saknar värden fylls i efterhand vid nästa efterbearbetning;
  går ett värde att räkna fram kör insamlingen efterbearbetningen även när ingen ny resa hittas.
* **Batteriets användbara kapacitet** (1.5.0, `vehicles.usable_battery_kwh`) anges under
  *Inställningar → Fordon och förare*. Tomt fält: ett standardvärde används bara där den
  ANVÄNDBARA kapaciteten är belagd för modellen och årsmodellen, annars är elen okänd. När
  kapaciteten fylls i eller ändras räknas den el om som DriveLog räknat ur kapaciteten (och el
  som saknas); ägarens egna och importerade värden rörs aldrig. Källor för V90 laddhybrid
  (offentliga, november 2026):
  * Volvo Cars pressmeddelande september 2021: batteriet i S90/V90/XC60 Recharge T8 ökar från
    11,6 till 18,8 kWh *nominellt*, först under årsmodell 2022.
  * 18,8 kWh-batteriet: användbart 14,9 kWh (evpulse.com, tflcar.com), 14,8 kWh
    (greencarreports.com). Standardvärde 14,9 kWh från årsmodell 2023.
  * 11,6 kWh-batteriet (årsmodell 2020–2022): användbart 9,1 kWh enligt greencarreports.com,
    10,4 kWh enligt pod-point.com (egen uppskattning). Källorna går isär och årsmodell 2021–2022
    kan ha båda batterierna → inget standardvärde. Ägarens bil (årsmodell 2021, elräckvidd
    32–36 km vid 100 %, alltså 11,6 kWh-batteriet) får inget standardvärde: elen visas när
    ägaren har fyllt i kapaciteten.
  * Volvos `batteryCapacityKWH` (fordonsuppgifterna) är nominell och visas bara som upplysning.
* **Felsökningsfil** (1.3.0): *Ladda ner felsökningsfil* under *Integrationer → Volvo*
  (`GET /api/v1/volvo/diagnostics?days=14`, 1–31 dygn) ger bilens avläsningar, hämtningarnas
  utfall (status och HTTP-kod) och de resor som byggdes av dem som JSON – utan VIN,
  registreringsnummer, positioner, adresser, platsnamn, rådata och nycklar. Id:n
  är löpnummer (`obs-1`, `trip-1`). Filen är till för den som hjälper dig felsöka.
  Från 1.4.2 innehåller filen också `internal_errors`: DriveLogs senaste interna fel (högst 20)
  med tid, steg, typ av fel, kodställen i DriveLogs egna filer och ett tvättat felmeddelande,
  kapat till 300 tecken. Kända värden (VIN, registreringsnummer, nycklar, kalenderlänken,
  sparade platser och de senaste resornas adresser) och mönster för gatuadresser, postnummer och
  koordinater tas bort ur meddelandet, men en adress eller ett platsnamn som varken är känt eller
  följer mönstren kan finnas kvar – läs filen innan du skickar den vidare.
  Från 1.5.0 innehåller filen också `months` (månad, läge, om filen skapades i förväg och när)
  och per resa `protected_by_month`, `created_at` (när DriveLog skapade resan), `start_method`
  (hur starttiden togs fram) samt korta koder för varför starttiden, bensinen och elen är
  okända (`start_unknown_reason`/`start_unknown_steps`, `fuel_unknown_reason`,
  `energy_unknown_reason`: t.ex. `closed_month`, `no_engine_start`, `engine_window`,
  `no_before_level`, `no_after_level`, `refuel`, `charging`, `no_capacity`, `unchanged_level`,
  `pending`). Inga platser eller positioner.
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
* **Ingen förbrukning per resa i API:t.** DriveLog uppskattar den ur trippmätaren, bilens snitt
  och nivåerna (se ovan). En tankning eller laddning som inte syns i en rapport gör
  nivåskillnaden okänd, och nivåerna i hela liter och hela procent ger ±1 l respektive ±1
  procentenhet. Elen ur batterinivån kräver batteriets användbara kapacitet.
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
* Förbrukningens metoder mot en riktig bil: att `averageFuelConsumptionAutomatic` och
  `averageEnergyConsumptionAutomatic` finns för bilen och avser `tripMeterAutomatic`s sträcka,
  snittens upplösning, när trippmätaren nollställs och hur ofta statistiken uppdateras. Saknar
  bilen de automatiska snitten används bara nivåskillnaden. Uppskattningen är prövad på påhittade
  avläsningar, demobilen och en avidentifierad uppspelning av ägarens förlopp (1.5.0) – inte
  mot den riktiga bilen.

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
