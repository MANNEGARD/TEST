# Ändringar

> Från 1.0.2 visar Home Assistant **Uppdatera** först när den nya versionen är färdigbyggd och
> går att hämta – det går bra att trycka direkt.

## 1.3.1

Säkerhetsrättningar efter en genomgång av hela appen.

- **Bara säkerhetsrättningar – appen fungerar som förut.** Ingen text, knapp, fråga, inställning
  eller standardvärde har ändrats, och ingenting behöver ställas om efter uppdateringen. Ingen
  ändring i databasen.
- **Säkrare inloggning.** Spärren mot gissade lösenord håller nu även när många försök görs
  samtidigt. En webbläsare där du redan har loggat in har en egen spärr, så den som gissar mot ditt
  konto kan inte längre låsa ute dig där (den slutar gälla när du byter lösenord). Lösenordsbytet
  har också fått en spärr: 8 felaktiga nuvarande lösenord inom 5 minuter. En adress som skrivs in
  vid en misslyckad inloggning sparas bara i ändringsloggen när kontot finns.
- **Integrationen i Home Assistant skickar bara DriveLogs egna notiser.** Allt annat som skulle
  hamna i utkorgen avvisas i stället för att skickas till telefonen, och rubrik och text visas
  aldrig som HTML. Skriver du in en publik adress med `http://` när integrationen läggs till
  används `https://`; lokala adresser och tilläggets värdnamn fungerar med `http://` som förut.
  Integrationen har ändrats: **uppdatera den i HACS och starta om Home Assistant** för att de här
  rättningarna ska gälla. Den integration du har installerad fungerar som förut mot den nya
  servern tills dess.
- **Kalendern tål elaka bokningar.** Upprepningsregler som aldrig kan inträffa och jättelika
  beskrivningar belastar inte längre servern, och bokningar försvinner inte när en synk avbryts i
  förtid (kalenderlänken och Outlook). Rubrik och plats från Outlook tvättas från osynliga tecken,
  som kalenderlänkens redan gjorde. Efter anslutning till Microsoft visas leverantörens felkoder
  som förut, men aldrig annan text ur adressfältet.
- **Importen och underlaget.** Osynliga styr- och formattecken tas bort ur ärenden och adresser
  vid import (originalanteckningen sparas oförändrad). Formelskyddet i underlaget gäller nu även
  text som börjar med en radbrytning, och Excel-filer med orimligt mycket text avvisas innan de
  packas upp. Original- och exportfiler raderas bara inne i DriveLogs egna kataloger.
- **Mindre till AI.** Bilens chassinummer (VIN) och registreringsnummer tas bort ur underlaget på
  båda nivåerna, för alla dina bilar och i alla skrivsätt. Koder och lösenord tas bort även i böjd
  form (*koden*, *lösenordet*, wifi), och ett AI-ärende med ord som inte finns i underlaget kastas
  också när det är skrivet med andra alfabet. Nivån du har valt är oförändrad.
- **Webbläsarens skydd.** Webbappen skickar nu en Content-Security-Policy, ett extra skydd mot
  inskjuten kod. Efter inloggning skickas du bara vidare till sidor i DriveLog. Kartan och egna
  kartservrar fungerar som förut.
- **Servern, anslutningarna och loggarna.** Anrop över 2 MB som inte är uppladdningar avvisas
  innan de läses. Kalenderlänken och adressuppslagen kontrollerar motpartens adress redan när
  anslutningen öppnas, och fler förklädda interna adresser spärras. Svar från Volvo, Microsoft
  och Home Assistant kontrolleras hårdare. En plats med säker position slås inte längre ihop via
  adressen med ett stopp mer än 2 km bort. Loggarna döljer nu även felspår, fler delade
  kalenderadresser och inloggningskoder i adresser, och databasfel visar inte de sparade värdena.
  Nedladdningslänkens tak på 10 hämtningar håller även vid samtidiga anrop. I produktion startar
  API:t inte om `DRIVELOG_CORS_ORIGINS` innehåller `*` eller en adress utan https (ingen av
  DriveLogs installationsvägar sätter den).
- **Kända begränsningar.** Det som inte har rättats, eftersom rättningen skulle ändra hur appen
  fungerar, står under *Kända begränsningar* i `docs/sakerhet.md` i repot.

## 1.3.0

- **Egna gränser för sammanslagning av tjänsteresor och privata resor.** Under *Inställningar →
  Sammanslagning av resor* finns nu två uppsättningar gränser: *Tjänsteresor* och *Privata
  resor*. Två tjänsteresor i rad följer tjänstegränserna, två privata följer privatgränserna; en
  tjänsteresa och en privat, eller en resa som inte är avgjord än, slås aldrig ihop och ger ingen
  fråga. Stopp hemma slås aldrig ihop. Det du ställer in gäller också i självgående läge. **Din
  installation behåller det tidigare sättet** (i självgående läge bara korta tankstopp, inga
  frågor) tills du sparar sektionen – det står överst i sektionen. Resor som redan finns slås
  inte ihop i efterhand.
