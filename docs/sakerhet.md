# Säkerhet och sekretess

DriveLog hanterar rörelsemönster och kalenderuppgifter. Det här dokumentet beskriver hur
uppgifterna skyddas och vad som lämnar din server.

## Autentisering och sessioner

- Lösenord hashas med Argon2id. Minst 10 tecken krävs.
- Inloggning ger en serverlagrad session. Kakan är `HttpOnly`, `SameSite=Lax` och `Secure` i
  produktion; databasen lagrar bara en hash av sessionstoken.
- Alla ändrande anrop kräver en CSRF-token i headern `X-CSRF-Token`.
- Inloggning är hastighetsbegränsad per IP-adress och per konto, och felmeddelandet avslöjar
  inte om kontot finns.
- Byte av lösenord loggar ut övriga sessioner och stänger alla nedladdningslänkar (se
  *Export* nedan). Det gäller också en återställning från kommandoraden eller från
  tilläggets inställningar.
- Det första kontot skapas vid första besöket; därefter kan konton bara skapas från servern.

## Maskinåtkomst (Home Assistant)

Home Assistant-integrationen använder en API-token med begränsad behörighet (`ha`). Den
visas en gång när den skapas, lagras som hash, kan återkallas när som helst och kan inte
användas för kontohantering, säkerhetskopior eller radering.

## Åtkomstkontroll

Varje API-anrop kräver inloggning eller token (utom hälsokontroll, första-gången-flödet och
den signerade nedladdningslänken, se *Export* nedan).
All data är knuten till en ägare, och varje uppslag kontrollerar ägarskapet – en annan
användares resor, platser, importer och exporter ger 404.

## Hemligheter

- OAuth-tokens, klienthemligheter, Home Assistant-token, AI-nycklar och kalenderlänken lagras
  krypterade (Fernet) med nyckeln i `DRIVELOG_ENCRYPTION_KEYS`. Nyckelrotation stöds.
- API:t lämnar aldrig ut en hemlighet – bara *att* den finns.
- Inga lösenord, nycklar, VIN eller privata adresser finns i koden eller dokumentationen.
- Loggarna maskerar tokens, nycklar, lösenord, `Authorization`-huvuden och VIN – i API:t,
  i webbserverns åtkomstlogg och i bakgrundsjobben (worker och schemaläggare).
- **Kalenderlänken (ICS)** ger läsrätt till kalendern och behandlas som ett lösenord. API:t
  visar bara *att* den finns och vilken server den går till. Den skrivs aldrig i
  ändringsloggen, i felmeddelanden eller i loggarna – felen är fasta texter utan adressen, och
  som extra skydd ersätts varje `webcal://`-adress och varje adress som ser ut som en delad
  eller publicerad kalender (`/owa/calendar/`, `/calendar/published/`, `.ics`) med
  `[KALENDERLÄNK DOLD]` i loggarna. *Ta bort länken* raderar den och dess händelser.
- OAuth-flödena använder PKCE och ett serverlagrat engångs-`state` med kort livslängd.

## Filuppladdning

- Endast `.xlsx` och `.csv`; storleksgräns (standard 10 MB) och radgräns.
- XLSX-filer kontrolleras som zip-arkiv före inläsning (antal delar, uppackad storlek) och
  filer med makron avvisas. Arbetsboken läses i skrivskyddat läge utan formler eller länkar.
- Originalfiler lagras under ett slumpat namn (filens hash) utanför webbroten, med
  filrättigheter 600, och nås bara via ett autentiserat anrop. De kan raderas per import.

### Företagsloggan (1.3.0)

- Bara PNG och JPEG. Typen avgörs av filens innehåll (filens första byte och bildens mått), inte
  av filnamnet eller den typ webbläsaren anger; SVG, HTML och allt annat avvisas, så att ingen
  aktiv kod kan laddas upp som "bild". Högst 1 MB och 4000 × 4000 bildpunkter.
- Filen sparas under datakatalogen (`branding/`) med ett eget slumpat namn och rättigheterna 600,
  ersätts vid ny uppladdning och raderas när du tar bort loggan. Den lämnas bara ut till den
  inloggade ägaren (`GET /api/v1/settings/logo`) och följer med i säkerhetskopian.
- Ingen bildbehandling görs på servern; Excel-biblioteket lägger in bilden som den är.

## Export

