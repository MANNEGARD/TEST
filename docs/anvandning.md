# Användning

DriveLog är byggt för att sköta sig självt: **resorna kommer från bilen → tydliga resor
avgörs av sig själva och resten blir en fråga i telefonen → när månaden är slut skapas
underlaget till arbetsgivaren av sig självt.**

Du äger bilen och redovisar bara tjänsteresorna. Privat körning syns som luckor i
mätarställningen mellan resorna – det är väntat och ger aldrig fel eller varningar.

## Månadsrutinen

Med Volvo och Home Assistant anslutna och *Självgående läge* påslaget (standard):

1. **Under månaden** svarar du på frågorna i telefonen när de kommer – en i taget, med
   kategori och ärende i samma tryck. Antagna kundbesök får en notis första gången per plats;
   säg emot om det är fel.
2. **Vid månadsskiftet** kommer notisen *Oktober klart* med siffrorna och knappen *Öppna
   underlaget*. Väntar något (en obesvarad fråga, bilens sista avläsning, en vetotid) säger
   notisen vad, och underlaget skapas när det är klart.
3. **Se antagna** i notisen visar raderna som DriveLog har antagit eller godkänt av sig självt,
   med skäl, *Stämmer* och *Fel*. Gå igenom dem innan du skickar.
4. Skicka filen till arbetsgivaren.

Utan Home Assistant fungerar samma flöde i webben: frågorna finns under *Granskning* och
månadskortet på *Export* visar läget och knappen *Hämta underlaget*. Väljer du *Jag godkänner
allt själv* gäller den tidigare rutinen: granska allt och klicka *Skapa Excel* under *Export*.

Import av Volvos körjournal (Excel) behövs inte, men fungerar som förut om du vill använda
den – till exempel för historik från tiden innan bilen kopplades in.

## Hur resorna kommer in

| Källa | Hur |
| --- | --- |
| **Volvo API** | Bilen läses av automatiskt och resor rekonstrueras. Nya platser får adress från OpenStreetMap. Starttiden räknas fram ur bilens avläsningar när de räcker, annars uppskattas den ur sträckan (se *Starttid* nedan) |
| **Import** | Ladda upp körjournalen från Volvo Cars-appen (XLSX). Verifierade resor med tider, adresser, mätarställning och förbrukning |
| **Manuellt** | *Körjournal → Lägg till resa* |

När samma resa senare importeras från Volvos körjournal ersätter den verifierade resan den
rekonstruerade, och dina beslut följer med.

**När resan syns (1.5.0).** Skickar bilen position och mätarställning när du slår av
tändningen syns resan i journalen vid första hämtningen efter ankomsten (med hämtning varannan
minut alltså inom ett par minuter). Saknas något – till exempel mätarvärdet kommer en stund
efter positionen – väntar DriveLog som förut tills uppgifterna har lagt sig. Kommer mätarvärdet
efter att resan redan syns rättas resans sträcka i stället för att det blir en extra resa, och
rättningen syns i resans historik. Frågan i telefonen kommer som förut tidigast en stund
(normalt tio minuter) efter ankomsten, så att en körning som fortsätter efter ett kort stopp
hinner slås ihop först.

## Självgående läge

Under *Inställningar → Automatik* finns ett val: **Självgående** (standard) eller **Jag
godkänner allt själv**. Läget gäller bara resor från bilen som kommer in efter att det slogs
på (*Självgående sedan …*). Importerade och manuellt inlagda resor, och resor från före dess,
rörs aldrig.

### Kom igång

Överst på *Översikt* visas kortet **Kom igång** i självgående läge så länge något av de tre
stegen återstår. Varje steg har en status och en knapp eller länk:

1. **Anslut bilen (Volvo)** – under *Integrationer*. Demobilen räknas inte.
2. **Markera bostaden** – ställ dig hemma med bilen och tryck **Bilen står hemma nu**. Bilens
   senast rapporterade position blir din bostad, och kortet visar när bilen rapporterade den
   (*Markerad med bilens position kl. 21:14*). Skriv gärna bostadens adress (gatuadress och
   ort) – den står i underlaget i stället för ordet *Hem*. Lämnar du fältet tomt påminner kortet
   om det, och du fyller i adressen senare under *Platser* (se *Adresser* nedan). Har bilen inte rapporterat någon position än säger
   kortet det. Vill du hellre peka ut bostaden själv väljer du *Markera på kartan i stället*
   (sidan Platser). Först när bostaden är markerad slår DriveLog upp adresser för dina resor –
   hemadressen skickas aldrig till karttjänsten, och stopp vid bostaden slås aldrig upp.
3. **Anslut telefonen (Home Assistant)** – under *Integrationer*, och välj vilken telefon
   frågorna ska gå till.

När alla steg är klara försvinner kortet (en gång visas *Klart – resten sköter sig självt*).
Kortet visas inte i läget *Jag godkänner allt själv*. Är bilen ansluten och har kört eller
rapporterat något sedan läget slogs på, men bostaden inte är markerad, får du ett larm i
telefonen: *DriveLog kan inte slå upp adresser än: markera din bostad i DriveLog (Översikt →
Kom igång).* Samma förklaring står i månadsläget och månadsnotisen så länge resor väntar på
adress av det skälet.

### Så avgörs en resa

När bilen har stått still i 10 minuter skapas resan. Ligger stoppet vid en känd plats används
den; annars skapas en ny obekräftad plats och adressen slås upp (se *Adresser* nedan). När
adressen är klar, eller efter högst 30 minuter, prövas reglerna i den här ordningen:

| Skäl på resan | När | Blir |
| --- | --- | --- |
| *Resa till arbetet* | Hemmet ↔ ordinarie tjänsteställe utan stopp, när du bara redovisar tjänsteresor | Privat |
| *Privat plats* | Start och mål är hemmet eller platser du har märkt som privata | Privat |
| *Din regel för …* | Du har en egen regel för rutten eller platsen | Det regeln säger. En regel för Arbete utan ärende ger en ärendefråga |
| *Hem ↔ kontor i …* | Hemmet ↔ ett av arbetsgivarens kontor, när du har valt att de resorna är tjänsteresor (se *Hem ↔ kontor* nedan) – oavsett tid och veckodag | Tjänst, ärende ur kalendern eller "Resa till kontoret i <ort>" |
| *Lunch* | Hela lunchmönstret stämmer (se *Lunchregeln* nedan) | Privat, båda resorna |
| *Kvällsärende under tjänsteresa* | Kort tur till eller från hotellet utanför kontorstid | Alltid en fråga |
| *Som de 3 senaste gångerna* | Samma rutt med samma kategori de tre senaste gångerna **du** godkände den; för Arbete även samma ärende, samma typ av dag och liknande sträcka (±15 %) | Samma kategori och ärende |
| *Hemresa efter …* | Hem samma dag efter en godkänd tjänsteresa, från samma plats och ungefär lika lång (±15 %) | Tjänst, "Hemresa efter <ärende>" |
| *Antaget kundbesök* | Se *Platsantagandet* nedan | Tjänst, "Kundbesök <namn>", med vetotid |
| – | Allt annat | En fråga |

