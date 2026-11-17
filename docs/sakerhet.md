# Säkerhet och sekretess

DriveLog hanterar rörelsemönster och kalenderuppgifter. Det här dokumentet beskriver hur
uppgifterna skyddas och vad som lämnar din server.

## Autentisering och sessioner

- Lösenord hashas med Argon2id. Minst 10 tecken krävs.
- Inloggning ger en serverlagrad session. Kakan är `HttpOnly`, `SameSite=Lax` och `Secure` i
  produktion; databasen lagrar bara en hash av sessionstoken.
- Alla ändrande anrop kräver en CSRF-token i headern `X-CSRF-Token`.
- Inloggning är hastighetsbegränsad per IP-adress och per konto (8 försök per 5 minuter;
  en lyckad inloggning nollställer kontots räknare), och felmeddelandet avslöjar inte om kontot finns. Från 1.3.1 räknas varje försök
  innan lösenordet prövas, i samma låsta steg, så att samtidiga försök inte tar sig förbi
  gränsen, och högst fyra lösenordsprövningar (Argon2) görs åt gången.
- **Känd enhet (1.3.1).** En lyckad inloggning sätter också en dold kaka för webbläsaren
  (`drivelog_device`: `HttpOnly`, `SameSite=Strict`, gäller bara för inloggningsanropet). Den är
  signerad och bunden till kontot och lösenordet. I en webbläsare med en giltig sådan kaka räknas
  försöken mot en egen spärr för just den webbläsaren i stället för kontots, så den som gissar
  mot kontot kan inte låsa ute dig från en webbläsare där du redan har loggat in. Alla sådana
  kakor slutar gälla när lösenordet byts.
- **Lösenordsbytet** har en egen spärr (1.3.1): 8 felaktiga nuvarande lösenord per 5 minuter.
  Felaktiga försök loggas i ändringsloggen, utan lösenord.
- En misslyckad inloggning loggas i ändringsloggen. Från 1.3.1 sparas den inskrivna adressen
  bara när den tillhör ett befintligt konto, så att ett lösenord som skrivits i fel fält inte
  sparas. Rader från tidigare versioner står kvar.
- Byte av lösenord loggar ut övriga sessioner och stänger alla nedladdningslänkar (se
  *Export* nedan). Det gäller också en återställning från kommandoraden eller från
  tilläggets inställningar.
- Det första kontot skapas vid första besöket; därefter kan konton bara skapas från servern. I
  Home Assistant-tillägget skapas kontot i stället bara ur tilläggets inställningar.

## Maskinåtkomst (Home Assistant)

Home Assistant-integrationen använder en API-token med begränsad behörighet (`ha`). Den
visas en gång när den skapas, lagras som hash, kan återkallas när som helst och kan inte
användas för kontohantering, säkerhetskopior eller radering.

## Åtkomstkontroll

Varje API-anrop kräver inloggning eller token, utom hälsokontrollen, första-gången-flödet,
själva inloggningen, återkomsterna från Volvos och Microsofts inloggning (som själva prövar
sessionen och ett engångs-`state`) och den signerade nedladdningslänken (se *Export* nedan).
Från 1.3.1 avvisas förfrågningskroppar över 2 MB med 413 innan de läses, om de inte är
uppladdningar (se *Kända begränsningar*).
All data är knuten till en ägare, och varje uppslag kontrollerar ägarskapet – en annan
användares resor, platser, importer och exporter ger 404.

## Hemligheter

- OAuth-tokens, klienthemligheter, Home Assistant-token, AI-nycklar och kalenderlänken lagras
  krypterade (Fernet) med nyckeln i `DRIVELOG_ENCRYPTION_KEYS`. Nyckelrotation stöds.
- API:t lämnar aldrig ut en hemlighet – bara *att* den finns.
- Inga lösenord, nycklar, VIN eller privata adresser finns i koden eller dokumentationen.
- Loggarna maskerar tokens, nycklar, lösenord, `Authorization`-huvuden och VIN (i
  standardformat: 17 tecken, versaler) – i API:t, i webbserverns åtkomstlogg och i
  bakgrundsjobben (worker och schemaläggare). Från 1.3.1 gäller det även felspår (tracebacks),
  JWT utan `Bearer` och parametrarna `code`, `state`, `session_state`, `token` och `secret` i
  adresser (t.ex. inloggningskoden i återkomsten från Microsoft). Databasfel skrivs utan de
  värden som skulle sparas.