- **Varning när hämtningen från Volvo har legat nere.** Kommer en resa efter ett uppehåll i
  hämtningen står det på resan, i Granskning och i frågan i telefonen när hämtningen var borta
  och hur många km mätaren ökade under tiden, med rådet att importera Volvos körjournal för
  dagen (då ersätts resan av de riktiga resorna, utan dubbletter) eller dela upp resan. En sådan
  resa godkänns aldrig av sig själv och blir inte privat utan att du har sagt det. Följd vid
  uppdateringen: oavgjorda resor som föreslås privata och har märkningen *kan vara flera
  körningar* får nu ett ärende i Granskning (ingen notis). Larmet om att hämtningen inte fungerar
  påminner om Volvos körjournal.
- **Ta bort importerade resor.** En importerad resa tas bort med *Ta bort resan* på resan, och
  flera på en gång med *Ta bort* i körjournalen. Resan raderas inte för gott – originalraden
  finns kvar under Import och borttagningen syns i historiken. Importerar du samma fil igen
  kommer de borttagna resorna tillbaka (förhandsgranskningen visar hur många), som de står i
  filen; resor som finns kvar hoppas över som förut.
- **Månadens underlag i förväg.** *Skapa underlaget nu* på den pågående månaden under *Export*
  skapar filen och länken direkt, med samma kontroller som vid månadsskiftet. I filen står när
  den skapades. Vid månadsskiftet kontrolleras månaden igen och ett uppdaterat underlag kommer
  bara om en redovisad resa har tillkommit eller ändrats.
- **Kartan för importerade resor.** Platser som har adress men ingen position (till exempel ur
  en importerad körjournal) får en ungefärlig position ur adressen, i bakgrunden och lite i
  taget. Kartan visar *Positionen slås upp* medan det pågår, och *Adressen hittades inte på
  kartan* om den inte gick att hitta. Hamnar en sådan plats vid bostaden eller kontoret slås de
  ihop, så att bostaden får sin adress – men aldrig en kund, en plats med företag eller egen regel,
  eller när flera adresser ligger där; då blir det ett förslag under *Platser*. Kommer bilen sedan
  till platsen ersätts den ungefärliga positionen av bilens.
- **Träffsäkrare adresser.** Adressuppslaget skickar nu platsens exakta position, så att rätt hus
  hittas, och bostadens adress slås upp av sig själv när den saknas.
- **Saknad adress syns i förhandsvisningen.** På exportsidan står *Adress saknas – lägg till under
  Platser* direkt i cellen för start eller mål, och tiden bryts aldrig på två rader.
- **AI får hela underlaget och skriver färdiga ärenden.** Med en AI-leverantör får AI nu
  *fullständigt underlag*: platsernas namn och adresser (även hemmet och privata platser), dagens
  resor, historiken, dina regler, arbetsgivaren, det du skriver under *Om mitt arbete* och
  kalenderbokningarna med beskrivning och deltagare. **Det gäller direkt efter uppdateringen**;
  välj *Minimalt* under *Inställningar → Förslag och AI* om du vill skicka mindre. AI lämnar
  alltid ett färdigt ärende i din stil, märkt *Förslag att bekräfta* – du godkänner med ett tryck,
  AI godkänner aldrig något själv och namn som inte finns i underlaget kastas. Koordinater,
  chassinummer, nycklar, deltagarnas e-postadresser och kalenderlänken skickas aldrig.
