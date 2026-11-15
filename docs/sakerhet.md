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
- Byte av lösenord loggar ut övriga sessioner.
- Det första kontot skapas vid första besöket; därefter kan konton bara skapas från servern.

## Maskinåtkomst (Home Assistant)

Home Assistant-integrationen använder en API-token med begränsad behörighet (`ha`). Den
visas en gång när den skapas, lagras som hash, kan återkallas när som helst och kan inte
användas för kontohantering, säkerhetskopior eller radering.

## Åtkomstkontroll

Varje API-anrop kräver inloggning eller token (utom hälsokontroll och första-gången-flödet).
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
hämtas av ägaren.

## Vad lämnar servern?

| Mottagare | Vad | När |
| --- | --- | --- |
| Volvo Cars API | Anrop för din bils mätarställning, position, nivåer | När Volvo är anslutet |
| Microsoft Graph | Läsning av valda kalendrar | När Outlook är anslutet |
| Home Assistant (din egen) | Notistexter | När notiser är påslagna |
| AI-leverantör | Avidentifierat underlag för en resa, se nedan | Endast om en leverantör är ansluten och reglerna inte räcker |
| Kartplattor | Vilket kartområde du tittar på | När en karta visas (kan stängas av eller pekas mot egen plattserver) |
| Geokodare | Adresser | Endast om geokodning uttryckligen slagits på |

Ingen telemetri eller analys skickas någonstans.

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

Notiser innehåller aldrig gatuadresser. Hemmet visas som "Hemmet" och privata platser som
"Privat plats". Med *Dölj platser på låsskärmen* (standard) är notistexten helt utan
platsnamn. iOS har ingen inställning i notisen för detta – välj dessutom *Visa
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
- Hastighetsbegränsningen gäller inloggning och första-gången-flödet, inte hela API:t.
- Kontospärren räknar misslyckade inloggningar per e-postadress. Den som känner till din
  adress kan därför tillfälligt (fem minuter) spärra inloggningen genom upprepade felaktiga
  försök. Befintliga sessioner påverkas inte.
- Klientens adress i inloggningsspärren och ändringsloggen tas från det sista värdet i
  `X-Forwarded-For`, det vill säga det som den egna proxyn lagt till. Publicera inte API-porten
  direkt mot internet utan proxy.
- Databasen är inte krypterad i vila utöver hemligheterna – använd diskkryptering på servern.
- Säkerhetskopior innehåller känsliga uppgifter och måste skyddas på samma sätt som servern.