- **Kalenderlänken (ICS)** ger läsrätt till kalendern och behandlas som ett lösenord. API:t
  visar bara *att* den finns och vilken server den går till. Den skrivs aldrig i
  ändringsloggen, i felmeddelanden eller i loggarna – felen är fasta texter utan adressen, och
  som extra skydd ersätts varje `webcal://`-adress och varje adress som ser ut som en delad
  eller publicerad kalender (`/owa/calendar/`, `/calendar/published/`, `/published/`,
  `/public-calendars/`, `/ical/`, `/calendars/`, `.ics`) med `[KALENDERLÄNK DOLD]` i loggarna. *Ta bort länken* raderar den och dess händelser.
- OAuth-flödena använder PKCE och ett serverlagrat engångs-`state` med kort livslängd. Efter
  återkomsten visar webben bara felkoder (som `access_denied`), aldrig annan text ur
  adressfältet, och efter inloggning skickas man bara vidare till sidor i DriveLog.

## Filuppladdning

- Endast `.xlsx` och `.csv` (en `.txt` läses som CSV); storleksgräns (standard 10 MB) och
  radgräns.
- XLSX-filer kontrolleras som zip-arkiv före inläsning (antal delar, uppackad storlek, från
  1.3.1 högst 50 MB delade strängar) och filer med makron avvisas. Arbetsboken läses i
  skrivskyddat läge utan formler eller länkar.
- Från 1.3.1 tas osynliga styr- och formattecken (t.ex. tecken som vänder textriktningen) bort
  ur ärenden och adresser vid import. Originalanteckningen sparas oförändrad.
- Originalfiler lagras under ett slumpat namn (filens hash) utanför webbroten, med
  filrättigheter 600, och nås bara via ett autentiserat anrop. De kan raderas per import.
  Raderingen (per import och under *Sekretess*) rör från 1.3.1 bara filer inne i importkatalogen,
  och exportfiler bara inne i exportkatalogen.

### Företagsloggan (1.3.0)

- Bara PNG och JPEG. Typen avgörs av filens innehåll (filens första byte och bildens mått), inte
  av filnamnet eller den typ webbläsaren anger; SVG, HTML och allt annat avvisas, så att ingen
  aktiv kod kan laddas upp som "bild". Högst 1 MB och 4000 × 4000 bildpunkter.
- Filen sparas under datakatalogen (`branding/`) med ett eget slumpat namn och rättigheterna 600,
  ersätts vid ny uppladdning och raderas när du tar bort loggan. Den lämnas bara ut till den
  inloggade ägaren (`GET /api/v1/settings/logo`) och följer med i säkerhetskopian. Från 1.3.1
  visar inställnings-API:t inte filnamnet på servern.
- Ingen bildbehandling görs på servern; Excel-biblioteket lägger in bilden som den är.

## Export

Användartext som börjar med `=`, `+`, `-`, `@`, tabb eller radbrytning skrivs som ren text
(skydd mot formula injection; radbrytningen `\n` från 1.3.1). Exportfiler får slumpade filnamn på servern och kan bara
hämtas av ägaren – eller via den signerade länken nedan.

### Nedladdningslänken i månadsnotisen

Månadsnotisen i självgående läge har en länk som hämtar underlaget utan inloggning
(`/api/v1/public/exports/<token>`).

- Länken är signerad (HMAC) och bunden till exporten, filens innehåll (sha256) och ett
  räknevärde per konto. Den gäller i **7 dagar** och för högst **10 hämtningar** (från 1.3.1
  räknas hämtningarna upp i databasen i ett steg, så taket håller även vid samtidiga anrop). En ersatt
  eller raderad fil ger ett fel i stället för filen.
- Svaret skickas som nedladdning med `Cache-Control: no-store`, `X-Robots-Tag: noindex` och
  `Referrer-Policy: no-referrer` (Caddy skriver om den sista till `same-origin`). Anropen är
  hastighetsbegränsade per IP-adress.
