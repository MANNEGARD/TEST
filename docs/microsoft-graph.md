# Microsoft Outlook (Microsoft Graph)

DriveLog läser din Outlook-kalender för att ge resorna kontext: ett möte nära resans
ankomst, på en plats som stämmer med målet, är en stark indikation på tjänsteresa och ger
ett förslag till syfte. Kalendern är en informationskälla – aldrig ett bevis och aldrig en
instruktion till systemet.

Det finns två sätt att koppla kalendern:

- **Kalenderlänk (ICS)** – enklast. Ingen inloggning och ingen appregistrering. Passar dig som
  inte är administratör i din organisation. Se nästa avsnitt.
- **Appregistrering (Microsoft Graph)** – kräver att du kan registrera en app hos Microsoft.
  Se *Vad som krävs* längre ned.

Båda kan användas samtidigt.

## Utan appregistrering: kalenderlänk (ICS)

En kalenderlänk är en hemlig adress som Outlook skapar när du delar eller publicerar din
kalender. DriveLog hämtar kalenderfilen bakom länken och läser händelserna därifrån.

### Skapa länken i Outlook på webben

1. Öppna Outlook på webben och gå till *Inställningar* (kugghjulet) → *Kalender* →
   *Delade kalendrar*.
2. Under *Publicera en kalender*: välj din kalender och **Kan visa rubriker och platser**.
   (Väljer du *Kan visa när jag är upptagen* ser DriveLog bara upptagen-tid och kan inte
   föreslå några ärenden.)
3. Klicka *Publicera* och kopiera **ICS-länken**. En länk som börjar med `webcal://` fungerar
   också – DriveLog gör om den till `https://`.

### Lägg in länken i DriveLog

1. Gå till *Integrationer → Microsoft Outlook → Kalenderlänk (ICS)*.
2. Klistra in länken och klicka **Spara**. DriveLog hämtar kalendern direkt. Går det inte
   sparas ingenting, och du får veta varför i klartext.
3. **Synka nu** hämtar kalendern på nytt. Annars sker det av sig självt var 30:e minut.
4. **Ta bort länken** raderar länken och alla händelser som har hämtats från den.
   Kalenderträffarna på resorna försvinner, men resorna finns kvar.

### Länken är som ett lösenord

Den som har länken kan läsa kalendern. DriveLog sparar den krypterad, visar den aldrig igen
(bara vilken server den går till) och skriver den aldrig i loggar, ändringsloggen eller
felmeddelanden. Dela den inte med någon annan.

Har länken kommit i fel händer: gå till samma ställe i Outlook, klicka *Sluta publicera*
och publicera sedan igen. Då blir den gamla länken ogiltig och du får en ny. Klistra in den
nya i DriveLog med **Byt länk**.

### Vad som läses

Rubrik, tid, plats, ledig/upptagen och om händelsen är privat. Med *Läs hela bokningen*
under *Inställningar → Kalender* (påslaget från 1.3.0) läses också beskrivningen
(`DESCRIPTION`, högst 4 000 tecken) och deltagarna (`ORGANIZER`/`ATTENDEE`: namn ur `CN`,
annars adressens första del, och adressen; högst 50). Med appregistreringen hämtas på samma
sätt `body` (som ren text), `attendees` och `organizer`. Allt i kalendern behandlas som
opålitlig text – aldrig som instruktioner – och tvättas från osynliga tecken och styrtecken
innan det sparas. Möteslänkar, inringningsnummer, mötes-ID, lösenord och signaturer tas bort
innan något skickas till en AI-leverantör, och e-postadresser skickas aldrig dit.

Kalenderlänken innehåller bara det som delningsnivån i Outlook släpper igenom: med *Kan visa
rubriker och platser* finns varken beskrivning eller deltagare – dela med **Kan visa all
information**. Det är inte belagt att Outlooks delade länk alls tar med deltagare även på den
nivån, därför visar *Integrationer → Kalender* hur många bokningar som har beskrivning
respektive deltagare efter varje synk.

Deltagare med samma e-postdomän som din arbetsgivare räknas som kollegor (*Min arbetsgivares
e-postdomän* under *Inställningar → Kalender*; DriveLog fyller i den ur kalendern första
gången, så länge du inte har sparat fältet själv). Din egen adress känns igen ur bokningarna
och tas bort ur deltagarlistan; den sparas krypterad och visas aldrig.

