# Ändringar

> Från 1.0.2 visar Home Assistant **Uppdatera** först när den nya versionen är färdigbyggd och
> går att hämta – det går bra att trycka direkt.

## 1.1.0

Körjournalen sköter sig själv utan Excel-uppladdning: resorna kommer från bilen, får adress,
kategori och ärende, och månadens underlag skapas av sig självt. Importen fungerar som förut
för den som vill använda den.

- **Självgående läge** (nytt, påslaget som standard). Under *Inställningar → Självgående läge*
  väljer du *Självgående* eller *Jag godkänner allt själv*. Bara resor från bilen som kommer in
  efter att läget slogs på berörs – importerade, manuellt inlagda och äldre resor rörs aldrig.
  En resa godkänns bara automatiskt av ett av skälen *Din regel för …*, *Som de 3 senaste
  gångerna*, *Hemresa efter …*, *Resa till arbetet*, *Privat plats* eller *Antaget kundbesök*.
  Skälet står på resan. Allt annat blir en fråga i telefonen.
- **Adresser till nya platser.** När bilen stannar på en okänd plats slår DriveLog upp
  gatuadressen i OpenStreetMap (Nominatim). Bara mittpunkten av en liten ruta (koordinaten
  med fyra decimaler) skickas, aldrig resa, tid eller namn. OpenStreetMap ser serverns adress och
  när uppslagen görs. Geokodningen är nu **på som standard**; den stängs av under
  *Inställningar → Sekretess*. Går uppslaget inte att göra står adressen tom – ingen adress
  hittas på.
- **Antaget kundbesök.** Stannar du på en ny plats som kartan visar som företag eller industri
  med namn, en vardag inom kontorstid (standard mån–fre 07–18) som inte är en röd dag eller
  afton, räknas resan som *Kundbesök &lt;namn&gt;*. Platser med arbetsgivarens namn eller inom
  1 km från tjänstestället, och myndigheter, advokater, mäklare och liknande antas aldrig –
  där får du en fråga. Du får en notis första gången per plats och kan säga emot under
  vetotiden (standard 1 dygn). Ett *Fel* gör att platsen aldrig antas igen och att övriga
  antagna resor dit frågas om, också i en månad som redan har skickats. Ändrar du kategori
  eller ärende på en godkänd tjänsteresa frågas hemresan efter den om. Antagna rader märks inte i Excel, men du ser dem före
  nedladdningen.
- **Starttid ur bilens egna uppgifter.** Räcker bilens avläsningar räknas starttiden fram och
  visas som *≈ 07:35 (±4 min)* i appen och *ca 07:35* i underlaget, med en fotnot. Annars är
  starttiden okänd, som förut; du kan ange den själv med *Ange starttid*.
- **En fråga i taget i telefonen.** Kategori och ärende besvaras i samma tryck (*Tjänst:
  &lt;ärende&gt;*, *Tjänst – skriv ärende*, *Privat*, *Alltid privat här*, *Senare*).
  Påminnelse efter 24 h, och efter 48 h läggs frågan undan så att nästa får plats. Vid
  månadsskiftet ställs månadens undanlagda frågor om en gång. Standardvärdena för påminnelse
  och utgång har ändrats från 20/72 h till 24/48 h om du inte själv har satt dem.
- **Platser visas på låsskärmen.** *Dölj platser på låsskärmen* är nu av som standard om du
  aldrig har ändrat inställningen. Hemmet visas som *Hemmet*, privata platser som *Privat
  plats* och okända platser bara som namn eller gata utan husnummer och ort.
- **Månadens underlag av sig självt.** När månaden är slut och allt är besvarat skapas filen,
  och du får notisen *Oktober klart* med en länk som går att öppna i telefonen utan att logga
  in. Väntar något säger notisen vad. Ändras något efteråt skapas en ny fil (*Oktober
  uppdaterad*), och den gamla länken slutar gälla. Blir månaden helt utan tjänsteresor
  återkallas filen. En saknad starttid stoppar inte underlaget
  men står i notisen och på exportsidan. Exportsidan har ett nytt månadskort.
- **Nedladdningslänken** gäller 7 dagar och högst 10 hämtningar. Den stängs med *Stäng alla
  nedladdningslänkar* på exportsidan och när du byter eller återställer lösenordet. Den som har
  Home Assistants token eller kommer åt Home Assistants historik kan hämta filen så länge
  länken gäller.
- **Larm i telefonen** när något har slutat fungera: Volvo-inloggningen går snart ut eller
  behöver göras om, hämtningen från Volvo fungerar inte, bilen har inte hört av sig på 4 dygn
  eller kalendern synkas inte. Ett larm per fel, högst två per dygn.
- **Home Assistant:** finns exakt en telefon väljs den av sig själv. Saknas telefon visas ett
  reparationsärende i Home Assistant och en banderoll i DriveLog, och frågorna väntar i stället
  för att försvinna. Når integrationen inte DriveLog på 30 minuter skickas en lokal notis.
  Integrationen har version 1.1.0.