- Varje hämtning räknas och loggas i ändringsloggen, utan token. Token maskeras som `[DOLD]` i
  åtkomstloggen.
- **Risken:** notisen går via Home Assistant. Den som har en Home Assistant-token, eller kommer
  åt Home Assistants historik eller loggar, kan hämta underlaget så länge länken gäller.
- **Stäng länkarna** med *Stäng alla nedladdningslänkar* på exportsidan, genom att byta
  lösenord (också med en återställning från kommandoraden eller tilläggets inställningar)
  eller genom att radera exporten. Byt aldrig `DRIVELOG_SECRET_KEY`
  för att stänga länkar – krypteringsnyckeln kan vara härledd ur den.

### Felsökningsfilen för Volvo (1.3.0)

*Ladda ner felsökningsfil* under *Integrationer → Volvo* ger en JSON-fil med bilens avläsningar
(mätarställning, bränsle, batteri, trippmätare, snittvärden, motorstatus med tidsstämplar),
hämtningarnas utfall och de resor som byggdes av dem, för de senaste 14 dygnen. Filen innehåller
**inte** chassinummer (VIN), registreringsnummer, positioner, adresser, platsnamn, Volvos rådata,
Volvos feltexter (bara felens nycklar och HTTP-koder) eller nycklar och tokens; databasens id
ersätts av löpnummer. Den skapas bara när du hämtar den och lämnar servern först när du själv
skickar den vidare.

Från 1.4.2 innehåller filen också `internal_errors`: DriveLogs senaste interna fel (högst 20)
med tid, steg, typ av fel, kodställen och ett tvättat felmeddelande kapat till 300 tecken.
Kända värden (VIN, registreringsnummer, nycklar, kalenderlänken, sparade platser och de senaste
resornas adresser) och mönster för gatuadresser, postnummer och koordinater tas bort ur
meddelandet, men en adress eller ett platsnamn som varken är känt eller följer mönstren kan
finnas kvar. Läs filen innan du skickar den vidare.

## Vad lämnar servern?

| Mottagare | Vad | När |
| --- | --- | --- |
| Volvo Cars API | Anrop för din bils mätarställning, position, nivåer | När Volvo är anslutet |
| Microsoft Graph | Läsning av valda kalendrar | När Outlook är anslutet |
| Kalenderlänk (ICS) | Hämtar kalenderfilen (GET) | När en länk är sparad, var 30:e minut och vid Synka nu |
| Home Assistant (din egen) | Notistexter – från 1.3.0 med hela adressen för platser utan bekräftat namn och bostadens adress (se *Notiser*) | När notiser är påslagna |
| AI-leverantör | Underlag för en resa – fullständigt (standard från 1.3.0) eller minimalt, se nedan | Endast om en leverantör är ansluten: när reglerna inte räcker, när ärendet saknas, när en kalenderbokning stämmer med resan, och vid AI-genomgången |
| Kartplattor | Vilket kartområde du tittar på, och sidans ursprung som Referer (t.ex. `https://drivelog.example/`, aldrig sökvägen) | När en karta visas (kan stängas av eller pekas mot egen plattserver) |
| Geokodare (standard OpenStreetMap Nominatim) | Platsens position (sex decimaler) där bilen stannat på en okänd plats, och bostadens position tills ett svar har kommit (1.3.0) | När geokodningen är på (standard från 1.1.0) och hemmet finns som plats med koordinat |
| Geokodare, adress → position (1.3.0) | Adresstexten för en plats som har adress men saknar position (t.ex. från en importerad körjournal) | Samma villkor, högst 3 per minut och inom samma dygnsgräns |
| Geokodare, *Hämta adressen från kartan* | Den valda platsens position, en gång | Bara när du trycker på knappen på en plats, och geokodningen är på |
| Home Assistant (din egen) | Signerad nedladdningslänk i månadsnotisen | Självgående läge, när underlaget är klart |

Ingen telemetri eller analys skickas någonstans.

### Geokodningen (OpenStreetMap)

När bilen stannar på en plats som inte redan finns i DriveLog slår servern upp gatuadressen,
som standard hos OpenStreetMaps Nominatim.