Användartext som börjar med `=`, `+`, `-`, `@`, tabb eller radbrytning skrivs som ren text
(skydd mot formula injection). Exportfiler får slumpade filnamn på servern och kan bara
hämtas av ägaren – eller via den signerade länken nedan.

### Nedladdningslänken i månadsnotisen

Månadsnotisen i självgående läge har en länk som hämtar underlaget utan inloggning
(`/api/v1/public/exports/<token>`).

- Länken är signerad (HMAC) och bunden till exporten, filens innehåll (sha256) och ett
  räknevärde per konto. Den gäller i **7 dagar** och för högst **10 hämtningar**. En ersatt
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
feltexter (bara felens nycklar och HTTP-koder) eller nycklar och tokens; databasens id ersätts
av löpnummer. Den skapas bara när du hämtar den och lämnar servern först när du själv skickar den
vidare.

## Vad lämnar servern?

| Mottagare | Vad | När |
| --- | --- | --- |
| Volvo Cars API | Anrop för din bils mätarställning, position, nivåer | När Volvo är anslutet |
| Microsoft Graph | Läsning av valda kalendrar | När Outlook är anslutet |
| Kalenderlänk (ICS) | Hämtar kalenderfilen (GET) | När en länk är sparad, var 30:e minut och vid Synka nu |
| Home Assistant (din egen) | Notistexter – från 1.3.0 med hela adressen för platser utan bekräftat namn och bostadens adress (se *Notiser*) | När notiser är påslagna |
| AI-leverantör | Underlag för en resa – fullständigt (standard från 1.3.0) eller minimalt, se nedan | Endast om en leverantör är ansluten: när reglerna inte räcker, när ärendet saknas, när en kalenderbokning stämmer med resan, och vid AI-genomgången |
| Kartplattor | Vilket kartområde du tittar på, och sidans ursprung som Referer (t.ex. `https://drivelog.example/`, aldrig sökvägen) | När en karta visas (kan stängas av eller pekas mot egen plattserver) |
| Geokodare (standard OpenStreetMap Nominatim) | Platsens position (sex decimaler) där bilen stannat på en okänd plats, och bostadens position en gång (1.3.0) | När geokodningen är på (standard från 1.1.0) och hemmet finns som plats med koordinat |
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
  DNS-svar och den faktiska motparten kontrolleras vid varje anrop. Klienten följer inga
  omdirigeringar, avbryter efter 4 sekunder och läser högst 64 kB.
- Svaret är opålitlig indata: bara gata, husnummer, postnummer, ort, namn och typ sparas, och
  all text tvättas från styr- och osynliga tecken.
- Adressdata © OpenStreetMap-bidragsgivare, under licensen ODbL. Erkännandet visas på sidan
  Platser och i inställningen.
- **Hämta adressen från kartan** (på en plats under *Platser*) skickar just den platsens
  position, avrundad till fyra decimaler, en gång när du trycker – samma anrop och skydd som
  ovan: svaret sparas per ruta (ett nytt tryck skickar ingenting) och anropet räknas mot
  gränsen 500 per dygn. Från 1.3.0 slås även bostadens adress upp automatiskt en gång (bara
  adress och ort sparas). Svaret från knappen sparas inte förrän du sparar platsen.

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
  molnleverantörernas metadataadress). Varje DNS-svar och den faktiska motparten efter
  anslutningen kontrolleras, så att ett namn inte kan bytas till en intern adress mellan
  kontroll och anslutning (DNS-ombindning). Samma hjälpfunktioner som geokodaren använder.
- Högst tre omdirigeringar följs, och varje hopp prövas på nytt med samma regler och en ny
  anslutning. Tidsgräns 5 sekunder för anslutning, 10 sekunder per läsning och 20 sekunder
  totalt. Svaret läses strömmat och avbryts vid 10 MB. Packade svar (gzip m.fl.) avvisas.
- Innehållet är opålitlig indata. Bara rubrik, tid, plats, ledig/upptagen, privat-flagga,
  UID och – med *Läs hela bokningen* (påslaget från 1.3.0) – beskrivningen (högst 4 000
  tecken) och deltagarna (namn och adress, högst 50) läses. Beskrivningar och deltagare
  skrivs aldrig i loggarna. All text tvättas från styr- och
  osynliga tecken och kapas. Taken (25 000 händelser, 5 000 tillfällen i synkfönstret, inga
  täta upprepningar) hindrar en elak fil från att belasta servern.
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
  signaler, tidigare bekräftade syften och – om tillåtet – rubrik, tid och plats för matchade
  bokningar. Hemadress, privata platsers namn, gatuadresser och brödtext skickas inte (de två
  sista kan slås på).