- **Kalendern** försöker igen av sig själv (högst varannan timme) efter upprepade fel, och
  resorna klassas om efter varje synk.
- **Sammanslagning i självgående läge:** resor slås bara ihop vid ett kort stopp (högst
  15 minuter) vid en bekräftad tank- eller laddstation, och ingen sammanslagningsfråga ställs.
  Ett stopp hemma eller vid en privat plats slås aldrig ihop för resor från bilen – det gäller
  i båda lägena, liksom den uträknade starttiden.
- Exportens varning *osäker datakvalitet* för varje resa från bilen är ersatt med konkreta
  kontroller: saknad starttid, saknad adress, möjliga missade resor och privat adress i
  underlaget.
- **Kom igång på översikten.** I självgående läge visar ett kort de tre stegen som återstår:
  anslut bilen, markera bostaden och anslut telefonen. Bostaden markeras med ett tryck på
  *Bilen står hemma nu* (bilens senast rapporterade position, med tidpunkten), eller på kartan.
  Utan markerad bostad slås inga adresser upp – nu står det varför, på översikten, i ett larm
  i telefonen och i månadsläget.
- Starttiden ur bilens trippmätare är byggd men avstängd tills den har prövats mot en riktig
  bil.

## 1.0.3

- **Adressvakt när STRATO DynDNS är avslaget.** Har du satt domänens A-post för hand (t.ex.
  för att DynDNS inte ingår i ditt STRATO-paket) kontrollerar appen nu var 15:e minut att
  domänen fortfarande leder hem. Gör den inte det står det i loggen vad A-posten pekar på och
  vad du ska göra – första gången och sedan högst en gång per dygn.
- Har kontrollen fungerat tidigare och misslyckas tre gånger i rad (cirka 45 minuter) visas
  också en **avisering i Home Assistant**: *DriveLog: &lt;domän&gt; leder inte längre hem*. Den
  försvinner av sig själv när domänen leder hem igen. Appen behöver därför åtkomst till Home
  Assistants API (`homeassistant_api`) – Home Assistant kan be dig godkänna det vid uppdateringen.
- Ingen ny tjänst tillfrågas: kontrollen är densamma som DynDNS-delen använder och går bara till
  din egen domän. Varningen för en AAAA-post körs nu i samma vakt, inte bara en gång vid start.

## 1.0.2

- **Glömt lösenord kan återställas i appen.** Skriv ett nytt lösenord i *Administratörens
  lösenord*, spara och starta om appen: lösenordet sätts för kontot med adressen i
  *Administratörens e-post* (kontot skapas om det saknas), alla inloggade sessioner för kontot
  avslutas och loggen bekräftar det. Fältet tillämpas bara när det har **ändrats** sedan det
  senast användes, så ett lösenord som står kvar skriver aldrig över ett som du har bytt inne i
  DriveLog. Appen sparar bara ett nyckelat kontrollvärde, aldrig lösenordet.
- Ett för kort lösenord eller en felaktig adress i fälten stoppar inte längre appen när kontot
  redan finns – DriveLog startar som vanligt och loggen säger att lösenordet inte ändrades.
- Första gången efter uppdateringen från 1.0.1 ändras inget lösenord om fältet redan är ifyllt
  (appen kan inte veta om det är ett gammalt lösenord). Vill du återställa: skriv ett nytt.
- En ny version erbjuds i Home Assistant först när avbilden är publicerad och går att hämta
  utan inloggning, så installation och uppdatering misslyckas inte längre med
  `manifest unknown` medan en version byggs.

## 1.0.1

- Säkerhet och robusthet efter granskning: första kontot skapas bara från appens inställningar
  (inte via API:t av en annan app), förfalskad `X-Forwarded-For` direkt mot port 8000 kringgår
  inte längre inloggningsspärren, `/data` ägs av root med databasen i `/data/app`, nycklarna
  läses bara säkert och ersätts aldrig tyst när databasen finns, alla fält syns direkt i fliken
  Konfiguration, DynDNS-kontrollen är fråga–svar och godtar bara publika adresser, och
  AAAA-varningen gäller även när routern sköter DynDNS.

## 1.0.0

- Första versionen av SMGD DriveLog som Home Assistant-tillägg: API, webbapp och Caddy
  (automatisk HTTPS via Let's Encrypt) i en container, med SQLite och bakgrundsjobben i API:t.
- Webbapp och proxy startas först när ett administratörskonto finns.
- Nycklarna skapas slumpmässigt första gången och sparas i tilläggets data (följer med i
  Home Assistants säkerhetskopior).
- Valfri STRATO DynDNS-uppdatering som bara frågar STRATO, med varning för AAAA-poster.
- Färdiga avbilder för amd64 och aarch64.