- Bara `GET <geokodare>/reverse` med platsens position (från 1.3.0 sex decimaler, för att rätt
  hus ska hittas; svaret sparas fortfarande per liten ruta) och `GET <geokodare>/search` med en
  plats adresstext skickas. Resa, tid, id, namn och
  kalender skickas aldrig. User-Agent är neutral (`SMGD-DriveLog/<version> (self-hosted
  logbook; reverse geocoding)`) och ingen Referer skickas.
- **OpenStreetMap ser serverns publika adress och när uppslagen görs.** Tidpunkterna kan säga
  något om när bilen stannar. Stäng av geokodningen under *Inställningar → Sekretess* om du
  inte vill det; då får nya platser ingen adress.
- Samma ruta slås bara upp en gång (svaret sparas), högst ett anrop per 1,1 sekund och 500 per
  dygn. Stopp vid kända platser och demobilen slås aldrig upp, och inget skickas förrän hemmet
  finns som plats med koordinat.
- Adressen till geokodaren måste vara https på port 443, utan användarnamn, frågesträng eller
  fragment, och får inte peka på `localhost` eller en lokal eller privat IP-adress. Varje
  DNS-svar kontrolleras vid varje anrop, och från 1.3.1 ansluter klienten bara till de adresser
  som har prövats (skydd mot DNS-ombindning). IPv6-adresser med en inbäddad IPv4-adress (t.ex.
  NAT64) prövas som den IPv4-adressen. Klienten följer inga omdirigeringar och läser högst
  64 kB. Anslutning och varje läsning har en gräns på 4 sekunder, och läsningen avbryts när
  4 sekunder har gått sedan anropet började; DNS-uppslaget har ingen egen gräns.
- Svaret är opålitlig indata: bara gata, husnummer, postnummer, ort, namn och typ sparas, och
  all text tvättas från styr- och osynliga tecken.
- Adressdata © OpenStreetMap-bidragsgivare, under licensen ODbL. Erkännandet visas på sidan
  Platser och i inställningen.
- **Hämta adressen från kartan** (på en plats under *Platser*) skickar just den platsens
  position (sex decimaler) en gång när du trycker – samma anrop och skydd som ovan: svaret
  sparas per ruta (ett nytt tryck skickar ingenting) och anropet räknas mot gränsen 500 per
  dygn. Från 1.3.0 slås även bostadens adress upp automatiskt när den saknas (bara adress och
  ort sparas; misslyckas uppslaget görs nya försök tills ett svar har kommit). Svaret från
  knappen sparas inte förrän du sparar platsen.
- Från 1.3.0 slås en importerad plats vars uppslagna position hamnar vid bostaden eller kontoret
  ihop med den (se ändringsloggen för 1.3.0). Från 1.3.1 slås en plats med säker position (din
  egen eller bilens) aldrig ihop via adressen med ett stopp mer än 2 km därifrån.

### Kartbilderna och Referer

Sidan skickar `Referrer-Policy: same-origin`: webbläsaren skickar ingen Referer till andra
webbplatser, så att inga adresser i appen (t.ex. en resas id) läcker. OpenStreetMaps kartservrar
kräver däremot en giltig Referer och svarar annars med en blockerad bild ("403 Access blocked").
Därför får **bara anropen efter kartbilder** policyn `strict-origin-when-cross-origin` (MapLibre
`transformRequest`, `frontend/src/components/map/tiles.ts`): kartservern ser sidans ursprung,
t.ex. `https://drivelog.example/`, aldrig sökvägen. Allt annat följer sidans policy, som är
oförändrad i Next, backend, Caddy (`deploy/Caddyfile` och tillägget) och demots `_headers`.
Helhetstesterna (`frontend/e2e`) fångar kartbildsanropen i en riktig webbläsare och kontrollerar
rubriken. Övriga krav i OpenStreetMaps regler för kartbilder: källhänvisningen
"© OpenStreetMap" visas på kartan, webbläsarens egen User-Agent skickas, plattorna cachas av
webbläsaren enligt serverns svar, och plattor hämtas bara när en karta visas – aldrig i bulk.

### Kalenderlänken (ICS)

Med en sparad kalenderlänk hämtar servern kalenderfilen hos kalendertjänsten (för Outlook:
Microsoft). Tjänsten ser serverns publika adress och när hämtningarna görs.