Aldrig, på någon nivå: koordinater, VIN, registreringsnummer, API-nycklar, tokens, lösenord,
kalenderlänken, Home Assistant-token och e-postadresser.

**Promptinjektion.** Allt i underlaget – kalendertexter, platsnamn från kartan, anteckningar,
importerade kommentarer, "Om mitt arbete" – är opålitlig indata. Det skickas avgränsat inom
`<data>…</data>` med en systemprompt som säger att innehållet är uppgifter, aldrig
instruktioner. Före sändning tvättas varje sträng (`classify/underlag.py`,
`calendar/calendar_text.py`, `text_safety.sanitize_text`): styrtecken och osynliga tecken bort,
`<`/`>` blir `‹`/`›`, länkar blir "[länk]", mötesblock (Teams/Zoom), inringningsnummer,
mötes-ID, koder och lösenord tas bort, långa nyckelliknande strängar, chassinummer och
koordinater tas bort, och texten kapas. Svaret valideras efteråt: ett ärende med ett namn som
inte finns i underlaget, med länk eller fel form avvisas; ett ärende godkänns aldrig av AI
själv; AI ensam ger aldrig automatisk klassning.

### Notiser

Från 1.3.0 väljer DriveLog precision före integritet i frågorna i självgående läge (ägarens
val): platser utan bekräftat namn visas med **hela adressen och husnumret** (postnumret tas
bort), med kartans namn först när det finns ("Exempel AB, Exempelgatan 12, Exempelstad").
**Bostaden visas med sin adress** (annars "Hemmet") och privata platser med sitt namn eller sin
adress. Bekräftade platser visas med sitt namn. Texterna går via Home Assistant till telefonen:
den som kommer åt Home Assistants historik, loggar eller en Home Assistant-token kan alltså se
adresserna. Notiserna i läget *Jag godkänner allt själv* visar som förut bara orten. Med *Dölj
platser på låsskärmen* är notistexten helt utan platser, bara dag och mil, och knapparna visar
bara "föreslaget ärende" i stället för ärendet (som ofta innehåller platsens namn). Från 1.1.0 är den
inställningen av som standard om du aldrig har ändrat den. Larm, månadsnotiser och notisen om en
ny version innehåller aldrig platser, VIN eller belopp per resa. Textsvar från telefonen tvättas och får vara
1–200 tecken. iOS har ingen inställning i notisen för detta – välj dessutom *Visa
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

- All trafik går över HTTPS via Caddy, med HSTS och restriktiva säkerhetshuvuden.
- API-svar skickas med `Cache-Control: no-store`; webbappens service worker cachar aldrig
  API-svar.
- I produktionsläge startar inte API:t med svag hemlighet, utan krypteringsnyckel eller utan
  https i bas-adressen.
- Containrarna körs som icke-root. Endast proxyn exponerar portar.
- API-dokumentationen (`/api/v1/docs`) är stängd i produktion.
- Installationsskriptet skapar administratörskontot innan proxyn startas, så att ingen
  utomstående kan hinna skapa det första kontot.

## Kända begränsningar

- Ingen tvåfaktorsautentisering. Lägg gärna en extra spärr framför (VPN, IP-filter eller
  autentisering i proxyn) om tjänsten exponeras mot internet.
- Hastighetsbegränsningen gäller inloggning, första-gången-flödet och nedladdningslänken, inte
  hela API:t.
- Kontospärren räknar misslyckade inloggningar per e-postadress. Den som känner till din
  adress kan därför tillfälligt (fem minuter) spärra inloggningen genom upprepade felaktiga
  försök. Befintliga sessioner påverkas inte.
- Klientens adress i inloggningsspärren och ändringsloggen tas från det sista värdet i
  `X-Forwarded-For`, det vill säga det som den egna proxyn lagt till. Publicera inte API-porten
  direkt mot internet utan proxy.
- Databasen är inte krypterad i vila utöver hemligheterna – använd diskkryptering på servern.
- Säkerhetskopior innehåller känsliga uppgifter och måste skyddas på samma sätt som servern.
