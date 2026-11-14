# Home Assistant och SMGD DriveLog

SMGD DriveLog kan ställa korta frågor om dina resor som interaktiva notiser på
telefonen via Home Assistants Companion-app (iOS och Android): *Arbete eller
privat?*, *Vad var syftet?*, *Ska resorna slås samman?* Du svarar direkt i notisen
och svaret hamnar i körjournalen. Integrationen ger dessutom sensorer med
nyckeltal (resor att granska, tjänstemil och privata mil denna månad, senaste synk).

Allt i den här guiden går att göra utan att öppna Home Assistant mot internet –
ingen publik webhook i Home Assistant behövs.

## Översikt

| Del | Vad den gör |
| --- | --- |
| DriveLog (backend) | Skapar frågor, tillämpar policy (tysta timmar, max per dygn, samlingsnotiser, påminnelser) och tar emot svar. |
| Integrationen `smgd_drivelog` | Lyssnar på knapptryck, skickar svaren till DriveLog (med persistent kö), levererar notiser i pull-läget och visar sensorer. |
| Companion-appen | Visar notisen och skickar händelsen `mobile_app_notification_action` när du trycker på en knapp. |

Svar går **alltid** från Home Assistant till DriveLog (`POST /api/v1/ha/callback`
med API-token). Notiser kan levereras på två sätt – se [Leveranslägen](#leveranslägen).

Krav: Home Assistant **2024.11** eller senare, Companion-appen installerad och
inloggad på telefonen, och att DriveLog kan nås från Home Assistant.

## 1. Installera integrationen

### Via HACS (custom repository)

1. Öppna **HACS** i Home Assistant.
2. Välj menyn (⋮) → **Anpassade arkiv** (*Custom repositories*).
3. Ange adressen till DriveLog-repot och välj typen **Integration**.
4. Sök efter **SMGD DriveLog** i HACS, välj **Ladda ner** och starta om Home Assistant.

### Manuellt

1. Kopiera katalogen `custom_components/smgd_drivelog/` från repot till
   `<config>/custom_components/smgd_drivelog/` i Home Assistant (där
   `configuration.yaml` ligger).
2. Starta om Home Assistant.

## 2. Skapa en API-token i DriveLog

1. Logga in i DriveLog och gå till **Inställningar → API-tokens**.
2. Skapa en token, t.ex. med namnet *Home Assistant*. Behörigheten ska vara **ha**.
3. Kopiera token direkt – den visas bara en gång. DriveLog sparar bara ett hashvärde.

Token ger endast åtkomst till Home Assistant-gränssnittet (`/api/v1/ha/*` och
`/api/v1/auth/whoami`). Den kan inte användas för att logga in i webbappen eller
ändra inställningar. Återkalla den när som helst under samma meny.

## 3. Lägg till integrationen i Home Assistant

1. **Inställningar → Enheter och tjänster → Lägg till integration → SMGD DriveLog**.
2. Fyll i:
   - **Adress till DriveLog**, t.ex. `https://drivelog.example.se` (utan `/api/v1`).
   - **API-token** från steg 2.
   - **Notify-tjänst** (valfri), t.ex. `notify.mobile_app_min_iphone`. Listan visar
     de `notify.mobile_app_*`-tjänster som finns i din Home Assistant.
3. Integrationen kontrollerar adress och token mot `GET /api/v1/auth/whoami` och
   att token har behörigheten `ha`.

Om token senare återkallas eller byts visar Home Assistant ett
*Återautentisering krävs*-meddelande: klistra in en ny token där. Notify-tjänst och
återkoppling ändras under **Konfigurera** på integrationen.

## Leveranslägen

Välj läge i DriveLog under **Inställningar → Integrationer → Home Assistant**
(inställningen `notifications.delivery_mode`).

### Pull-läge (rekommenderas)

Integrationen hämtar notiser från DriveLog var 30:e sekund
(`GET /api/v1/ha/outbox`), skickar dem med `notify.mobile_app_<enhet>` och kvitterar
(`POST /api/v1/ha/outbox/ack`).

- **Fördelar:** endast *utgående* anslutningar från Home Assistant. DriveLog behöver
  ingen åtkomst till Home Assistant och ingen long-lived token från Home Assistant
  lagras i DriveLog. Fungerar även om Home Assistant sitter bakom NAT/CGNAT.
- **Nackdelar:** upp till ~30 sekunders fördröjning. Kräver att integrationen är
  installerad och igång.
- Status *ansluten* visas först när integrationen har skickat ett livstecken
  (`POST /api/v1/ha/heartbeat`, var 5:e minut). Uteblir livstecknen i mer än
  10 minuter visas status *fel*; frågorna ligger kvar i utkorgen och levereras när
  integrationen hör av sig igen. En notis som inte kvitterats skickas igen – taggen
  gör att den ersätter sig själv i stället för att dyka upp två gånger.

### Push-läge

DriveLog anropar Home Assistants REST-API direkt
(`POST /api/services/notify/mobile_app_<enhet>`) med en long-lived access token.

- **Fördelar:** omedelbar leverans; fungerar för leveransen även utan att
  integrationen pollar.
- **Nackdelar:** DriveLog måste kunna nå Home Assistant, och en long-lived token
  med full åtkomst till Home Assistant lagras (krypterat) i DriveLog.
- Svaren kräver fortfarande integrationen – den tar emot knapptrycken och skickar
  dem till DriveLog.

Konfigurera push-läget i DriveLog:

1. Skapa en long-lived access token i Home Assistant (din profil → **Säkerhet** →
   **Långlivade åtkomsttokens**).
2. Ange Home Assistants adress (t.ex. `http://homeassistant.local:8123`), token och
   enhet i DriveLog och välj **Testa anslutning**. Status blir *ansluten* först när
   DriveLog har verifierat adress och token (`GET /api/` och `GET /api/config`).

Token från Home Assistant visas aldrig igen i DriveLog – API:t svarar bara om en
token finns (`has_token`). Om Home Assistant inte svarar ligger frågan kvar och
försöks igen med växande intervall (2, 4, 8 … högst 60 minuter); ingen fråga går
förlorad.

## Välja mobil enhet

Varje telefon med Companion-appen har en legacy-tjänst
`notify.mobile_app_<enhetsnamn>`. Den tjänsten krävs för knappar i notiser –
notify-*entiteterna* stöder bara titel och text.

Enheten väljs i DriveLog (listan fylls via Home Assistant i push-läget och via
integrationens livstecken i pull-läget). Har du inte valt någon enhet i DriveLog
används den som angetts i integrationen.

## Testnotis

I DriveLog: **Skicka testnotis**. Tryck på **Fungerar** i notisen – då visas
*bekräftad* i DriveLog och du vet att hela kedjan (leverans och svar) fungerar.
Testnotiser går fram även under tysta timmar.

## Notistyper

Alla texter är på svenska, med mil och decimalkomma.

| Typ | Exempel | Knappar |
| --- | --- | --- |
| Ny resa | *Ny resa: Hemmet → Örebro. 4,2 mil. Var detta arbete eller privat?* | Arbete, Privat, Granska, Senare |
| Syfte | *Du har en resa till Jönköping. Vad var syftet?* | Upp till tre tidigare bekräftade syften, Ange nytt syfte (textsvar), Öppna körjournal |
| Sammanslagning | *Två resor med 38 minuters stillestånd. Ska de slås samman?* | Ja, Nej, Granska, Senare |
| Samlingsnotis | *5 resor behöver granskas i körjournalen.* | Öppna granskning, Senare |
| Veckosammanställning | *Veckans körjournal: 48,6 mil i tjänsten, 8,2 mil privat och 3 resor som behöver granskas.* | Öppna körjournal |
| Daglig avstämning (av som standard) | *Dagens körjournal: 3 resor, 12,4 mil. 1 resa behöver granskas.* | Öppna granskning |

Efter **Arbete** frågar DriveLog direkt efter syftet om det saknas.
**Granska/Öppna** öppnar `…/m/q/<id>` (mobil snabbvy) eller `…/review` och ändrar
ingenting. **Senare** skjuter upp frågan (standard 120 minuter).

Endast resor som avslutats de senaste 48 timmarna ger notiser – aldrig importerad
historik – och demofordonet ger aldrig riktiga notiser.

### Svar och regler

- Det första giltiga svaret gäller. Dubbla knapptryck och omförsök ger
  *dubblett* utan ny ändring.
- En fråga som gått ut (standard 72 timmar) ändrar ingenting om du svarar i
  notisen; du får en kort återkoppling och kan svara i webbappen i stället.
- Besvaras eller avgörs frågan i webbappen tas notisen bort från telefonen
  (`clear_notification`).

## Inställningar

Under **Inställningar → Notiser** i DriveLog:

| Inställning | Standard | Betydelse |
| --- | --- | --- |
| Notistyper | ny resa, syfte, sammanslagning, vecka | Slå av/på varje typ. |
| Tysta timmar | 21:00–07:00 | Inga frågor skickas; de levereras när fönstret tar slut. |
| Max per dygn | 6 | Övriga frågor väntar till nästa dygn (0 = obegränsat). |
| Samlingsfönster | 10 min | Väntetid så att flera frågor kan samlas. |
| Samlingsnotis vid fler än | 3 | Fler väntande frågor → en samlingsnotis. |
| Påminnelse efter | 20 h | Påminnelse om obesvarad fråga (ersätter den gamla notisen). |
| Max påminnelser | 1 | |
| Frågan går ut efter | 72 h | Därefter bara i webbappen. |
| Dölj platser på låsskärmen | på | Låsskärmstexten innehåller inga platsnamn. |
| Veckosammanställning | söndag 18:00 | I din tidszon. |
| Daglig avstämning | 20:30 | Skickas bara om något behöver granskas. |

## Sensorer

| Sensor | Enhet | Källa |
| --- | --- | --- |
| Resor att granska | antal | öppna granskningsärenden |
| Obesvarade frågor | antal | öppna notisfrågor |
| Tjänstemil denna månad | mil | segment klassade som arbete (attribut `km`) |
| Privata mil denna månad | mil | segment klassade som privat (attribut `km`) |
| Senaste synk | tidpunkt | senaste lyckade synk mot bilen |

Sensorerna uppdateras var 5:e minut från `GET /api/v1/ha/summary`. Demodata
räknas aldrig in.

## Tjänst: `smgd_drivelog.flush_queue`

Skickar köade svar till DriveLog direkt (utan att vänta på backoff) och hämtar nya
notiser. Användbart efter att DriveLog varit nere.

## Sekretess

- Notistexter innehåller aldrig gatuadresser. Hemmet visas som *Hemmet*, platser
  du markerat som privata som *Privat plats*, övriga med platsnamn eller ort.
- Med **Dölj platser på låsskärmen** (på som standard) innehåller notistexten inga
  platsnamn alls, t.ex. *Ny resa: 4,2 mil. Var detta arbete eller privat?*.
  Detaljerna syns först när du öppnar DriveLog.
- iOS har inget sätt att via notisens innehåll dölja den på låsskärmen. Ställ in
  **Inställningar → Notiser → Home Assistant → Visa förhandsvisningar: När olåst**
  på iPhone. På Android: dölj känsligt innehåll på låsskärmen i systemets
  notisinställningar.
- Svarstexter (syften) lagras i integrationens kö på Home Assistant-servern tills
  de nått DriveLog. Diagnostikfilen innehåller varken token, adress eller svarstexter.
- API-token och Home Assistant-token visas aldrig i API-svar eller loggar.

## Felsökning

| Symptom | Åtgärd |
| --- | --- |
| Status *konfigurerad* men inte *ansluten* | Push: välj **Testa anslutning**. Pull: kontrollera att integrationen är installerad och att adressen stämmer – status blir *ansluten* vid första livstecknet. |
| Status *fel* i pull-läget | Inget livstecken på 10 minuter. Kontrollera att Home Assistant når DriveLog (t.ex. `curl https://drivelog.example.se/api/v1/health` från HA-värden) och Home Assistants logg. |
| *Återautentisering krävs* | API-token har återkallats – skapa en ny i DriveLog och ange den. |
| Notisen kommer men knapparna saknas | Kontrollera att en `notify.mobile_app_*`-tjänst används (inte en notify-entitet), och tryck länge/dra ned på notisen för att se knapparna. |
| Knapptryck ger ingen effekt | Kontrollera i **Utvecklarverktyg → Händelser** att `mobile_app_notification_action` med `DRIVELOG_…` tas emot. Titta på diagnostiken (kö och senaste fel). Kör `smgd_drivelog.flush_queue`. |
| Inga notiser alls | Kontrollera notistyper, tysta timmar och max per dygn i DriveLog, att resan är nyare än 48 timmar och att en enhet är vald. |
| Testnotis i push-läget misslyckas | Kontrollera Home Assistants adress (nås den från DriveLog?) och att long-lived token är giltig. |

Diagnostik: **Enheter och tjänster → SMGD DriveLog → ⋮ → Ladda ned diagnostik**.

## Demoläge

I DriveLog kan du slå på **demoläge** för Home Assistant. Notiserna visas då på en
simulerad telefon i webbappen, inklusive exakt den tjänstedata som skulle ha
skickats till Home Assistant. Status är *demo* – aldrig *ansluten* – och inga
anrop görs mot någon riktig Home Assistant.

## Vad som inte har kunnat verifieras

Följande har byggts enligt Home Assistants dokumentation men inte provats mot en
riktig installation och telefon:

- Att iOS-händelsen `mobile_app_notification_action` innehåller precis `action`,
  `reply_text` och `action_data` i alla appversioner (integrationen kräver bara
  `action`; frågans id ligger i åtgärdssträngen).
- Utseendet på knappar, textsvar (`behavior: textInput`), SF Symbols-ikoner och
  `push.interruption-level` på iPhone, samt hur många knappar som visas.
- Att `clear_notification` med samma `tag` tar bort notisen på alla iOS-versioner.
- Integrationens beteende i Home Assistant (konfigurationsflöde, reauth,
  sensorer, `Store`-kön och schemalagd hämtning) – logiken utan Home
  Assistant-beroenden är enhetstestad, men integrationen har inte körts i en
  riktig Home Assistant 2024.11+.
- Leverans till Android-enheter (formatet är detsamma men texter och knappar
  kan visas annorlunda).