- Bara `GET` med en neutral User-Agent (`SMGD-DriveLog/<version> (self-hosted logbook;
  calendar link)`). Inga kakor, ingen Referer och inga proxyinställningar från miljön.
- Länken måste vara https på port 443 utan användarnamn eller lösenord, och får inte peka på
  `localhost` eller en lokal, privat, länklokal, CGNAT- eller reserverad adress (även
  molnleverantörernas metadataadress och IPv6-adresser med en sådan IPv4-adress inbäddad). Varje
  DNS-svar kontrolleras, och från 1.3.1 ansluter klienten bara till de adresser som har prövats,
  så att ett namn inte kan bytas till en intern adress mellan kontroll och anslutning
  (DNS-ombindning). Den faktiska motparten kontrolleras dessutom efter anslutningen. Samma
  hjälpfunktioner som geokodaren använder.
- Högst tre omdirigeringar följs, och varje hopp prövas på nytt med samma regler och en ny
  anslutning. Tidsgräns 5 sekunder för anslutning och 10 sekunder per läsning, och läsningen
  avbryts när 20 sekunder har gått sedan hämtningen började (ett enskilt steg kan alltså dra
  över något). Svaret läses strömmat och avbryts vid 10 MB. Packade svar (gzip m.fl.) avvisas.
- Innehållet är opålitlig indata. Bara rubrik, tid, plats, ledig/upptagen, privat-flagga,
  UID och – med *Läs hela bokningen* (påslaget från 1.3.0) – beskrivningen (högst 4 000
  tecken) och deltagarna (namn och adress, högst 50) läses. Beskrivningar och deltagare
  skrivs aldrig i loggarna. All text tvättas från styr- och
  osynliga tecken och kapas. Taken (25 000 händelser, 5 000 tillfällen i synkfönstret, inga
  täta upprepningar, från 1.3.1 högst 10 upprepningsregler per händelse) och spärren mot regler
  som aldrig kan inträffa hindrar en elak fil från att belasta servern. Mycket långa texter kapas
  innan de tvättas.
- Har en synk avbrutits i förtid (av taken eller tidsgränsen) tas inga bokningar bort som synken
  inte hann se (1.3.1). Samma sak gäller Outlook-synken. Där följs nästa-sida-länkar bara till
  `graph.microsoft.com`, och rubrik, plats och adress tvättas på samma sätt som för
  kalenderlänken.
- Tolkningen använder biblioteken `icalendar` (BSD-2-Clause) och `recurring-ical-events`
  med `x-wr-timezone` (LGPL-3.0-or-later). De används oförändrade som installerade paket.

### AI-leverantören

Två nivåer (*Inställningar → Förslag och AI*):

- **Fullständigt underlag** (standard från 1.3.0): platsernas namn och gatuadresser (även
  bostaden och privata platser), kartans namn och typ, platsanteckningar, dagens resor, resan
  före och efter, historik och dina regler, kalenderbokningarna för dagen och – vid resor till
  en annan ort – hela vistelsen och de närmaste dagarna (rubrik, plats, tvättad beskrivning och
  deltagarnas namn med kollega/extern och den externa e-postdomänen), arbetsgivarens namn och
  "Om mitt arbete". **Det betyder att personuppgifter (du själv, kollegor, kunder och
  kandidater i bokningarna) och arbetsgivarens uppgifter lämnar servern till den leverantör du
  valt** – ägaren har bett om det, och arbetsgivaren har godkänt att jobbkalendern används.
  Privata bokningar skickas bara som tid.
- **Minimalt**: platsetikett eller ort, platstyp, veckodag, klockslag, sträcka, regelmotorns
  signaler, tidigare bekräftade syften och rubrik, tid och plats för matchade bokningar
  (påslaget från början, kan stängas av). Hemadress, privata platsers namn, gatuadresser och
  brödtext skickas inte (de två sista kan slås på; brödtexten är då de första 300 tecknen).
  På den här nivån skickas rubrik och brödtext som de står i kalendern – koder, länkar,
  e-postadresser eller lösenord som någon har skrivit där tas inte bort (men VIN och
  registreringsnummer tas bort, se nedan).