- **Kalendern först för ärendet.** Stämmer en kalenderbokning med resan formuleras ärendet ur
  bokningen – för en resa till en annan ort ur hela vistelsens bokningar ("Rest till Jönköping för
  … med Lennart"). Godkänns resan automatiskt sätts kalenderns ärende utan fråga, märkt *Ur
  kalendern* på resan och i månadens genomgång, och *Ärendet stämmer inte* tar bort det. En egen
  regel med fast text gäller "när kalendern inte säger något annat" eller "alltid" – du väljer när
  du skapar regeln.
- **Läs hela bokningen.** DriveLog läser bokningens beskrivning och deltagare (kollegor känns igen
  på arbetsgivarens e-postdomän, som fylls i ur kalendern). **Påslaget efter uppdateringen**;
  stängs av under *Inställningar → Kalender*. Möteslänkar, inringningsnummer och lösenord tas bort
  innan något skickas till AI. Under *Integrationer* står hur många bokningar som har beskrivning
  och deltagare.
- **Gå igenom med AI.** *Bedöm med AI* på resan, *Gå igenom med AI* för valda resor i
  körjournalen och *Gå igenom väntande med AI* i Granskning – med antal och leverantör före start,
  högst 25 resor per gång, förlopp och resultat. Godkända och importerade resor rörs aldrig. Utan
  AI-leverantör körs bara reglerna.
- **Resor mellan hemmet och våra kontor godkänns automatiskt.** Resor mellan hemmet och vilket som
  helst av arbetsgivarens kontor godkänns som tjänsteresor, även kvällar och helger. **Påslaget i
  din installation efter uppdateringen** (ditt val); stängs av under *Inställningar →
  Tjänsteställe*. Korta kvällsturer från hotellet under en tjänsteresa ger alltid en fråga.
- **Lunchregeln.** En kort tur från ett av era kontor mitt på dagen (11–13 en arbetsdag), ett stopp
  på 20–45 minuter och tillbaka till samma kontor blir privat utan fråga – du får ingen fråga om
  utresan medan du äter. Hela mönstret krävs, aldrig klockslaget ensamt, och den gäller inte hos
  kunder, med en egen regel eller när en kalenderbokning stämmer. Lunchregeln är på från start och
  gäller även resor som väntar på svar när uppdateringen installeras. Gränserna ändras och regeln
  stängs av under *Inställningar → Lunchregeln*; den kräver att minst ett kontor finns som
  bekräftad plats. En kort tur hem och tillbaka till samma kontor görs inte privat av sig själv,
  men godkänns inte heller automatiskt som tjänst när resor mellan hemmet och kontoret räknas som
  tjänst – då får du en fråga.
- **Bensin och el per resa.** Resor från bilen visade tidigare "–" för bensin och el. Nu räknas
  förbrukningen fram ur bilens egna avläsningar – i första hand den automatiska trippmätaren och
  snittförbrukningen för samma period, annars skillnaden i tanken; en tankning under resan gör
  värdet okänt. Batteriets nivå används inte än. Värdena är uppskattade och står med ≈ på resan.
  Räcker inte bilens uppgifter, eller är de för grova (avrundade snitt på en lång trippmätare),
  står det fortfarande "–"; inget hittas på.
- **Felsökningsfil för bilen.** Under *Integrationer → Volvo* laddar *Ladda ner felsökningsfil* ner
  bilens avläsningar och hämtningar de senaste 14 dygnen och de resor som byggdes av dem – utan
  chassinummer, registreringsnummer, positioner och adresser – att skicka till den som hjälper
  dig felsöka.
- **Notis när DriveLog har uppdaterats.** När en ny version har startat får du en notis i
  telefonen, "DriveLog är uppdaterad till …", med de viktigaste nyheterna. En notis per version,
  ingen vid vanlig omstart. Stängs av under *Inställningar → Notiser*; kräver att telefonen är
  ansluten via Home Assistant-integrationen.
- **Versionen syns i appen.** Längst ned i sidomenyn (och i användarmenyn på mobilen) står
  *DriveLog 1.3.0*. Den leder till den nya sektionen *Inställningar → Om DriveLog* med det som är
  nytt i versionen.
- **Riktiga versionsnummer och logga i Home Assistant.** Varje version ges ut som en utgåva på
  GitHub, så HACS visar till exempel *1.2.2 → 1.3.0* i stället för en kod som "97aa56f" och
  erbjuder bara uppdatering när en ny version finns. Utgåvans text säger när integrationen inte
  har ändrats, så att omstarten av Home Assistant kan vänta. Integrationen visar DriveLogs logga
  från Home Assistant 2026.3.
- **Hela adressen i notiserna.** Notiserna visar nu hela adressen med husnummer för platser utan
  namn, och bostaden med sin adress. *Dölj platser på låsskärmen* under *Inställningar → Notiser*
  döljer dem som förut. Underlaget till arbetsgivaren ändras inte.
- **Företagslogga i underlaget.** Under *Inställningar → Milersättning och export* kan du ladda upp
  en logga (PNG eller JPEG, högst 1 MB), se den, byta den och ta bort den. Loggan läggs diskret
  uppe till höger i underlagets sidhuvud; *Visa loggan i underlaget* stänger av den utan att ta
  bort den.

## 1.2.3

Rättningar i Excel-underlaget.

- **Datumet syns i Excel.** Datumkolumnen var för smal, så Excel visade `#####` i stället för
  datumet.
- **Hela ärendet och hela adressen syns.** En text som bröts på fler rader i Excel än DriveLog
  räknade med blev avklippt. Raderna får nu den höjd som behövs.
- **Mellanstopp (via) går att stänga av.** Under *Inställningar → Milersättning och export*
  väljer du om underlaget ska visa stoppen på vägen ("via …") under målet eller bara start och
  mål. Från början är det påslaget, som förut.

## 1.2.2

- **Inga fler "Motstridiga mätarställningar" av avrundning.** Bilen och Volvos körjournal anger
  hela kilometer, så två resor i rad kan skilja en kilometer utan att något är fel. Sådan
  avrundning ger aldrig längre något ärende, någon lucka eller någon fråga om överlappande
  resor – oavsett vad toleransen under *Inställningar → Sammanslagning av resor* är satt till.
  Verkliga dubbletter och överlapp ger fortfarande ärende. Öppna ärenden som bara berodde på
  avrundning stängs av sig själva strax efter uppdateringen (ärenden och luckor du redan har
  hanterat rörs inte). Din inställning ändras inte.
- **Snabbare Granskning.** När du svarar Arbete, Privat eller Godkänn visas nästa ärende
  direkt, och du kan svara i rask takt – med musen eller med tangenterna. Förslagen för övriga
  resor räknas om i bakgrunden en stund efter ditt senaste svar, och listan uppdateras av sig
  själv när det är klart. Går ett svar inte att spara visas felet tydligt och ärendet läggs
  tillbaka. Har ett förslag (kategori eller syfte) hunnit ändras när du trycker *Godkänn
  förslag* får du se det nya förslaget i stället. Samma sak gäller massredigering och svar på
  frågorna i telefonen. Har du AI-bedömning påslagen väntar dina svar inte längre på den.
- **Underlagets filnamn** är nu *Underlag milersättning - förarens namn - registreringsnummer -
  november 2026.xlsx*. Förarens namn tas från *Inställningar → Allmänt*.
- **Ingen dubblerad adress i underlaget.** En plats som heter som sin adress skrevs två gånger
  ("Gatan 1, Ort, Gatan 1, Ort"). Nu står adressen en gång; platser med eget namn står som
  förut som *Namn, adress*.
- **Tiderna får plats på en rad** i Excel-filen ("06:53–08:51" och "ca 12:31–12:46" bröts förut
  på två rader).

## 1.2.1

Rättningar efter den första riktiga resan.

- **Kartan visas igen.** OpenStreetMaps kartservrar blockerade kartbilderna ("403 Access
  blocked") eftersom de kom utan avsändarsida. Nu skickar kartan sidans adress (bara
  `https://din-server/`, aldrig sökvägen) med anropen efter kartbilder; allt annat är som förut.
- **Adress i underlaget, aldrig "Hem".** Start och mål står alltid med gatuadress och ort. En
  bekräftad plats med eget namn står som *Namn, adress*, bostaden med sin adress. Saknar
  bostaden eller en annan bekräftad plats adress visas det före exporten, och månadens underlag
  väntar tills den är ifylld (även när orten saknas). Adressen skrivs på platsen under
  *Platser*, eller hämtas med knappen **Hämta adressen från kartan** (platsens position skickas
  då en gång till karttjänsten, standard OpenStreetMap). En privat plats står bara med adressen.
- **Starttid ur sträckan.** När bilen bara rapporterar efter ankomsten uppskattas starttiden ur
  sträckan och en snittfart – från dina egna resor mellan samma platser (eller med liknande
  längd) när de räcker, annars en vanlig fart för sträckan. Den visas som *≈ 07:35 (±5 min)*,
  står som *ca* i underlaget, och ett tryck visar varifrån den kommer. En starttid du själv
  angett rörs aldrig. Resor som redan saknar starttid får uppskattningen efter uppdateringen,
  utom i månader vars underlag redan har skapats av månadsskiftet.

## 1.2.0

Kalendern går nu att koppla utan appregistrering hos Microsoft.

- **Kalenderlänk (ICS).** Under *Integrationer → Microsoft Outlook* klistrar du in en
  kalenderlänk från Outlook på webben (*Inställningar → Kalender → Delade kalendrar*, välj *Kan
  visa rubriker och platser*). Ingen inloggning behövs. `webcal://`-länkar fungerar också.
- Länken är som ett lösenord: den sparas **krypterad** och visas aldrig igen – inte i appen,
  inte i loggarna. DriveLog provar länken innan den sparas och säger i klartext vad som är fel.
- Kalendern hämtas **var 30:e minut** i samma tidsfönster som tidigare, och *Synka nu* hämtar
  direkt. *Ta bort länken* raderar länken och mötena som hämtats från den; resorna finns kvar.
- **Tips** visas när länken bara visar ledig/upptagen-tid eller när inga möten har plats, så
  att du kan dela kalendern på rätt sätt.
- Outlook via appregistrering fungerar som förut. Båda sätten kan användas samtidigt, utan att
  mötena dubbleras.

## 1.1.0

Körjournalen sköter sig själv utan Excel-uppladdning: resorna kommer från bilen, får adress,
kategori och ärende, och månadens underlag skapas av sig självt. Importen fungerar som förut
för den som vill använda den.

- **Självgående läge** (nytt, påslaget som standard). Under *Inställningar → Automatik*
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
