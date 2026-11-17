# SMGD DriveLog

DriveLog körs här i sin helhet: API:t, webbappen och en HTTPS-proxy (Caddy) som hämtar
certifikat från Let's Encrypt. Databasen (SQLite), nycklarna och certifikaten ligger i appens
egen datakatalog och tas med i Home Assistants säkerhetskopior.

Den fullständiga guiden – STRATO, routern, notiser och felsökning – finns i repot:
[docs/home-assistant-tillagg.md](https://github.com/SMGD-MP/TEST/blob/HEAD/docs/home-assistant-tillagg.md).

## Innan du startar

1. **Domänen** (t.ex. `drivelog.smgd.se`) måste peka på hemmets publika IPv4-adress – antingen
   genom att appen sköter STRATO DynDNS (se nedan), genom att routern gör det, eller genom att
   du sätter A-posten för hand (se *Utan DynDNS: adressvakten*).
   Domänen får **inte** ha någon AAAA-post (IPv6) – ta bort den som STRATO lägger in för sin
   parkeringssida, annars kan certifikatet inte utfärdas.
2. **Routern** ska skicka port **80** och **443** (TCP, och gärna 443 UDP) vidare till
   Home Assistant-maskinens lokala adress.
3. Ingen annan app får använda port 80 eller 443 på maskinen (t.ex. NGINX Proxy Manager).

## Inställningar (fliken Konfiguration)

| Fält | Betydelse |
| --- | --- |
| **Domän** (`domain`) | Domänen DriveLog nås på, t.ex. `drivelog.smgd.se` (utan `https://`). |
| **Administratörens e-post** (`admin_email`) | E-post för administratörskontot – blir inloggningen. |
| **Administratörens lösenord** (`admin_password`) | Minst 12 tecken. Fyll i för att skapa kontot första gången eller för att återställa ett glömt lösenord. Lämna gärna tomt annars. |
| **STRATO DynDNS** (`strato_dyndns`) | På: appen håller domänens A-post hos STRATO uppdaterad. Av: något annat (t.ex. routern) sköter det. |
| **STRATO-användarnamn** (`strato_dyndns_user`) | Domänen i ditt STRATO-paket, t.ex. `smgd.se`. |
| **STRATO DynDNS-lösenord** (`strato_dyndns_password`) | DynDNS-lösenordet du har satt hos STRATO. |
| **Loggnivå** (`log_level`) | `debug`, `info`, `warning` eller `error`. |
| **Extra domäner** (`extra_domains`) | Fler tjänster på samma server, en rad per domän: `transcribe.smgd.se=transcribe:8080`. Se [Extra domäner](#extra-domäner-fler-tjänster-på-samma-server). Lämna tomt annars. |

Tomma fält räknas som ej ifyllda. Kontot skapas **bara** härifrån: så länge det inte finns
startar appen bara API:t internt, och loggen säger vad du ska fylla i – webbappen och proxyn
startas inte, så ingenting är åtkomligt från internet.

**Lösenordet i fältet tillämpas bara när det har ändrats** sedan förra gången appen använde
det. Därför kan du byta lösenord inne i DriveLog som vanligt, även om det gamla står kvar i
fältet – det skriver inte över ditt nya vid nästa omstart. Töm gärna fältet när kontot finns.
Appen sparar inte lösenordet, bara ett kontrollvärde som inte går att räkna tillbaka.

## Glömt lösenordet

1. Fliken **Konfiguration**: skriv ett **nytt** lösenord (minst 12 tecken) i
   **Administratörens lösenord** och kontrollera att **Administratörens e-post** är adressen du
   loggar in med. Tryck **Spara**.
2. Fliken **Information**: tryck **Starta om**.
3. Fliken **Logg**: där ska stå `Lösenordet för <din adress> sattes från appens inställningar.`
4. Logga in med det nya lösenordet. Alla som var inloggade på kontot har loggats ut.
5. Töm gärna fältet igen och tryck **Spara** (ingen omstart behövs).
6. Nedladdningslänkar i tidigare notiser har slutat gälla.

Står det i stället att lösenordet **inte** ändrades:

- *"minst 12 tecken"* eller *"ser inte ut som en e-postadress"*: rätta fältet och börja om.
- *"det finns inget märke …"* eller *"märkesfilen … var trasig"*: appen vet inte om lösenordet
  i fältet är ett nytt eller ett gammalt som har blivit kvar (det händer t.ex. första gången
  efter uppdateringen från 1.0.1), och ändrar då ingenting för säkerhets skull. Skriv ett
  **annat** nytt lösenord och gör om steg 1–3.

Står adressen i **Administratörens e-post** inte på något konto skapas ett nytt
administratörskonto med den adressen (loggen varnar då).

## Första start

1. Fyll i fälten och tryck **Spara**.
2. Fliken **Information**: slå på **Starta vid uppstart** och **Vakthund**, tryck **Starta**.
3. Fliken **Logg**. När det fungerar står där:
   - att nycklarna och kontot skapades (bara första gången) och
     `Lösenordet för <din adress> sattes från appens inställningar.`,
   - `API:t är redo. Home Assistant-integrationen når det på http://<värdnamn>:8000` – spara
     värdnamnet, det behövs för notiserna,
   - `DriveLog startas på https://<din domän>`.
4. Öppna `https://<din domän>` och logga in. Första gången kan det ta någon minut innan
   certifikatet är på plats.

## Notiser i Home Assistant

Installera integrationen **SMGD DriveLog** via HACS (samma repo) och ange som adress
`http://<värdnamn>:8000` – värdnamnet från loggen, t.ex. `xxxxxxxx-drivelog`. API-token skapas
i DriveLog under **Integrationer → Home Assistant**. Port 8000 publiceras inte på maskinen och
nås inte från internet.

Har du exakt en telefon med Companion-appen väljs den av sig själv. Med flera telefoner väljer
du en i DriveLog; tills dess visar Home Assistant reparationsärendet *DriveLog har frågor men
ingen telefon är vald*, och frågorna väntar i DriveLog.

Från 1.3.0 visar notiserna hela adressen för platser utan namn och bostaden med sin adress
(*Dölj platser på låsskärmen* under *Inställningar → Notiser* döljer dem), och efter en
uppdatering kommer notisen *DriveLog är uppdaterad till …* med de viktigaste nyheterna – en gång
per version. HACS visar integrationens version som till exempel *1.3.0* (efter första utgåvan kan
du behöva trycka *Ladda om* i HACS) och integrationen får DriveLogs logga från Home Assistant
2026.3.

## Självgående körjournal

Från 1.1.0 sköter körjournalen sig själv utan Excel-uppladdning när Volvo är anslutet:

- Resorna kommer från bilen. Nya platser får gatuadress från **OpenStreetMap** (Nominatim).
  Platsens position skickas, aldrig resa, tid eller namn, men OpenStreetMap ser appens publika
  adress och när uppslagen görs. Från 1.3.0 slås också bostadens adress upp, och platser från en
  importerad körjournal som har adress men ingen position får en ungefärlig position ur
  adressen. Adressdata © OpenStreetMap-bidragsgivare
  (ODbL). Geokodningen stängs av under *Inställningar → Sekretess*.
- Tydliga resor godkänns av sig själva (egen regel, samma som de tre senaste gångerna,
  hemresa, resa till arbetet, privat plats, antaget kundbesök, och från 1.3.0 hem ↔ kontor och
  lunchregeln – båda stängs av under *Inställningar → Tjänsteställe* respektive *Lunchregeln*). Resten blir en fråga i taget
  i telefonen via Home Assistant. Välj *Jag godkänner allt själv* under *Inställningar →
  Automatik* om du hellre godkänner allt.
- När månaden är klar skapas underlaget, och notisen i telefonen har en länk som går att
  öppna utan inloggning.
  Vill du lämna in månaden i förväg trycker du *Skapa underlaget nu* på månadskortet under
  *Export*; vid månadsskiftet kontrolleras den igen.
- **Kom igång:** överst på *Översikt* visar kortet *Kom igång* vad som återstår – anslut
  bilen, markera bostaden (*Bilen står hemma nu* använder bilens senaste position) och anslut
  telefonen. Adresser slås upp först när bostaden är markerad. Kortet försvinner när allt är klart.
- **Kalendern (valfritt):** under *Integrationer → Microsoft Outlook* kan du klistra in en
  kalenderlänk (ICS) från Outlook på webben – ingen inloggning eller appregistrering behövs.
  Mötena ger förslag på ärende. Länken sparas krypterad och visas aldrig igen. Från 1.3.0
  läser DriveLog hela bokningen (beskrivning och deltagare) – dela kalendern med *Kan visa all
  information*. Stängs av under *Inställningar → Kalender*.
- **AI (valfritt):** med en AI-leverantör under *Integrationer → AI* får AI från 1.3.0
  *fullständigt underlag* (platsernas namn och adresser, dagens resor, kalenderbokningar,
  arbetsgivaren) för att skriva färdiga ärenden som du godkänner. Välj *Minimalt* under
  *Inställningar → Förslag och AI* om du vill skicka mindre. Koordinater, chassinummer, nycklar
  och kalenderlänken skickas aldrig.

**Om nedladdningslänken:** den gäller i 7 dagar och för högst 10 hämtningar. Notisen går via
Home Assistant, så den som har en Home Assistant-token eller kommer åt Home Assistants
historik kan hämta underlaget så länge länken gäller. Stäng alla länkar med *Stäng alla
nedladdningslänkar* på exportsidan. Länkarna stängs också när du byter lösenord, både inne i
DriveLog och när lösenordet återställs från fliken Konfiguration (se *Glömt lösenordet*).

Mer om läget, platsantagandet och månadsskiftet finns i
[docs/anvandning.md](https://github.com/SMGD-MP/TEST/blob/HEAD/docs/anvandning.md).

## STRATO DynDNS

Med **STRATO DynDNS** påslaget kontrollerar appen var femte minut att domänen fortfarande
leder hit, och skickar en uppdatering till STRATO bara när den inte gör det (och en gång per
dygn för säkerhets skull). Ingen annan tjänst tillfrågas. Fel som kräver åtgärd (fel lösenord,
okänd domän) stoppar uppdateringarna tills appen startas om – upprepade felaktiga försök kan
annars få STRATO att spärra domänen. DynDNS måste ingå i ditt STRATO-paket.

## Utan DynDNS: adressvakten

Ingår inte DynDNS i ditt STRATO-paket sätter du A-posten för hand och låter **STRATO DynDNS**
vara **Av**. Byter hemmet publik adress slutar sidan då att fungera – därför kontrollerar appen
var 15:e minut att domänen fortfarande leder hit (samma kontroll som DynDNS-delen, ingen annan
tjänst tillfrågas):

- Fungerar det står `<din domän> leder hit (A-posten <adress>).` i loggen.
- Fungerar det inte står det `<din domän> leder inte hit: A-posten pekar på <adress> …` med
  vad du ska göra: ändra A-posten hos STRATO till hemmets publika IPv4-adress (WAN- eller
  internetadressen på routerns statussida) och kontrollera att port 80 och 443 skickas vidare.
  Raden upprepas högst en gång per dygn.
- Har kontrollen **aldrig** fungerat kan det också bero på att routern saknar hårnåls-NAT – då
  fungerar sidan utifrån fast kontrollen misslyckas. Pröva från mobilnätet med wifi avslaget.
- Har kontrollen fungerat förut och misslyckas i cirka 45 minuter visas en **avisering i Home
  Assistant** (*DriveLog: &lt;domän&gt; leder inte längre hem*). Den försvinner av sig själv när
  domänen leder hem igen. För det behöver appen åtkomst till Home Assistants API; den används
  bara till aviseringen.

## Extra domäner (fler tjänster på samma server)

Äger DriveLog port 80 och 443 kan appens inbyggda proxy (Caddy) även ta hand om andra tjänster
på samma maskin: den hämtar ett certifikat för en extra domän och skickar trafiken vidare till
tjänstens container. Fyll i **Extra domäner** (`extra_domains`) med en rad per domän:

```
transcribe.smgd.se=transcribe:8080
```

- Formen är `<domän>=<värd>:<port>`. Värden är namnet på den andra tjänstens container på
  Dockers nät, utan `http://` och utan sökväg. Tjänsten bakom talar vanlig HTTP; WebSockets
  fungerar. `localhost`, `127.0.0.1` och andra IP-adresser går inte: inne i appens container är
  `127.0.0.1` DriveLog självt.
  I Home Assistant ligger appen på Supervisors interna nät och når andra appar på deras
  värdnamn.
- **Domänen måste peka hit** precis som DriveLogs egen (A-post, och ingen AAAA-post som pekar
  någon annanstans), annars kan certifikatet inte hämtas. Den får inte vara samma som `domain`.
- Den extra domänen får bara de allmänna säkerhetshuvudena (HSTS, `nosniff`, `Referrer-Policy`)
  – inte DriveLogs spärr för mikrofon och kamera eller förbudet mot inramning. Det bestämmer
  tjänsten själv.
- **Den extra tjänsten nås direkt från internet.** DriveLogs inloggning skyddar den inte, och
  den ligger på samma domän som DriveLog. Tjänsten måste själv ha inloggning och vara säker –
  lägg inte till en tjänst du inte litar på. Proxyn släpper igenom högst 50 MB per förfrågan.
- Med STRATO DynDNS uppdateras bara `domain`: gör den extra domänen till ett CNAME till
  DriveLogs domän, annars slutar den fungera när den publika adressen byts.
- Är en rad felskriven startar appen inte, och loggen säger vad som ska rättas. En rad som pekar
  på `localhost` eller en IP-adress (det föreslogs i 1.4) hoppas i stället över med en varning i
  loggen, och appen startar med de andra raderna.
- På en egen server sköts det här av installationsraden med `--extra-doman`, se
  [docs/installation.md](https://github.com/SMGD-MP/TEST/blob/HEAD/docs/installation.md#extra-domäner-fler-tjänster-på-samma-server).

## Säkerhetskopior och uppdateringar

Appen stoppas en kort stund medan Home Assistant tar en säkerhetskopia, så att databasen
alltid är hel. Kopian innehåller databasen, filerna, certifikaten **och krypteringsnyckeln** –
förvara den som en hemlighet. Saknas nyckeln men databasen finns startar appen inte; återställ
då ur en säkerhetskopia.

**Uppdatera** visas först när en ny version är färdigbyggd och går att hämta – tryck direkt.
Databasen uppgraderas automatiskt vid start, och Home Assistant tar en säkerhetskopia först om
du låter rutan för det vara ikryssad. Vilken version som körs står längst ned i DriveLogs
sidomeny och under *Inställningar → Om DriveLog*.

## Felsökning

- **`Det finns inget konto`:** fyll i e-post och lösenord för administratören, spara, starta om.
- **Glömt lösenordet:** se avsnittet *Glömt lösenordet* ovan.
- **Certifikatet utfärdas inte:** kontrollera att domänen pekar på rätt adress (A-post), att
  ingen AAAA-post finns och att port 80 och 443 når Home Assistant-maskinen utifrån.
- **Fungerar utifrån men inte hemma:** routern saknar troligen "hårnåls-NAT". Se guiden.
- **Aviseringen *… leder inte längre hem*:** se *Utan DynDNS: adressvakten* ovan.
- **Appen startar inte:** loggen börjar med vad som behöver rättas.
- **Konstiga resor eller förbrukning från bilen:** ladda ner felsökningsfilen under
  *Integrationer → Volvo* och skicka den till den som hjälper dig. Filen innehåller inga
  chassinummer, registreringsnummer eller positioner. Från 1.4.2 innehåller den också de senaste
  interna felen med ett tvättat felmeddelande: kända nycklar, sparade platser och resornas
  adresser tas bort, men ett okänt platsnamn eller en okänd adress kan finnas kvar i texten.
- **Raden *Avläsningen hämtades, men …* under *Senaste fel*:** ett steg i bearbetningen av en
  resa misslyckades. Resan finns i journalen. Raden står kvar tills en resa har bearbetats utan
  fel. Ladda ner felsökningsfilen och skicka den till den som hjälper dig.