Aldrig, på någon nivå: koordinater, VIN, registreringsnummer, DriveLogs egna API-nycklar och
tokens, dina lösenord, kalenderlänken, Home Assistant-token och deltagarnas e-postadresser.
Från 1.3.1 tas VIN och registreringsnummer bort på båda nivåerna för alla dina bilar och i alla
skrivsätt (med mellanslag, bindestreck eller punkt, gemener och helbreddstecken), och dessutom
allt som ser ut som ett VIN. Tidigare togs bara den exakta sparade skrivformen bort, och bara på
nivån Fullständigt.
På nivån Fullständigt tas dessutom länkar, e-postadresser, koder, mötes-ID och lösenord bort ur
rubriker, platser, beskrivningar och anteckningar – från 1.3.1 även i böjd form (*koden*,
*lösenordet*), wifi-lösenord och mötes-id för Teams, Zoom och Webex.

**Promptinjektion.** Allt i underlaget – kalendertexter, platsnamn från kartan, anteckningar,
importerade kommentarer, "Om mitt arbete" – är opålitlig indata. Det skickas avgränsat inom
`<data>…</data>` med en systemprompt som säger att innehållet är uppgifter, aldrig
instruktioner. På båda nivåerna blir `<`/`>` i hela underlaget `‹`/`›` (`classify/llm.py`), så
ingen text kan stänga avgränsningen. På nivån Fullständigt tvättas dessutom varje sträng (`classify/underlag.py`,
`calendar/calendar_text.py`, `text_safety.sanitize_text`): styrtecken och osynliga tecken bort,
`<`/`>` blir `‹`/`›`, länkar blir "[länk]", mötesblock (Teams/Zoom), inringningsnummer,
mötes-ID, koder och lösenord tas bort, långa nyckelliknande strängar, chassinummer och
koordinater tas bort, och texten kapas. Svaret valideras efteråt: osynliga tecken tas bort ur
ärendet innan det prövas och sparas, och ett ärende med ett namn som inte finns i underlaget
(från 1.3.1 i alla skriftsystem och med helbreddstecken), med länk eller fel form avvisas.

**Vad AI får avgöra.** AI:s förslag till ett ärende märks *Förslag att bekräfta* och godkänns
aldrig av AI. I självgående läge finns två undantag, där AI:s bedömning eller en kalenderbokning
avgör utan att du tillfrågas (se *Kända begränsningar*): en resa till en okänd plats av oklar
typ (t.ex. advokat, myndighet) kan antas vara en tjänsteresa enbart på AI:s bedömning, med
vetotid (*Antaget kundbesök*, standard 24 timmar), och när en resa godkänns automatiskt kan
ärendet tas ur en kalenderbokning eller ur ett AI-ärende som bygger på kalendern.

### Notiser

Från 1.3.0 väljer DriveLog precision före integritet i frågorna i självgående läge (ägarens
val): platser utan bekräftat namn visas med **hela adressen och husnumret** (postnumret tas
bort), med kartans namn först när det finns ("Exempel AB, Exempelgatan 12, Exempelstad").
**Bostaden visas med sin adress** (annars "Hemmet") och privata platser med sitt namn eller sin
adress. Bekräftade platser visas med sitt namn. Texterna går via Home Assistant till telefonen:
den som kommer åt Home Assistants historik, loggar eller en Home Assistant-token kan alltså se
adresserna. Notiserna i läget *Jag godkänner allt själv* visar som förut bara orten. Med *Dölj
platser på låsskärmen* är notistexten helt utan platser, bara dag och mil, och i självgående läge
visar knapparna bara "föreslaget ärende" i stället för ärendet (som ofta innehåller platsens
namn). **Undantag:** i läget *Jag godkänner allt själv* visar frågan *Vad var syftet?* dina
tidigare syften som knappar även när inställningen är på, och de kan innehålla platsnamn. Från
1.1.0 är inställningen av som standard om du aldrig har ändrat den. Larm, månadsnotiser och notisen om en
ny version innehåller aldrig platser, VIN eller belopp per resa. Textsvar från telefonen tvättas och får vara
1–200 tecken. Från 1.3.1 visas `<` och `>` i rubrik och text som `‹` och `›`, så att telefonen
aldrig tolkar texten som HTML, och integrationen i Home Assistant skickar bara vidare DriveLogs
eget notisformat (rubrik, text, DriveLogs egna knappar, taggar och länkar som börjar med http
eller https). Annat innehåll avvisas och kvitteras som misslyckat. Det gäller när integrationen
har uppdaterats till 1.3.1. iOS har ingen inställning i notisen för detta – välj dessutom *Visa
förhandsvisningar: När olåst* för Home Assistant-appen i iPhone-inställningarna.