Det här blir **alltid** en fråga, utan automatik:

- mätarställning saknas, resan kan vara flera körningar (*Kan vara flera körningar* står då i
  frågan), resan blandar privat och tjänst, kalendern säger emot bilens uppgifter, eller du
  har regeln *Fråga mig alltid*.

Och det här blir **aldrig** automatiskt Arbete (privata regler gäller ändå):

- något pekar mot privat, till exempel en privat kalenderbokning, en privat plats eller en
  tidigare rättelse,
- resan går mellan hemmet och det ordinarie tjänstestället (utom när du har valt *Hem ↔ kontor*),
- målet är hemmet (utom hemresa), en privat plats, tjänstestället eller ett hotell (utom med
  egen regel).

Inget godkänns som bara vilar på en kalenderrubrik, en schablontext eller en plats du inte
har bekräftat. En automatiskt godkänd resa räknas aldrig som "dina tre senaste gånger".

Resor som godkänts av sig själva märks **Godkänd automatiskt · <skäl>** och antagna resor
**Antaget**, i listan och på resans sida, där knappen *Ändra* finns. Rättar du en resa gäller
ditt beslut: en regel som slog fel frågar dig i stället nästa gång ("Regeln för X frågar dig
nu i stället"), och en rättad *Som de 3 senaste gångerna* gör att den inte gäller rutten längre.

### Hem ↔ kontor

Under *Inställningar → Tjänsteställe* finns valet **Resor mellan hemmet och våra kontor är
tjänsteresor och godkänns automatiskt**. Påslaget godkänns resor mellan hemmet och vilket som
helst av arbetsgivarens kontor (platser markerade som kontor eller ordinarie arbetsplats) som
tjänsteresor, även på kvällar och helger, med skälet *Hem ↔ kontor i <ort>*. Mätarställning som
saknas, flera körningar, kalenderkonflikt och *Fråga mig alltid* stoppar som vanligt. Skatteverkets
huvudregel är att resor mellan bostaden och det ordinarie tjänstestället är privata – att räkna dem
som tjänsteresor är ditt val och ditt ansvar gentemot arbetsgivaren. **Efter uppdateringen till
1.3.0 är valet påslaget i din installation**; stäng av det under Tjänsteställe om du inte vill ha
det. Avstängt gäller det tidigare (*Resa till arbetet*, privat).

### Lunchregeln

Korta turer från kontoret mitt på dagen är oftast lunch. När **hela** mönstret stämmer blir
båda resorna privata utan att du får någon fråga:

1. Resan börjar vid ett av era kontor en arbetsdag.
2. Den är kort: högst 10 minuter och 8 km.
3. Du stannar mellan 20 och 45 minuter på samma ställe.
4. Du kör lika kort tillbaka till samma kontor, och allt sker mellan 11:00 och 13:00.

Regeln gäller inte om stället är en bekräftad kund eller arbetsplats, om du har en egen regel för
det, om du tidigare har sagt att en resa dit var i tjänsten, om kartan säger kontor eller
industri, eller om en kalenderbokning stämmer med resan. Säger kartan restaurang, kafé eller butik
räknas det som ett starkare tecken. Klockslaget ensamt gör aldrig en resa privat.

Du får ingen fråga om första resan medan du äter: DriveLog väntar på återresan. Kommer ingen
återresa inom stoppets längsta tid + körtiden + 10 minuter (65 minuter med standardgränserna)
hanteras resan som vanligt. Gränserna ändras och regeln stängs av under *Inställningar →
Lunchregeln*. Regeln kräver att minst ett kontor finns som bekräftad plats – annars står en
varning där. Den är påslagen från början och gäller även resor som väntar på svar när
uppdateringen installeras. I läget *Jag godkänner allt själv* blir lunchresorna förslag på Privat.

Åker du hem och sedan tillbaka till samma kontor enligt mönstret görs resorna inte privata av
sig själva. Har du valt att resor mellan hemmet och kontoren är tjänsteresor godkänns de inte
heller automatiskt som tjänst – du får en fråga när du är tillbaka på kontoret.

### Platsantagandet

OpenStreetMap säger ofta vad en plats är. DriveLog delar in platsen i tre arter:

| Art | Kartans typ (exempel) |
| --- | --- |
| Arbetsplats | kontor, hantverk, industri, verkstad, lager- och kontorsbyggnad |
| Aldrig | butik, restaurang, skola, vård, fritid, turism, bostadshus |
| Oklar | allt annat, även inget kartobjekt |

En resa **antas** vara *Kundbesök <namn>* när allt detta stämmer: ingen av spärrarna ovan
gäller och ingen egen regel eller historik har avgjort resan, målet är en ny obekräftad
arbetsplats med namn i kartan (eller en oklar plats med namn som AI-leverantören bedömer som
arbete, om en sådan är inkopplad), du har inte sagt *Fel* om platsen tidigare, och stoppet
ligger inom kontorstid (standard mån–fre 07–18) och inte på en röd dag eller afton (t.ex.
juldagen, långfredag, midsommarafton).

Inget antas heller – det blir en vanlig fråga – när platsens namn innehåller din arbetsgivares
namn (*Inställningar → Allmänt*), när platsen ligger inom 1 km från ditt tjänsteställe, eller
när kartan visar en myndighet, advokat, mäklare, försäkringsbolag, församling,
arbetsförmedling, förening eller ett vineri/bryggeri. Där uträttar man ofta privata ärenden.

- **Notis första gången per plats:** *Antaget: Kundbesök Exempel AB · tis 2,3 mil · Räknas som
  tjänst i morgon 14:05 om du inte säger emot*, med *Stämmer*, *Fel – privat* och *Annat
  ärende*. Senare besök antas utan notis men är fortfarande märkta *Antaget*.
- **Vetotid:** resan kommer med i månadens underlag först när vetotiden har gått (standard 1
  dygn; välj *Fråga mig först*, *1 dygn* eller *3 dygn*). Med *Fråga mig först* antas
  ingenting – kundbesöket blir i stället första knappen i en vanlig fråga.
- **Utanför kontorstid** antas inget, men frågan får knappen *Tjänst: Kundbesök <namn>*.
  Klockslaget räknas aldrig som tecken på privat.
- **Namn saknas:** en arbetsplats utan namn i kartan ger frågan *Vilket företag var det?*. Ditt
  svar blir platsens företag och en egen regel.
- **Stämmer** eller ett företagsnamn gör platsen till en bekräftad kund med en egen regel
  (*Din regel för …*).
- **Fel** gör att platsen aldrig antas igen. Övriga antagna resor dit öppnas och frågas om.
  Ligger någon av dem i en månad som redan har skickats, skapas ett uppdaterat underlag när
  du har svarat.
- **Hemresan följer utresan:** ändrar du kategori eller ärende på en godkänd tjänsteresa
  frågas hemresan efter den (*Hemresa efter …*) om igen.
- Antagna rader märks inte i Excel-filen. Du ser antalet i månadsnotisen och listan under
  *Se antagna* innan du laddar ned filen.

### Adresser

När bilen stannar på en okänd plats slår DriveLog inom någon minut upp adressen i
OpenStreetMap (Nominatim). Från 1.3.0 skickas platsens egen position (sex decimaler), så att
rätt hus hittas – aldrig resa, tid eller namn. Svaret sparas per liten ruta (fyra decimaler,
ungefär 11 × 6 meter), så samma ruta slås bara upp en gång. Ingenting skickas
förrän hemmet finns som plats med koordinat (se *Kom igång* ovan). Svaret ger gatuadress och, om kartan har det,
namn och typ. Matchar adressen en plats som redan finns, till exempel från en import, används
den. Går uppslaget inte att göra står adressen tom; ingen adress hittas på.

OpenStreetMap ser serverns publika adress och när uppslagen görs. Geokodningen stängs av under
*Inställningar → Sekretess*; då får nya platser ingen adress och du namnger dem själv.
Adressdata © OpenStreetMap-bidragsgivare, under licensen ODbL.

**Adressen i underlaget.** Underlaget skriver alltid start och mål med gatuadress och ort,
aldrig *Hem*. En bekräftad plats med eget namn står som *Acme AB, Kundvägen 2, Exempelstad*,
bostaden med sin adress – även en manuell eller importerad resa där det står "Hem". En privat
plats står bara med sin adress, utan namnet. I appen i övrigt visas platsernas namn som förut.
Från 1.3.0 slås också bostadens adress upp av sig själv när bostaden har en position men ingen
adress; bara adress och ort fylls i, namnet *Hemmet* ändras inte. Saknar bostaden eller en
annan bekräftad plats adress eller ort står det på exportsidan (*Bostaden saknar adress – fyll
i den under Platser*), och månadens underlag skapas inte förrän den är ifylld. Öppna platsen under **Platser** och
skriv adressen i fältet **Adress**, eller tryck **Hämta adressen från kartan**: då skickas
just den platsens position (fyra decimaler) en gång till karttjänsten (standard
OpenStreetMap), och svaret fylls i fältet så att du kan kontrollera det innan du sparar. Svaret
sparas, så ett nytt tryck skickar ingenting, och uppslagen räknas mot samma gräns som övriga
(500 per dygn). Knappen kräver att platsen har en
sparad position och att geokodningen är på.

På exportsidan står saknad adress också direkt i förhandsvisningens cell för start eller mål:
*Adress saknas – lägg till under Platser*.

**Positioner ur adresser (1.3.0).** En importerad resa har adresser men inga positioner. Platser
som har en adress men saknar position slås upp åt andra hållet – adressen skickas till samma
karttjänst och svaret blir en position. Det sker i bakgrunden, högst tre platser åt gången och
inom samma gränser som övriga uppslag (en per sekund, 500 per dygn), utan att du gör något.
Positionen märks *ungefärlig position ur adressen* på platsen och på resans karta. Medan det
pågår visar kartan *Positionen slås upp*. Hittar kartan inte adressen, eller ger den flera
möjliga platser, står det *Adressen hittades inte på kartan*; sätt då ut platsen själv under
**Platser**. En sådan adress prövas inte igen förrän du ändrar den. En position som du har satt
eller som kommer från bilen skrivs aldrig över; en ungefärlig position ersätts däremot av bilens
när bilen kommer dit. Hamnar en uppslagen plats vid bostaden eller ett kontor som saknar adress
slås de ihop, och bostaden eller kontoret får adressen. Har båda olika adresser, är den uppslagna
platsen en bekräftad kund (eller har företag eller en egen regel), eller ligger flera olika
adresser vid bostaden, visas i stället ett förslag om att slå ihop dem under Platser.

### Starttid

**Motorstarten (från 1.5.0).** Bilen rapporterar själv när motorn startar, med egen tid, även
när position och mätarställning kommer först när tändningen slås av. DriveLog använder den som
resans starttid: bilens egen tid när den ligger mellan två hämtningar. Tiden visas som en
vanlig tid, utan "≈", och utan "ca" i underlaget. Saknas bilens tid används minuten mitt
emellan hämtningarna när de ligger högst 6 minuter isär; den är uträknad och visas därför som
**≈ 06:15 (±1 min)** och **ca** i underlaget. Ett tryck visar grunden ("Bilen rapporterade själv att
motorn startade 06:15 …"). Den ändras aldrig av sig själv – bara du kan ange en annan tid med
**Ange**. Skulle motorstarten ge en orimligt hög snittfart för sträckan (en laddhybrid som
startar på el kan starta motorn först en bit in i resan) används den inte; då gäller reglerna
nedan. Resor från de senaste två veckorna som saknar starttid eller har en uppskattad starttid
rättas vid nästa hämtning, och ändringen syns i resans historik – utom resor där du angett
starttiden och resor som finns i ett underlag som redan är skapat. En resa räknas som skyddad bara om den finns i den skapade filen: har månadens fil skapats i förväg får en resa som tillkommer efteråt starttid och förbrukning som vanligt.

Volvo rapporterar bilens position först när en körning är slut. Räcker bilens avläsningar
under färden räknar DriveLog fram starttiden och visar den som **≈ 07:35 (±4 min)**; ett tryck
visar grunden ("Bilen stod still … och var på väg …"). I underlaget skrivs den **ca 07:35**,
med en fotnot.

Rapporterar bilen bara när resan är slut **uppskattas starttiden ur sträckan**: körtiden är
sträckan delad med en snittfart, och starttiden är ankomsten minus körtiden – aldrig utanför
det fönster bilens avläsningar tillåter. Snittfarten tas från dina egna resor med kända tider
mellan samma platser (minst 3), annars med liknande längd (minst 5), annars från en enkel
tabell (under 3 km 25 km/h, under 10 km 35, under 30 km 50, under 80 km 65, längre 80 km/h).
Exempel: 9 km, framme 16:42, inga egna resor → 35 km/h, 15 minuter → **≈ 16:27 (±5 min)**.
Tiden märks precis som en uträknad tid: "≈" och felmarginal i appen, **ca** i underlaget, och
körtiden visas som uppskattad. Ett tryck på tiden visar varifrån farten kommer. Felmarginalen
blir större när dina egna resor på sträckan varierar mycket, och egna resor med en snittfart
under 15 km/h (t.ex. en importerad resa där bilen stod parkerad) räknas inte. Resor som
saknade starttid före 1.2.1 får uppskattningen vid nästa hämtning från bilen – utom resor som
finns i ett underlag som månadsskiftet redan har skapat, så att ett inlämnat underlag inte
ändras (en resa som tillkommit efter att månadens fil skapades finns inte i filen och får sin
tid som vanligt). Kalendern, klassningen och AI-förslagen räknar inte med en uppskattning ur sträckan
som känd avfärdstid.

Saknas mätarställning eller en säker ankomsttid visas *Start okänd (mellan 06:10 och 08:52)*
och knappen **Ange starttid**. En tid du anger märks *angiven av dig* och ändras aldrig av
DriveLog. Underlaget skriver vid okänd start bara sluttiden, "–08:52". En saknad starttid
stoppar inte månadens underlag, men den står i månadsnotisen och på exportsidan.

### Månadsskiftet

DriveLog kontrollerar föregående månad varje timme från klockan 06 den första. Månaden är klar
när det här inte längre gäller:

| Väntar på | Text |
| --- | --- |
| Bilens sista avläsning efter månadsslutet | *Väntar på bilens sista uppgifter* |
| Ny Volvo-inloggning | *Volvo behöver ny inloggning* |
| Obesvarade frågor (även uppskjutna) | *N resor väntar på ditt svar* (fördelning av en blandad resa, eller resor från tiden före självgående läget som telefonen inte frågar om: *öppna webben*) |
| Tjänsteresor som inte är godkända | *N tjänsteresor är inte godkända* |
| Vetotid för antagna resor | *Väntar till … på ditt veto* |
| Adresser som slås upp (högst 48 h) | *Adresser slås upp*, eller *Adressen kunde inte slås upp än – nytt försök görs varje dygn* om karttjänsten inte svarar |
| Fel i kontrollen, t.ex. överlappande resor, privat adress i underlaget eller en plats utan adress | Felets rubrik, t.ex. *Bostaden saknar adress – fyll i den under Platser* |

- **Klar:** underlaget skapas och notisen *Oktober klart* kommer, med *Öppna underlaget* och,
  när det finns antagna rader, *Se antagna*. Notisen säger också hur många resor som saknar
  starttid och hur många rader som är antagna.
- **Väntar:** notisen *Oktober väntar …* säger vad som väntar. Den kommer första gången och
  sedan var 3:e dygn, högst 3 gånger. Månadens undanlagda frågor ställs om en gång.
- **Inga tjänsteresor:** notisen *November: inga tjänsteresor*, ingen fil. Rättar du efteråt
  den sista tjänsteresan i en skickad månad till privat återkallas filen och länken slutar gälla.
- **Ändrat efteråt:** ändrar du en resa i en månad som redan har skickats skapas en ny fil
  inom ett par timmar, med notisen *Oktober uppdaterad: +1 resa, +34 km*. Den gamla filen och dess
  länk slutar gälla. Ändras inget i underlaget skapas aldrig någon ny fil.

**Skapa underlaget nu.** Vet du att bilen inte ska användas i tjänsten mer den här månaden kan
du skapa underlaget i förväg: tryck **Skapa underlaget nu** på den pågående månaden i
månadskortet på *Export*. Samma kontroller gäller som vid månadsskiftet – obesvarade frågor,
vetotiden för antagna resor och kopplingen till Volvo. Väntar något ser du vad, och ingen fil
skapas. Annars skapas filen och länken direkt, och i filens sidhuvud står *Skapad 16 november
2026 kl. 10:12, innan månaden var slut*. Perioden är fortfarande hela månaden (filnamnet slutar
på *november 2026*). Vid månadsskiftet kontrolleras månaden igen: har en redovisad resa
tillkommit eller ändrats skapas ett uppdaterat underlag med notisen *November uppdaterad*, och
finns det då en obesvarad fråga om en tjänsteresa får du notisen *November väntar …*. Annars
händer inget mer – ingen ny fil och ingen ny notis. Har något ändrats innan månaden är slut
visar kortet *Skapa nytt underlag nu*.

Månadskontrollen körs bara i självgående läge och för bilar som DriveLog läser av. Månader som
tog slut innan läget slogs på sköts som förut, för hand. På *Export* visar månadskortet läget
för innevarande och föregående månad, upplysningar som *1 resa saknar starttid*, *Kontrollera
nu* och *Se antagna*.

### Länken i notisen

*Öppna underlaget* hämtar filen direkt i telefonen, utan inloggning. Länken gäller i 7 dagar
och för högst 10 hämtningar, och exportsidan visar hur många gånger filen hämtats via länk.

Notisen går via Home Assistant. Den som har en Home Assistant-token eller kommer åt Home
Assistants historik kan därför hämta underlaget så länge länken gäller. Länken stängs när:

- filen ersätts av en nyare ("En nyare fil finns – öppna senaste notisen") eller raderas,
- du byter lösenord, inne i DriveLog eller genom en återställning från kommandoraden eller
  tilläggets inställningar,
- du trycker **Stäng alla nedladdningslänkar** på exportsidan.

## Import

1. Öppna **Import** och dra in en eller flera filer (XLSX eller CSV).
2. **Förhandsgranskningen** visar per fil: igenkänt format, nya resor, dubbletter och rader
   med fel (med förklaring per rad), samt period, körsträcka, bensin och el. Inga resor
   skapas förrän du bekräftar.
3. Klicka **Bekräfta import**.

- **Dubbletter:** samma fil kan laddas upp igen utan att något händer. Samma resa i en annan
  export känns igen på starttid och mätarställning.
- **Ångra:** varje import kan ångras under *Tidigare importer*.
- **Borttagna resor kommer tillbaka:** har du tagit bort en importerad resa (se *Ta bort en
  resa* nedan) och importerar samma fil – eller en annan fil med samma resa – igen, kommer den
  borttagna resan tillbaka. Förhandsgranskningen visar hur många (*Kommer tillbaka*), medan resor
  som finns kvar hoppas över som dubbletter. En resa som kommer tillbaka läggs in som den står i
  filen och bedöms på nytt; ändringar och beslut du gjorde innan du tog bort den kommer inte
  tillbaka.
- **Originalen bevaras:** kategori, adresser, mätarställningar, sträcka, förbrukning och
  anteckningar sparas exakt som i filen. Dina kommentarer blir resornas syfte och ändras
  aldrig automatiskt.
- Importerade resor som redan är märkta *Arbete* räknas som klara – de behöver inte godkännas
  en gång till.

## Platser – gör detta en gång

Under **Platser** finns alla adresser ur journalen. Överst visas förslag:

- **Platstyper** – hem, kontor, kunder och hotell, härledda ur dina resor och kommentarer.
  Godkänn eller avvisa med ett klick.
- **Slå ihop** – grannadresser på samma gata som troligen är samma parkering.

Ge platserna egna namn ("Örebrokontoret") och sätt gärna ut dem på kartan, så kan positioner
från bilen kopplas till rätt plats. Bara bekräftade platser påverkar förslagen. En plats
markerad *Privat destination* ger privat klassning, och resor dit hamnar aldrig i underlaget.

Fliken *AI-minne* visar vad systemet lärt sig per plats och rutt, dina korrigeringar (som kan
ångras) och egna regler, till exempel "resor hit är alltid Arbete med syftet …" eller *Fråga
mig alltid*. Ett mönster som blivit fel stänger du av med *Använd inte*.

## Körjournalen

**Körjournal** visar resorna med datum, tider, adresser, mätarställning, avstånd, kategori
och syfte. Snabbfiltren *Alla / Arbete / Privat / Att granska* ligger överst.

- **Byt kategori med ett klick** direkt i raden; syftet redigeras på plats.
- Sök i adresser och syften, sortera och filtrera på period.
- Markera flera rader för att sätta kategori eller syfte, godkänna, slå samman, exportera
  eller ta bort (se nedan).

### Ta bort en resa

Importerade och manuellt inlagda resor kan tas bort: **Ta bort resan** längst ned på resans
sida, eller markera flera i körjournalen och välj **Ta bort**. Resor från bilen kan inte tas
bort. Innan något händer ser du vad borttagningen gör:

- Resan försvinner ur körjournalen och underlaget, men raderas inte för gott: originalraden
  finns kvar under *Import → Tidigare importer*, och borttagningen syns i historiken.
- Kilometrarna räknas därefter som övrig (privat) körning när bara tjänsteresor redovisas, och
  blir en lucka med frågor när hela körjournalen redovisas.
- Öppna frågor om resan stängs, och det du har bestämt om resan används inte längre för
  förslag. Egna regler som du har skapat finns kvar (de tas bort under *Platser → AI-minne*).
- Har månadens underlag redan skapats uppdateras det, på samma sätt som när du ändrar en resa.
  En Excel-fil du har skapat själv ändras inte.
- En importerad resa kommer tillbaka om du importerar samma fil igen, en manuellt inlagd gör det
  inte.

En sammanslagen resa delar du först upp (under *Sammanslagning* på resan), och en resa som ingår
i en tjänsteresa löser du först upp tjänsteresan för – texten säger hur.

| Status | Betydelse |
| --- | --- |
| Importerad | Kategorin kommer från den importerade körjournalen |
| Godkänd | Du har bestämt eller godkänt |
| Godkänd automatiskt · <skäl> | Självgående läge: godkänd av ett av skälen ovan, till exempel *Din regel för …* |
| Antaget | Självgående läge: antaget kundbesök, se *Platsantagandet* |
| Automatiskt klassad (preliminär) | Systemet är säkert, men resan tas med i underlaget först när du godkänt (i självgående läge blir sådana resor förslag i stället) |
| Förslag | Väntar på ditt godkännande |
| Behöver granskas | Underlaget räcker inte – du väljer |

Faktiska värden visas som vanlig text, uppskattade kursivt med "≈", och saknade som "–".
En okänd starttid visas som "okänd" eller *Start okänd (mellan … och …)*. En uträknad eller ur
sträckan uppskattad starttid visas alltid med "≈" och felmarginal, se *Starttid* ovan.

## Resedetaljer

Överst: karta, sträcka, kategori och syfte. Under hopfällda rubriker finns varför systemet
föreslog som det gjorde, matchande Outlook-möten, liknande tidigare resor, ingående
originalresor och ändringshistorik.

- **Kartan** visar de punkter som faktiskt är kända. En linje mellan dem är inte den körda
  vägen, och det står utskrivet. För importerade resor slås adresserna upp i bakgrunden (se
  *Positioner ur adresser* ovan).
- **Uppehåll i hämtningen från Volvo:** kom resan efter ett uppehåll i hämtningen visas en gul
  ruta överst med när hämtningen låg nere och hur mycket mätaren ökade under tiden, t.ex.
  *Hämtningen låg nere mellan mån 16/11 08:10 och 12:40. Under tiden ökade mätaren med 95 km …*.
  Det kan vara flera körningar. Tryck **Importera körjournal** och importera Volvos körjournal
  för dagen – då ersätts resan av de riktiga resorna, utan dubbletter – eller **Dela upp** för att
  fördela kilometrarna i tjänst och privat. Har du redan svarat om resan och importen ger flera
  resor står ditt tidigare beslut i ett ärende i Granskning, så att du kan lägga in det på rätt
  resa. Samma besked kommer i frågan i telefonen. En sådan resa godkänns aldrig av sig själv, och
  den blir inte privat utan att du har sagt det.
- **Delvis privat?** Dela resan i delsträckor (t.ex. 30 km tjänst + 12 km privat). Summan
  måste stämma med den uppmätta sträckan, och bara tjänstedelen hamnar i underlaget.
- **Ångra ändring** återställer både resan och det systemet lärt sig av ändringen.

## Kalendern

Kalendern är en ledtråd: ett möte nära resans ankomst, på en plats som stämmer med målet,
ger ett förslag på ärende. Den kopplas under *Integrationer → Microsoft Outlook* på ett av två
sätt – eller båda, utan att mötena dubbleras:

- **Kalenderlänk (ICS)** – enklast, ingen inloggning och ingen appregistrering. Skapa en länk i
  Outlook på webben (*Inställningar → Kalender → Delade kalendrar*, publicera och välj *Kan visa
  rubriker och platser*), klistra in den och tryck *Spara*. Länken är som ett lösenord: den
  sparas krypterad och visas aldrig igen. DriveLog hämtar kalendern var 30:e minut, och *Synka
  nu* hämtar direkt.
- **Appregistrering hos Microsoft** – för den som kan registrera en app i organisationen.

Steg för steg och vad som läses finns i [Microsoft Outlook](microsoft-graph.md).

**Läs hela bokningen** (*Inställningar → Kalender*, påslaget efter uppdateringen till 1.3.0):
DriveLog läser bokningens beskrivning och deltagare, inte bara rubrik, tid och plats. Det behövs
för att ärenden som *Kundbesök hos … med Lennart* ska kunna skrivas. Kalenderlänken innehåller
bara det som delningen släpper igenom – dela kalendern med **Kan visa all information** och
kopiera den nya länken. Under *Integrationer* står hur många bokningar som har beskrivning
respektive deltagare; Outlook tar inte alltid med deltagarna i en delad kalender.

**Min arbetsgivares e-postdomän** (samma sektion) avgör vilka deltagare som är kollegor (samma
domän) och vilka som är externa (kunder, kandidater, leverantörer). Så länge fältet är tomt och
du aldrig har sparat det fyller DriveLog i det ur kalendern vid en synk – kontrollera det och
ändra det om det är fel. Ett värde du har sparat skrivs aldrig över. Använder du en allmän
e-postadress (t.ex. gmail) kan domänen ha tagits från någon annans inbjudan; fyll då gärna i
fältet själv.

En bokning kopplas till resan även utan plats i bokningen, om en ort eller ett företag i rubriken,
beskrivningen eller bland deltagarnas domäner stämmer med resmålet. Att bara tiden stämmer räcker
aldrig för mer än ett förslag. Onlinemöten och privata bokningar förklarar aldrig en resa.

## Ärendet och AI

**Ärendets text** väljs i den här ordningen när resan är en tjänsteresa:

1. Din egen regel med fast text och läget *Använd alltid texten*.
2. En kalenderbokning som stämmer med resan i tid och plats – för en resa till en annan ort även
   bokningarna under hela vistelsen och de närmaste dagarna. AI formulerar ärendet ur bokningen
   (utan AI används bokningens rubrik).
3. Din egen regel med fast text (läget *när kalendern inte säger något annat*, standard).
4. Hemresa, hotell eller ditt vanligaste bekräftade ärende för platsen.
5. AI:s eget förslag ur hela underlaget.

När resan godkänns automatiskt (av din regel, *Hem ↔ kontor*, hemresa eller *Som de 3 senaste
gångerna*) och en bokning stämmer sätts kalenderns ärende utan fråga. Resan märks **Ur
kalendern** på resans sida och i månadens genomgång, och **Ärendet stämmer inte** tar bort det:
resan får då ett ärende att bekräfta i Granskning och bokningen kopplas inte till resan igen.
Läget för en egen regel med fast text väljer du när du skapar regeln under *Platser → AI-minne*.

Ett ärende från AI är alltid ett förslag – märkt **Förslag att bekräfta** – som du godkänner med
ett tryck; AI godkänner aldrig något själv. Saknar förslaget direkt stöd i underlaget står det
under förslaget. Namn som inte finns i underlaget kastas. I telefonen visas AI:s förslag som knapp
bara när du inte har något tidigare bekräftat ärende för rutten eller platsen, och aldrig som
*Alltid: …*.

**Hur mycket underlag får AI?** (*Inställningar → Förslag och AI*, gäller bara med en vald
AI-leverantör):

- **Fullständigt underlag** (rekommenderas, gäller direkt efter uppdateringen): namn och
  gatuadress för start och mål – även hemmet och privata platser –, kartans namn och typ för
  okända ställen, dagens alla resor, resan före och efter, historiken för samma mål och rutt,
  dina regler och platsanteckningar, arbetsgivarens namn, det du skriver under **Om mitt
  arbete**, och kalenderbokningarna med rubrik, plats, beskrivning och deltagarnas namn (kollega
  eller extern, och den externa e-postdomänen). Privata bokningar skickas bara som tid, utan
  text.
- **Minimalt**: bara platsens namn och ort, veckodag, klockslag, sträcka, regelmotorns signaler
  och dina tidigare ärenden. Kalenderrubrik, brödtext och gatuadresser styrs av tre egna val.

Koordinater, chassinummer, nycklar, lösenord, e-postadresser och kalenderlänken skickas aldrig,
och möteslänkar, inringningsnummer, koder och lösenord i bokningar och anteckningar tas bort före
sändningen. Allt i underlaget behandlas som uppgifter, aldrig som instruktioner.

**Gå igenom med AI.** Resor kan bedömas på nytt med AI (eller bara med reglerna, om ingen
leverantör är vald):

- en resa: **Bedöm med AI** (eller *Bedöm igen*) i klassningskortet på resans sida,
- flera: markera resor i körjournalen och tryck **Gå igenom med AI**,
- alla som väntar: **Gå igenom väntande med AI** i Granskning.

Innan något skickas står hur många resor som bedöms och vilken leverantör som används. Högst 25
resor tas per gång (de äldsta först); kör igen för resten. Resorna bedöms en i taget med förlopp,
och *Avbryt* stoppar efter pågående resa. Resor som redan är godkända eller importerade rörs
aldrig. Till sist visas hur många som fick ett nytt förslag, godkändes av dina regler eller var
oförändrade, med länk till varje resa.

## Sammanslagning av resor

Bilen delar upp en resa varje gång du stannar. Under *Inställningar → Sammanslagning av resor*
bestämmer du hur korta stopp som räknas som samma resa, med egna gränser för **tjänsteresor**
och **privata resor**:

| Stillestånd | Tjänsteresor (standard) | Privata resor (standard) |
| --- | --- | --- |
| Högst 15 minuter | Slås ihop automatiskt (kan ångras) | Slås ihop automatiskt |
| Över 15 och högst 60 minuter | Du får en fråga | Ingen fråga, två resor |
| Längre | Inget automatiskt; markera resorna och välj *Slå samman* | Samma |

- Två tjänsteresor i rad följer tjänstegränserna, två privata följer privatgränserna.
- En tjänsteresa och en privat, eller en resa som inte är avgjord än, slås aldrig ihop
  automatiskt och ger ingen fråga. Antagna kundbesök som du inte har bekräftat räknas inte som
  avgjorda.
- Resorna slås ihop när båda är avgjorda – av dig, av en regel eller av importen. Det gäller även
  i självgående läge: en ny resa från bilen slås ihop i samma stund som den godkänns, annars när
  du svarar. Ändrar du senare kategorin på en sammanslagen resa förblir den en resa (dela upp
  den om du vill).
- Stopp hemma eller vid en privat plats slås aldrig ihop.
- Frågor hamnar i Granskning, och i telefonen om notiser om sammanslagning är på (bara för resor
  från de senaste två dygnen).

**Efter uppdateringen till 1.3.0** gäller det tidigare sättet tills du sparar sektionen (det
står överst i sektionen): i självgående läge slås bara stopp på högst 15 minuter vid bekräftade
tank- och laddstationer ihop, och inga frågor ställs; i läget *Jag godkänner allt själv* gäller
samma gränser för alla resor. När du sparar gäller de nya gränserna för resor från och med då;
resor som redan finns slås inte ihop i efterhand.

Den sammanslagna resan visar mellanstoppen ("via …"), summerar sträckan och behåller första
starttid och sista sluttid. Originalresorna finns alltid kvar, och gränserna ändras under
*Inställningar → Sammanslagning av resor*.

Två resor slås inte ihop automatiskt om bilen har körts däremellan (kilometer saknas i
mätarställningen), och en privat delsträcka blir aldrig tjänst genom en sammanslagning.
Saknar en importerad resa klockslag (bara datum) går stilleståndet inte att bedöma; då ställs
ingen fråga, men du kan fortfarande slå samman manuellt.

Ett stopp hemma eller vid en bekräftad privat plats slås aldrig ihop med de nya gränserna. Med
det tidigare sättet gäller det när någon av resorna kommer från bilen, i båda lägena. Ett mellanstopp hemma eller vid en privat plats på en resa från bilen skrivs som
"Privat stopp" i underlaget, aldrig med adress; importerade och manuellt inlagda resor skrivs
som förut.

## Granskning

**Granskning** samlar det som behöver ditt beslut. Snabbflödet går igenom ärendena ett i
taget, även med tangentbordet (A = arbete, P = privat, G = godkänn).

| Ärende | Åtgärd |
| --- | --- |
| Arbete eller privat? | Välj |
| Vad var syftet? | Välj ett tidigare syfte eller skriv ett nytt |
| Slå samman? | Ja / Nej |
| Övrigt (saknad mätarställning, kalenderkonflikt, möjlig dubblett) | Kontrollera |

Ärenden kan skjutas upp eller avfärdas, och varje åtgärd kan ångras. **Gå igenom väntande med
AI** överst bedömer alla väntande resor på nytt (se *Ärendet och AI*).

Nästa ärende visas så fort du har svarat – du behöver inte vänta på servern och kan svara i
rask takt. Ett svar påverkar ofta förslagen för andra resor (till exempel andra resor till
samma plats). De räknas om i bakgrunden en stund efter ditt senaste svar, och listan uppdateras
av sig själv när det är klart. Medan svar skickas står "Sparar … svar" under ärendet, och
webbläsaren varnar om du stänger fliken innan de har kommit fram. Går ett svar inte att spara
(eller svarar servern inte inom 20 sekunder) visas felet på ärendet, som läggs tillbaka först i
kön. Har förslaget (kategori eller syfte) hunnit ändras när du trycker *Godkänn förslag*
godkänns ingenting; du ser i stället det nya förslaget.

Bilen och Volvos körjournal anger mätarställningen i hela kilometer. Två resor i rad som skiljer
sig 1 km (glapp eller överlapp) räknas därför som avrundning: det blir varken ärende
("Motstridiga mätarställningar", "Överlappande resor") eller lucka, oavsett toleransen under
*Inställningar → Sammanslagning av resor*. 2 km eller mer är mer än avrundning och följer
toleransen som förut. Ett ärende som inte längre gäller stängs av sig självt, och en lucka som
du redan har hanterat (till exempel intygat som privat körning) står kvar som den är.

## Översikten

Månadens tjänstemil, antal tjänsteresor, beräknad milersättning och vad som väntar på
granskning – samt diagram över tjänstekörning över tid och vanligaste destinationerna.
Milersättningen räknas bara på godkända resor. Mer statistik (resvägar, månadsjämförelse,
förbrukning) och *Veckan i korthet* (veckans tjänstemil, vad som väntar på granskning och
vanligaste destinationer) finns under hopfällda rubriker.

## Export

Se [Excel-export](excel-export.md). I självgående läge skapas underlaget av sig självt (se
*Månadsskiftet* ovan), och månadskortet överst på *Export* visar läget. För hand: **Export →
välj månad → Skapa Excel.** Filen
innehåller ett blad med månadens tjänsteresor och en summering med mil och ersättning.
Vill du bara redovisa vissa resor markerar du dem i körjournalen och väljer *Exportera valda*.

Vill du ha företagets logga i underlaget laddar du upp den under *Inställningar → Milersättning
och export → Företagslogga i underlaget* (PNG eller JPEG, högst 1 MB). Den läggs diskret uppe till
höger i sidhuvudet, i underlag du skapar och i månadens automatiska underlag. *Visa loggan i
underlaget* stänger av den utan att ta bort den; *Ta bort* tar bort filen från servern. Underlag
som redan är skapade ändras inte.

## Notiser i telefonen

Med Home Assistant ansluten kommer frågorna som interaktiva notiser, och svaren sparas direkt
i journalen. Frågan utgår från vad DriveLog har kommit fram till: har resan bedömts som
tjänst med ett ärende frågar notisen *Tjänsteresa – stämmer det?* med *Godkänn* (tjänst och
ärendet i ett tryck), *Ändra ärende*, *Privat* och *Senare*; utan ärende *Tjänsteresa –
vilket ärende?* med förslag; bedömd som privat *Privat resa – stämmer det?*. Bara en resa som
inte är bedömd, som kan vara flera körningar eller som har delsträckor med olika kategori får
den gamla frågan: i självgående läge *Tjänst eller privat?* med knappar som *Tjänst: Kundbesök
…*, *Tjänst – skriv ärende*, *Privat* och *Alltid privat här*, i manuellt läge *Var detta
arbete eller privat?* med *Arbete*, *Privat*, *Granska* och *Senare*. I självgående läge kommer en fråga åt
gången. DriveLog påminner efter 24 timmar och lägger undan frågan efter 48, så att nästa
får plats. Larm när något har slutat fungera (Volvo, kalendern) och månadsnotisen kommer också
hit. Obesvarade frågor ligger kvar under *Granskning*. Tysta timmar, max antal per
dygn och påminnelser ställs in under *Inställningar → Notiser*. Se
[Home Assistant](home-assistant.md).

För platser utan namn visar notisen hela adressen med husnummer, och bostaden visas med sin
adress. Slå på *Dölj platser på låsskärmen* under *Inställningar → Notiser* om andra kan se din
skärm – då visar notisen bara dag och sträcka.

När DriveLog har uppdaterats till en ny version kommer notisen *DriveLog är uppdaterad till …*
med de viktigaste nyheterna – en gång per version, aldrig vid en vanlig omstart. Den stängs av med
*Notis när DriveLog har uppdaterats* under *Inställningar → Notiser*.

## Vilken version kör jag?

Längst ned i sidomenyn står till exempel *DriveLog 1.3.0* (på mobilen i användarmenyn uppe till
höger). Tryck på den för *Inställningar → Om DriveLog*, där det står vad som är nytt i versionen.
Hela ändringsloggen finns på GitHub (länken står där) och, om du kör DriveLog som tillägg, på
tilläggets sida i Home Assistant.

## Bränsle och energi

Under *Mer → Bränsle & energi* visas bensin och el. Förbrukning räknas som uppmätt när den
kommer per resa ur den importerade körjournalen. För resor från bilen räknar DriveLog fram
förbrukningen ur bilens egna avläsningar: färddatorns snitt gånger trippmätaren, eller
skillnaden i tanken och batteriet mellan senaste rapporten före resan och rapporten vid
ankomsten. DriveLog väljer den källa som är säkrast för just den resan. En tankning eller
laddning under resan gör värdet okänt. Värdena är uppskattade och står med ≈ på resan; räcker
inte bilens uppgifter står det "–".

**Hur säkert är värdet?** Bilen rapporterar tanken i hela liter och batteriet i hela procent.
Då kan skillnaden vara fel med upp till en liter respektive en procentenhet: "12 l ± 1 l" betyder
att resan drog mellan 11 och 13 liter. DriveLog avrundar inte själv. I resans detaljer står hur
värdet räknades fram – metod, nivån före och efter och felgränsen. Elen är nettot ur batteriet;
bilen kan ha laddat batteriet med motorn eller bromsarna under färd.

**Batteriets kapacitet.** För att räkna el behöver DriveLog batteriets *användbara* kapacitet i
kWh. Fyll i den under *Inställningar → Fordon och förare*. Är fältet tomt används ett
standardvärde bara när det finns ett belagt värde för modellen och årsmodellen; annars visas
ingen el. När du fyller i eller ändrar kapaciteten räknas elen om på resorna från de senaste 14
dygnen (dina egna värden rörs inte, och inte heller resor som finns i ett underlag som redan är
skapat).

Här kan du också föra in tankningar och laddningar.

### Felsökningsfil

Strular något med bilens data kan du under *Integrationer → Volvo* trycka **Ladda ner
felsökningsfil**. Filen innehåller bilens avläsningar och hämtningar de senaste 14 dygnen och de
resor som byggdes av dem, men inte chassinummer, registreringsnummer, positioner, adresser eller
platsnamn. Skicka den till den som hjälper dig felsöka. Från 1.5.0 visar filen också månadernas
underlag (skapat i förväg och när) och, för varje resa, en kort kod för varför starttiden, bensinen
eller elen saknas.

Kraschar ett steg i bearbetningen efter en hämtning står det under *Senaste fel* i Volvo-panelen
("Avläsningen hämtades, men …"). Resan finns ändå i journalen. Raden försvinner när
bearbetningen har lyckats igen, oftast efter nästa körning. Från 1.4.2 står de senaste interna
felen också under `internal_errors` i felsökningsfilen, med ett tvättat felmeddelande: kända
adresser och platser tas bort, men ett okänt platsnamn kan bli kvar, så läs filen innan du
skickar den vidare.

## Demoläge

Utan nycklar kan varje integration provas i demoläge. Demodata hamnar på en separat
**demobil** som väljs i fordonsväljaren; en banderoll visar att det är demo, och ingenting
blandas med din riktiga journal. *Stäng av demoläge* tar bort demodatan.

## Inställningar i korthet

| Sektion | Innehåll |
| --- | --- |
| Fordon och förare | Registreringsnummer, bil, ditt namn och arbetsgivare (visas i exporten); tidszon under *Avancerat* |
| Milersättning och export | Kronor per mil, visa belopp, mellanstopp, företagslogga i underlaget, exportens utseende, vad som redovisas |
| Självgående läge | *Självgående* eller *Jag godkänner allt själv*, kontorstid och vetotid för antagna kundbesök, skälen för automatiska godkännanden och länk till dina egna regler |
| Sammanslagning av resor | Gränserna 15 och 60 minuter (i självgående läge, se ovan) |
| Lunchregeln | Av/på och gränserna för lunchmönstret (fönster, körtid, sträcka, stopp) |
| Förslag och AI | Trösklar, strikt läge, underlagsnivå (Fullständigt/Minimalt), Om mitt arbete, AI-förslag på syfte |
| Notiser | Typer, tysta timmar, max per dygn, påminnelser, veckosammanställning, notis när DriveLog har uppdaterats, dölj platser på låsskärmen |
| Kalender | Läs hela bokningen, arbetsgivarens e-postdomän, tidsfönster för matchning |
| Tjänsteställe | Hem ↔ kontor som tjänsteresor, ordinarie tjänsteställe, bostaden som tjänsteställe, förmånsbil och krav på godkännande vid skattemässig osäkerhet |
| Förkortningar | Egna förkortningar i kommentarer, t.ex. ortsförkortningar |
| Sekretess | Karta, geokodning (OpenStreetMap, på som standard), gallring, om originalfiler sparas vid import, radering av historik |
| Säkerhet | Lösenord, API-tokens |
| Säkerhetskopia | Skapa och hämta kopior |
| Om DriveLog | Vilken version som körs och vad som är nytt i den |

### Fullständig körjournal

Behöver du i stället en fullständig körjournal där även privata resor redovisas och allt
stäms av mot mätarställningen – till exempel för en förmånsbil – väljer du *Fullständig
körjournal* under *Milersättning och export → Vad redovisas?*. Då visas luckor i
mätarställningen som något att reda ut. Välj samtidigt *Detaljerad* under *Exportens utseende*
om du vill ha den utförliga arbetsboken med flera blad – de två valen är oberoende av varandra.
