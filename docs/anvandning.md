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
- resan går mellan hemmet och det ordinarie tjänstestället,
- målet är hemmet (utom hemresa), en privat plats, tjänstestället eller ett hotell (utom med
  egen regel).

Inget godkänns som bara vilar på en kalenderrubrik, en schablontext eller en plats du inte
har bekräftat. En automatiskt godkänd resa räknas aldrig som "dina tre senaste gånger".

Resor som godkänts av sig själva märks **Godkänd automatiskt · <skäl>** och antagna resor
**Antaget**, i listan och på resans sida, där knappen *Ändra* finns. Rättar du en resa gäller
ditt beslut: en regel som slog fel frågar dig i stället nästa gång ("Regeln för X frågar dig
nu i stället"), och en rättad *Som de 3 senaste gångerna* gör att den inte gäller rutten längre.

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
eller som kommer från bilen skrivs aldrig över. Hamnar en uppslagen plats vid bostaden eller ett
kontor som saknar adress slås de ihop, och bostaden eller kontoret får adressen; har båda olika
adresser visas ett förslag om att slå ihop dem under Platser.

### Starttid

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
saknade starttid före 1.2.1 får uppskattningen vid nästa hämtning från bilen – utom resor i
en månad vars underlag månadsskiftet redan har skapat, så att ett inlämnat underlag inte
ändras. Kalendern, klassningen och AI-förslagen räknar inte med en uppskattning ur sträckan
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

Ärenden kan skjutas upp eller avfärdas, och varje åtgärd kan ångras.

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

## Notiser i telefonen

Med Home Assistant ansluten kommer frågorna som interaktiva notiser, och svaren sparas direkt
i journalen. I självgående läge kommer en fråga åt gången, med kategori och ärende i samma
tryck och knappar som *Tjänst: Kundbesök …*, *Tjänst – skriv ärende*, *Privat* och *Alltid
privat här*. DriveLog påminner efter 24 timmar och lägger undan frågan efter 48, så att nästa
får plats. Larm när något har slutat fungera (Volvo, kalendern) och månadsnotisen kommer också
hit. Obesvarade frågor ligger kvar under *Granskning*. Tysta timmar, max antal per
dygn och påminnelser ställs in under *Inställningar → Notiser*. Se
[Home Assistant](home-assistant.md).

## Bränsle och energi

Under *Mer → Bränsle & energi* visas bensin och el. Förbrukning räknas bara som uppmätt när
den kommer per resa ur den importerade körjournalen; bilens snittvärden och tanknivå från
Volvo räknas inte som förbrukning per resa. Här kan du också föra in tankningar och laddningar.

## Demoläge

Utan nycklar kan varje integration provas i demoläge. Demodata hamnar på en separat
**demobil** som väljs i fordonsväljaren; en banderoll visar att det är demo, och ingenting
blandas med din riktiga journal. *Stäng av demoläge* tar bort demodatan.

## Inställningar i korthet

| Sektion | Innehåll |
| --- | --- |
| Fordon och förare | Registreringsnummer, bil, ditt namn och arbetsgivare (visas i exporten); tidszon under *Avancerat* |
| Milersättning och export | Kronor per mil, visa belopp, exportens utseende, vad som redovisas |
| Självgående läge | *Självgående* eller *Jag godkänner allt själv*, kontorstid och vetotid för antagna kundbesök, skälen för automatiska godkännanden och länk till dina egna regler |
| Sammanslagning av resor | Gränserna 15 och 60 minuter (i självgående läge, se ovan) |
| Förslag och AI | Trösklar, strikt läge, vad som får skickas till AI-leverantören |
| Notiser | Typer, tysta timmar, max per dygn, påminnelser, veckosammanställning |
| Kalender | Tidsfönster för matchning |
| Tjänsteställe | Avancerat: ordinarie tjänsteställe, bostaden som tjänsteställe, förmånsbil och krav på godkännande vid skattemässig osäkerhet |
| Förkortningar | Egna förkortningar i kommentarer, t.ex. ortsförkortningar |
| Sekretess | Karta, geokodning (OpenStreetMap, på som standard), gallring, om originalfiler sparas vid import, radering av historik |
| Säkerhet | Lösenord, API-tokens |
| Säkerhetskopia | Skapa och hämta kopior |

### Fullständig körjournal

Behöver du i stället en fullständig körjournal där även privata resor redovisas och allt
stäms av mot mätarställningen – till exempel för en förmånsbil – väljer du *Fullständig
körjournal* under *Milersättning och export → Vad redovisas?*. Då visas luckor i
mätarställningen som något att reda ut. Välj samtidigt *Detaljerad* under *Exportens utseende*
om du vill ha den utförliga arbetsboken med flera blad – de två valen är oberoende av varandra.
