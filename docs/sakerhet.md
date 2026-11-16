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
  *Export* nedan). En återställning från kommandoraden eller från tilläggets inställningar
  stänger inte länkarna.
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

- OAuth-tokens, klienthemligheter, Home Assistant-token och AI-nycklar lagras krypterade
  (Fernet) med nyckeln i `DRIVELOG_ENCRYPTION_KEYS`. Nyckelrotation stöds.
- API:t lämnar aldrig ut en hemlighet – bara *att* den finns.
- Inga lösenord, nycklar, VIN eller privata adresser finns i koden eller dokumentationen.
- Loggarna maskerar tokens, nycklar, lösenord, `Authorization`-huvuden och VIN – i API:t,
  i webbserverns åtkomstlogg och i bakgrundsjobben (worker och schemaläggare).
- OAuth-flödena använder PKCE och ett serverlagrat engångs-`state` med kort livslängd.

## Filuppladdning

- Endast `.xlsx` och `.csv`; storleksgräns (standard 10 MB) och radgräns.
- XLSX-filer kontrolleras som zip-arkiv före inläsning (antal delar, uppackad storlek) och
  filer med makron avvisas. Arbetsboken läses i skrivskyddat läge utan formler eller länkar.
- Originalfiler lagras under ett slumpat namn (filens hash) utanför webbroten, med
  filrättigheter 600, och nås bara via ett autentiserat anrop. De kan raderas per import.

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
  lösenord inne i DriveLog eller genom att radera exporten. Byt aldrig `DRIVELOG_SECRET_KEY`
  för att stänga länkar – krypteringsnyckeln kan vara härledd ur den.

## Vad lämnar servern?

| Mottagare | Vad | När |
| --- | --- | --- |
| Volvo Cars API | Anrop för din bils mätarställning, position, nivåer | När Volvo är anslutet |
| Microsoft Graph | Läsning av valda kalendrar | När Outlook är anslutet |
| Home Assistant (din egen) | Notistexter | När notiser är påslagna |
| AI-leverantör | Avidentifierat underlag för en resa, se nedan | Endast om en leverantör är ansluten och reglerna inte räcker |
| Kartplattor | Vilket kartområde du tittar på | När en karta visas (kan stängas av eller pekas mot egen plattserver) |
| Geokodare (standard OpenStreetMap Nominatim) | Mittpunkten av en liten ruta (koordinat med fyra decimaler) där bilen stannat på en okänd plats | När geokodningen är på (standard från 1.1.0) och hemmet finns som plats med koordinat |
| Home Assistant (din egen) | Signerad nedladdningslänk i månadsnotisen | Självgående läge, när underlaget är klart |

Ingen telemetri eller analys skickas någonstans.

### Geokodningen (OpenStreetMap)

När bilen stannar på en plats som inte redan finns i DriveLog slår servern upp gatuadressen,
som standard hos OpenStreetMaps Nominatim.

- Bara `GET <geokodare>/reverse` med rutans mittpunkt skickas. Resa, tid, id, namn och
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

### AI-leverantören

Skickas: platsetikett eller ort, platstyp, veckodag, klockslag, sträcka, regelmotorns
signaler, tidigare bekräftade syften för samma resa och – om tillåtet – rubrik, tid och plats
för matchade kalenderbokningar. Skickas aldrig: koordinater, VIN, registreringsnummer,
hemadress, privata platsers namn, gatuadresser eller kalenderns brödtext (de två sista kan
slås på uttryckligen).

Kalendertexter och importerade kommentarer behandlas som opålitlig indata. De skickas
avgränsade som data, och modellens svar valideras efteråt – en instruktion gömd i en
kalenderrubrik kan inte ändra en klassning eller smyga in påhittade uppgifter.

### Notiser

Notiser innehåller aldrig husnummer. Hemmet visas som "Hemmet" och privata platser som
"Privat plats". Obekräftade platser visas bara som "företagsnamn, ort" eller "gata utan
husnummer, ort"; hela adressen syns först i webben efter inloggning. Med *Dölj platser på
låsskärmen* är notistexten helt utan platser, bara dag och mil. Från 1.1.0 är den
inställningen av som standard om du aldrig har ändrat den. Larm och månadsnotiser innehåller
aldrig platser, VIN eller belopp per resa. Textsvar från telefonen tvättas och får vara
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