Upprepade möten, undantag och flyttade eller inställda tillfällen hanteras, och tiderna
räknas om rätt även när Outlook skriver sina egna tidszonsnamn. Heldagsposter, inställda
möten och poster markerade som *Ledig* ignoreras, precis som med appregistreringen.

Synken använder samma fönster som appregistreringen (60 dagar bakåt och 14 dagar framåt,
inställbart). Händelser som har tagits bort i Outlook tas bort i DriveLog vid nästa synk.

### Tips som kan visas

- *Länken visar bara ledig/upptagen-tid* – du har delat med *Kan visa när jag är upptagen*.
  Dela på nytt med **Kan visa rubriker och platser** och klistra in den nya länken.
  DriveLog sparar inga händelser från en sådan länk.
- *Länken innehåller inga händelser i synkfönstret* – kontrollera att du har delat rätt kalender.
- *Ingen händelse i länken har någon plats* – förslagen blir svagare, eftersom DriveLog bara
  kan jämföra tider.
- *Kalenderlänken innehåller inga beskrivningar* (minst 5 bokningar, ingen med beskrivning) –
  dela kalendern med **Kan visa all information** och kopiera den nya länken.

### Båda sätten samtidigt

Har du både kalenderlänk och appregistrering används båda. Ett möte som finns i båda räknas
bara en gång (samma möte känns igen på dess id, eller på samma start, slut och rubrik – så även
upprepade möten). Att koppla bort Outlook (appregistreringen) rör inte länkens händelser, och att
ta bort länken rör inte Outlooks.

### Gränser

För att en trasig eller elak kalenderfil inte ska kunna belasta servern:

- Kalenderfilen får vara högst 10 MB och innehålla högst 25 000 händelser.
- Högst 5 000 tillfällen i synkfönstret läses; de senare hoppas över (ett tips visas).
- Mycket täta upprepningar (varje sekund, minut eller timme) och serier som skulle kräva
  orimligt många beräkningar hoppas över. En trasig händelse hoppas över, resten läses.
- Länken måste vara `https://` (eller `webcal://`) på den vanliga porten, utan användarnamn
  eller lösenord. Den får inte peka på en lokal adress, och högst tre omdirigeringar följs.

## Vad som krävs

- Ett Microsoft-konto (privat eller arbets-/skolkonto).
- En appregistrering i Microsoft Entra (kostnadsfri).
- För arbetskonton: att organisationen tillåter användare att ge samtycke till appar som
  läser kalendern. Kräver organisationen administratörsgodkännande behöver IT godkänna appen.

## 1. Registrera appen