## Spårbarhet

Alla viktiga ändringar loggas (`audit_events`) med vem eller vad som gjorde dem: användare,
system, AI, Home Assistant eller import. Originaldata bevaras alltid; resedetaljen visar
ändringshistoriken och exporten har ett eget blad för den.

## Radering

Under *Inställningar → Sekretess* kan fordonsobservationer, kalenderposter, exportfiler,
notishistorik och all resehistorik för ett fordon raderas permanent. Rådataobservationer kan
också gallras automatiskt efter ett valt antal dagar. *Koppla bort* på en integration raderar
dess hemligheter.

## Drift

- All trafik går över HTTPS via Caddy, med HSTS och restriktiva säkerhetshuvuden. Från 1.3.1
  skickar webbappen en Content-Security-Policy: skript bara från den egna servern (inline
  tillåts, eftersom Next behöver det), inga inbäddade objekt, ingen inramning och formulär bara
  till den egna servern. Bilder och anslutningar tillåts mot http och https, så att egna
  kartservrar fungerar.
- API-svar skickas med `Cache-Control: no-store`; webbappens service worker cachar aldrig
  API-svar.
- I produktionsläge startar inte API:t med svag hemlighet, utan krypteringsnyckel, utan https i
  bas-adressen eller (från 1.3.1) med `*` eller en adress utan https i `DRIVELOG_CORS_ORIGINS`.
  Består `DRIVELOG_ENCRYPTION_KEYS` bara av blanksteg eller kommatecken, eller innehåller den en
  ogiltig nyckel, startar API:t ändå – då med en nyckel härledd ur `DRIVELOG_SECRET_KEY` – men
  från 1.3.1 står en varning i loggen. Tillägget och installationsskripten skapar giltiga nycklar.
- API och webbapp körs som icke-root. I avbilden för egen server och tillägget körs även proxyn
  som icke-root, medan startprogrammet kör som root (det sätter rättigheter och startar de
  andra processerna utan nya privilegier); i Compose-varianten kör proxyn (Caddy) som root.
  Endast proxyn exponerar portar.
- API-dokumentationen (`/api/v1/docs`) är stängd i produktion.
- Installationsskriptet skapar administratörskontot innan proxyn startas, så att ingen
  utomstående kan hinna skapa det första kontot.
- Leveranskedjan: i båda arbetsflödena på GitHub är åtgärderna fästa vid commit-SHA (1.3.1), och
  publiceringen sparar inte sin token i git och installerar inga paket från PyPI. Avbildens
  byggkontext tar aldrig med `.env`-filer.

## Kända begränsningar

- Ingen tvåfaktorsautentisering. Lägg gärna en extra spärr framför (VPN, IP-filter eller
  autentisering i proxyn) om tjänsten exponeras mot internet. **Obs:** portar som Docker
  publicerar (80 och 443) går förbi brandväggens vanliga regler, till exempel `ufw`. Ett IP-filter
  måste därför ligga i kedjan `DOCKER-USER`, i leverantörens brandvägg eller i proxyn – en
  `ufw`-regel ensam stänger ingenting.
- Hastighetsbegränsningen gäller inloggning, lösenordsbyte, första-gången-flödet och
  nedladdningslänken, inte hela API:t.
- Kontospärren räknar misslyckade inloggningar per e-postadress från webbläsare där du inte har
  loggat in förut (en tidigare inloggad webbläsare har en egen spärr, se *Känd enhet*). Den som
  känner till din adress kan därför spärra inloggningen från en ny webbläsare eller telefon så
  länge försöken fortsätter. Befintliga sessioner och tidigare inloggade webbläsare påverkas
  inte, men efter ett lösenordsbyte gäller det alla webbläsare tills du har loggat in i dem igen.
- Klientens adress i inloggningsspärren och ändringsloggen tas från det sista värdet i
  `X-Forwarded-For`, det vill säga det som den egna proxyn lagt till. Publicera inte API-porten
  direkt mot internet utan proxy. På den egna servern kan alla besökare över IPv6 räknas som en
  och samma adress (Dockers nätverksbrygga), så att de delar IP-spärren.
- Uppladdningar (import och logga) tas emot i sin helhet, upp till proxyns tak på 60 MB, innan
  inloggningen och filens storleksgräns prövas. Andra förfrågningskroppar har ett tak på 2 MB.
- Databasen är inte krypterad i vila utöver hemligheterna – använd diskkryptering på servern.
- Säkerhetskopior innehåller känsliga uppgifter och måste skyddas på samma sätt som servern. En
  återställd äldre kopia tar tillbaka det lösenord och de API-token som gällde när kopian togs.

### Inte rättat, eftersom rättningen skulle märkas

Säkerhetsgenomgången inför 1.3.1 fann också följande. Rättningarna skulle ändra hur appen
fungerar eller ser ut, så de har inte gjorts utan ägarens beslut.

- **Kalenderbokningar räknas oavsett vem som har skickat dem.** En bokning räknas även om du inte
  har tackat ja, så en inbjudan från någon annan kan bli ärendet på en resa som godkänns
  automatiskt i självgående läge, eller göra att en resa ger en fråga i stället för att godkännas
  av sig själv. Kontrollera ärendena i månadens genomgång.
- **AI kan ensam ge *Antaget kundbesök*** för en okänd plats av oklar typ i självgående läge,
  med vetotid. Allt i underlaget till AI är opålitlig indata, så den bedömningen kan påverkas av
  kalendertexter och kartans platsnamn.
- **Jobbdomänen kan läras in från någon annans inbjudan.** Använder du en allmän e-postadress
  (t.ex. gmail) och har fältet *Min arbetsgivares e-postdomän* tomt kan DriveLog fylla i det
  utifrån bokningar som någon annan har skickat. Fyll i fältet själv eller kontrollera det under
  *Inställningar → Kalender*; ett värde du har sparat skrivs aldrig över.
- **Inget tak för antalet AI-anrop per dygn.** Ett fast tak skulle kunna fördröja förslagen i
  vanlig användning. Sätt gärna en kostnadsgräns hos AI-leverantören.
- **Notisknapparna fungerar på låst telefon.** Den som har din telefon kan svara på frågorna utan
  att låsa upp den. Stäng av Home Assistant-appens notiser på låsskärmen i telefonens
  inställningar om andra kan komma åt telefonen.
- ***Dölj platser på låsskärmen*** döljer inte de tidigare syftena på knapparna i läget *Jag
  godkänner allt själv* (se *Notiser*).
- **Lösenordsbyte återkallar inte API-token.** Byt lösenord *och* återkalla token under
  *Inställningar → Säkerhet* om du misstänker att någon annan har varit inloggad.
- **Push-läget godtar `http://` till Home Assistant**, också till en publik adress. Då skickas
  Home Assistants token okrypterad. Använd `https://` om Home Assistant nås över internet, eller
  pull-läget.
- **Volvo-panelen** visar ett maskerat VIN och en referens som är härledd ur VIN; tillsammans kan
  de troligen räcka för att räkna fram hela VIN. Bara den inloggade ägaren ser dem.
- **Hälsokontrollen visar versionsnumret** utan inloggning. Det används av bevakning och
  felsökning.
- **Borttagning av en importerad resa** tar inte tillbaka en Volvo-resa som importen ersatte, och
  en bekräftad arbetsplats som bara har en adress får sitt område ur kartans svar utan
  rimlighetskontroll.
- **Drift och leveranskedja.** Pythonberoendena och bas-avbilderna är inte låsta till exakta
  versioner, och den automatiska uppdateringen på servern kontrollerar inte avbildens digest
  eller signatur. Containerns logg på den egna servern har ingen storleksgräns. Tillägget
  begär administratörsåtkomst till Home Assistant. Compose-installationen (`install.sh`) lägger
  administratörslösenordet på kommandoraden medan kontot skapas, och dess Caddyfile släpper in
  60 MB per förfrågan. De här ändringarna kräver en provstart med Docker och görs i ett eget
  steg.