1. Gå till [entra.microsoft.com](https://entra.microsoft.com) → *Identity* → *Applications* →
   *App registrations* → *New registration*.
2. **Name:** t.ex. `SMGD DriveLog`.
3. **Supported account types:** *Accounts in any organizational directory and personal
   Microsoft accounts* (då fungerar både privata och arbetskonton).
4. **Redirect URI:** plattform *Web* och adressen som visas i DriveLog under
   *Integrationer → Microsoft Outlook*, normalt
   `https://<din-domän>/api/v1/integrations/microsoft/callback`.
5. Registrera. Kopiera **Application (client) ID**.
6. *Certificates & secrets* → *New client secret*. Kopiera **Value** direkt (visas bara en gång).
   Hemligheten **går ut** efter den tid du väljer (högst 24 månader i portalen). Skriv upp
   datumet: när den har gått ut avvisar Microsoft inloggningen, och då räcker det inte att
   ansluta igen – skapa en ny hemlighet här och spara den i DriveLog.
7. *API permissions* → *Add a permission* → *Microsoft Graph* → *Delegated permissions* →
   lägg till `Calendars.Read` och `offline_access`. (`openid` och `email` begärs också vid
   inloggningen; de är standardbehörigheter och kräver inget administratörsgodkännande.)

## 2. Anslut i DriveLog

1. *Integrationer → Microsoft Outlook* → fyll i applikations-ID och klienthemlighet. Tenant
   lämnas som `common` (eller ange organisationens tenant-id om appen bara ska fungera där).
2. *Anslut* → logga in hos Microsoft och godkänn.
3. Välj vilka kalendrar som ska användas och klicka *Synka nu*.

## Behörigheter

| Behörighet | Varför |
| --- | --- |
| `Calendars.Read` | Läsa rubrik, tid, plats och onlinemötesflagga. Endast läsning |
| `offline_access` | Förnya åtkomsten utan ny inloggning |
| `openid`, `email` | Visa vilket konto som är anslutet |

Inget skrivs till kalendern. Händelsernas brödtext hämtas inte alls om du inte slår på
*Spara förhandsvisning av brödtext* under *Inställningar → Kalender*.

## Hur matchningen fungerar

En bokning kopplas till en resa bara när **både** tid och plats talar för det:

- **Tid:** resan anländer från två timmar före till 45 minuter efter mötets start (starkast
  inom en halvtimme före), anländer under mötet, eller avgår i anslutning till mötets slut
  (stöd för återresor). Fönstren är inställbara.
- **Plats:** koordinater om de finns, annars jämförs platsnamn, företag, gata och ort –
  egna förkortningar (*Inställningar → Förkortningar*, t.ex. HBG, JKPG) expanderas först.
  Bokningen behöver ingen plats (1.3.0): en ort eller ett företag i rubriken eller
  beskrivningen, eller en extern deltagares e-postdomän som stämmer med resmålets företag
  (`exempelakeri.se` ↔ "Exempel Åkeri AB"), ger en stark träff. Allmänna e-postdomäner (Gmail,
  Outlook, Hotmail, iCloud, Telia m.fl.) säger ingenting om företag och räknas inte.

Ett möte samma dag men långt från resan i tid matchas inte. Tid ensam ger bara en svag träff
(förslag, aldrig stöd för ett ärende). Onlinemöten (Teams, Zoom, Webex i plats eller
beskrivning, utan fysisk plats) förklarar aldrig en resa och kopplas inte. Heldagsposter, inställda möten och poster markerade som *Ledig*
ignoreras. Privata bokningar (känslighet *Privat*) talar för privat resa.

Har en bokning en fysisk plats på en **annan ort** samtidigt som resan ankommer någon
annanstans skapas ärendet *Konflikt mellan kalender och fordonsdata* i granskningscentret,
och resan klassas inte automatiskt.

I resedetaljen visas alla kandidater med poäng. Du kan välja en annan träff eller avvisa
den föreslagna – ditt val behålls vid senare synk.

## Drift

- Bakgrundsjobbet körs var 15:e minut, men varje anslutning synkas högst var 30:e minut
  (60 dagar bakåt, 14 dagar framåt – inställbart). Efter varje lyckad synk i bakgrunden
  klassas resorna om, precis som efter *Synka nu*.
- Efter tre fel i rad får anslutningen status *fel*. Den försöks ändå igen av sig själv,
  högst varannan timme, och blir *ansluten* igen vid första lyckade synk. En anslutning som
  behöver anslutas på nytt försöks inte.
- Med Home Assistant ansluten får du larmet *DriveLog: Kalendern synkas inte* i telefonen när
  anslutningen behöver anslutas på nytt, har fel eller inte har synkats på över ett dygn –
  en gång per fel och ingen notis när det fungerar igen.
- Åtkomsttoken förnyas automatiskt; den nya uppdateringstoken sparas varje gång. Tokens
  lagras krypterade.
- Vid 429 från Microsoft respekteras `Retry-After`.
- Återkallas åtkomsten visas *Behöver anslutas på nytt* – klicka *Anslut* igen.
- *Koppla bort* raderar tokens och alla kalenderposter som har hämtats med appregistreringen
  (kalenderlänkens händelser står kvar).
- Kalenderlänken synkas av samma bakgrundsjobb och med samma regler: högst var 30:e minut,
  efter tre fel i rad status *fel* och ett nytt försök högst varannan timme. Larmet
  *Kalendern synkas inte* gäller även länken när appregistreringen inte är ansluten.

## Inte verifierat mot verklig tjänst

Flödet är testat mot simulerade Graph-svar (OAuth med PKCE, sidindelning, tokenrotation,
`invalid_grant`, throttling). Inloggning mot en riktig tenant, organisationers
samtyckespolicyer och privata Microsoft-kontons tokenlivslängd har inte kunnat prövas utan
dina uppgifter.

Kalenderlänken är testad mot påhittade kalenderfiler formade som Outlooks (Windows-tidszoner,
upprepningar med undantag och flyttade tillfällen, heldag, ledig/upptagen, privata poster)
och mot simulerade servrar (omdirigeringar, fel, för stora svar, lokala adresser). Den har
inte provats mot en riktig länk från Outlook eller Microsoft 365.
