# AI-klassificering, syften och inlärning

Varje resa får en av tre skattemässiga kategorier – **Arbete**, **Privat** eller **Ej
avgjord** – samt en intern underkategori (kundbesök, kontorsarbete, möte, utbildning,
personalärende, operativ insats, hemresa, hotell, förflyttning, arbetsresa, privat ärende).

## Tre steg

### 1. Regelmotorn (alltid)

En deterministisk motor (`classify/signals.py`) tar fram mätbara signaler. Varje signal har
riktning, vikt och en svensk förklaring som visas i resedetaljen.

| Signal | Riktning | Kommentar |
| --- | --- | --- |
| Destination markerad som privat plats | Privat | Stark |
| Känd kund / känt kontor (bekräftad plats) | Arbete | |
| Bostad ↔ ordinarie tjänsteställe | Privat + skattenotering | Kräver alltid godkännande, se nedan |
| Bostaden är tjänsteställe | Arbete | Enligt dina inställningar |
| Manuell regel för plats eller rutt | Enligt regeln | Stark |
| Manuell regel "Fråga mig alltid" | Ej avgjord – du avgör varje gång | Går före allt annat; AI anropas inte |
| Användarbekräftad historik för rutten/destinationen | Arbete eller privat | Växer med antalet bekräftelser |
| Importerad historik (urval) | Arbete | Låg, begränsad vikt |
| Tidigare korrigeringar av liknande resor | Mot korrigeringen | Minskar tilliten till den äldre regeln |
| Matchande Outlook-möte | Arbete (privat bokning → privat) | Vikt efter träffens styrka |
| Återresa efter tjänsteärende / privat ärende | Samma som utresan | Även över flera dagar |
| Hotell under pågående tjänsteresa | Arbete | |
| Kalenderbokning på annan plats samtidigt | – | Tvingar granskning |
| Resa mellan hemmet och ett av era kontor, när du valt att det är tjänst | Arbete | Ersätter regeln bostad ↔ tjänsteställe (1.3.0) |
| Lunchmönster: kort tur från kontoret mitt på dagen | Privat | Bara när hela mönstret stämmer, se Lunchregeln |
| Bokningar på resmålets ort de närmaste dagarna | Arbete | Resa dagen före, t.ex. söndag till Helsingborg inför måndagens möte |

**Tidpunkt och veckodag används aldrig som privatindikator – med ett undantag: lunchregeln,
som kräver hela mönstret (kort tur från kontoret en arbetsdag, ett kort stopp och tillbaka
till samma kontor inom lunchfönstret). Klockslaget ensamt räcker aldrig.** Kvällar och helger
är vanliga tjänsteresetider i det historiska underlaget. Kvällsärenden under en tjänsteresa
(en kort tur från hotellet efter kontorstid) ger alltid en fråga, och resor hem ↔ kontor
godkänns oavsett tid när du har valt det.

Signalerna vägs samman till en kategori och en *beslutsindikator* (0–1). Den är ett
kalibrerbart tröskelvärde, inte en statistisk sannolikhet. Indikatorn begränsas uppåt när
underlaget är svagt:

- enbart importerad historik som stöd → högst 0,75 (kan aldrig bli automatisk klassning),
- osäker datakvalitet (rekonstruerad resa, okänd starttid, saknad mätarställning) → högst 0,70,
- motstridiga signaler eller kalenderkonflikt → högst 0,60 och obligatorisk granskning.

### 2. AI-lagret (valfritt)

Är en AI-leverantör ansluten (Anthropic eller OpenAI, utbytbart) anropas den när reglerna
inte räcker: kategorin är oavgjord, en tjänsteresa saknar ett tillräckligt syfte, eller en
kalenderbokning stämmer med resan (då formulerar AI ärendet ur bokningen). Den anropas aldrig
för skattemässiga gränsfall (de avgör du) och aldrig när din regel säger "fråga mig alltid".

**Underlagsnivå** (*Inställningar → Förslag och AI*, `ai.context_level`):

- **Fullständigt underlag** (standard från 1.3.0, gäller direkt efter uppdateringen). Allt som
  hjälper bedömningen skickas, se tabellen under *Sekretess mot AI-leverantören*. Med den här
  nivån anropas AI även när reglerna inte hittat något underlag alls.
- **Minimalt**: det tidigare, avidentifierade underlaget (platsens namn och ort, veckodag,
  klockslag, sträcka, regelmotorns signaler och dina tidigare ärenden). Valen för
  kalenderrubrik, brödtext och gatuadresser gäller bara här. AI anropas bara när det finns
  något underlag att väga.

Modellen svarar med strukturerad JSON: kategori, beslutsindikator, ett **färdigt formulerat
ärende** för tjänsteresor, om ärendet har direkt stöd i underlaget (`purpose_supported`),
korta belägg och om resan behöver granskas. Svaret passerar sedan skyddsräcken:

- **AI kan föreslå, aldrig besluta.** Ett rent AI-förslag ger som mest "begär godkännande".
  Allt AI-ärende sparas som förslag som du godkänner med ett tryck – AI godkänner aldrig något
  själv.
- **AI kan inte vända ett regelbeslut.** Avviker bedömningen flaggas resan för granskning.
- **Inga påhittade namn.** Varje namn, ort, företag, förkortning och siffra i ärendet måste
  finnas i det som skickades: platsnamn, orter, företag, kalenderbokningarnas rubrik, plats,
  beskrivning och deltagarnas namn, tidigare bekräftade ärenden, "Om mitt arbete",
  arbetsgivaren och dina förkortningar. En extern deltagares e-postdomän räknas *inte*: ett
  företagsnamn får aldrig hittas på ur en domän. Allmänna ord, veckodagar och sammansättningar
  ("Projektmöte") är tillåtna. Ett ärende med ett namn utan stöd **avvisas** och nästa steg i
  ordningen nedan tas; avvisningen syns i resans underlag.
- **Form:** en rad, högst 80 tecken, inga länkar eller `<>{}`. Annars avvisas förslaget (det
  kapas aldrig).
- **Ärendetyp utan stöd** (t.ex. "Utbildning" utan något i underlaget som säger det), eller en
  text som bara är "Tjänsteresa"/"Möte": förslaget **behålls** men märks *Förslag att
  bekräfta* (`supported: false`). Det blir aldrig grund för ett automatiskt godkännande.
- **Reservmall:** gav AI inget användbart ärende och finns inget regelförslag blir förslaget
  "Tjänsteresa till <plats>" – aldrig tomt och aldrig "syfte behöver bekräftas".
- **Indata är data, inte instruktioner.** Underlaget skickas avgränsat inom `<data>…</data>`.
  Varje sträng tvättas först: styrtecken och osynliga tecken (t.ex. riktningsomkastning) tas
  bort, `<` och `>` blir `‹` och `›` så att ingen text kan stänga avgränsningen, länkar blir
  "[länk]", koder och lösenord i fritext ("Lösenord: …", "Portkod 4711", mötes-ID) tas bort,
  långa nyckelliknande strängar, chassinummer och koordinater tas bort, och allt kapas.
  Valideringen sker efteråt, så en "lydig" modell kan inte ändra utfallet.
- **Fel hos leverantören** påverkar inte journalen: reglernas resultat gäller.

**AI-genomgång** (knappen *Bedöm med AI* på resan, *Gå igenom med AI* i körjournalen och *Gå
igenom väntande med AI* i Granskning): webben kör högst 25 resor per gång, en i taget, med
`POST /trips/{id}/classify`. Resor som redan är godkända (av dig eller automatiskt) eller
importerade tas aldrig med. Utan leverantör används bara reglerna och ingenting lämnar
servern. I självgående läge prövar autopiloten resan direkt efteråt; övriga resors förslag
räknas om i bakgrunden.

### 3. Automationsnivå

| Beslutsindikator | Resultat | Status |
| --- | --- | --- |
| ≥ 0,85 och inget kräver granskning | Automatisk preliminär klassning | *Automatiskt klassad (preliminär)* |
| ≥ 0,60 | Förslag som väntar på godkännande | *Förslag* |
| < 0,60 | Obligatorisk manuell granskning | *Behöver granskas* (kategori Ej avgjord) |

Trösklarna ändras under *Inställningar → Förslag och AI*. I **strikt läge** blir ingenting automatiskt
klassat – allt måste godkännas. Preliminära klassningar särredovisas i dashboard och
flaggas i exporten tills de godkänts.

Resor som du själv avgjort, och resor med källmärkt klassning ur en importerad körjournal,
bedöms aldrig om. För dem föreslås bara ett saknat syfte.

### 4. Självgående läge

I självgående läge (standard från 1.1.0) blir en automatisk preliminär klassning av en resa
från bilen i stället ett *Förslag*. Det som godkänns av sig självt avgörs av en egen,
strängare regeltabell i `classify/autopilot.py`, prövad i den här ordningen: *Resa till
arbetet*, *Privat plats*, *Din regel för …*, *Hem ↔ kontor i …* (när du valt det), *Lunch*,
*Kvällsärende under tjänsteresa* (alltid en fråga), *Som de 3 senaste gångerna*, *Hemresa
efter …* och *Antaget kundbesök*. Allt annat blir en fråga. Tabellen och spärrarna beskrivs i
[Användning → Självgående läge](anvandning.md#självgående-läge).

- Inget godkänns automatiskt som Arbete när någon signal pekar mot Privat, när reglerna
  noterar motstridiga signaler, för resor mellan bostad och tjänsteställe, eller när målet är
  hemmet, en privat plats, tjänstestället eller ett hotell.
- Ett automatiskt godkännande eller antagande räknas aldrig in i mönstren och aldrig som
  bekräftad historik.
- **AI och platsantagandet:** för en ny, obekräftad plats som kartan har ett namn för men
  ingen tydlig typ (art *oklar*) får AI-leverantören – bara om en redan är inkopplad, och bara
  i självgående läge – platsens namn, kartans typ, ort, veckodag och klockslag. Aldrig
  koordinater, gatuadress eller personuppgifter. Bedömer den platsen som arbete, utan avvikelse
  mot reglerna, kan resan antas som kundbesök. Ärendet blir AI:ns text bara om den klarar
  fabriceringsskyddet ovan och har stöd i underlaget, annars "Kundbesök <namn>". Utan
  leverantör blir det en fråga.
- **Hem ↔ kontor (1.3.0):** under *Tjänsteställe* kan du välja att resor mellan hemmet och
  vilket som helst av arbetsgivarens kontor (platser markerade som kontor eller ordinarie
  arbetsplats) är tjänsteresor. Då godkänns de automatiskt – även på kvällar och helger – med
  skälet "Hem ↔ kontor i <ort>". Övriga spärrar gäller (mätarställning saknas, flera
  körningar, kalenderkonflikt, "fråga alltid", en privat plats). Skatteverkets huvudregel är
  att sådana resor är privata; valet är ditt. Avstängt gäller *Resa till arbetet* som förut.
  Befintliga installationer fick valet påslaget vid uppdateringen till 1.3.0 (ägarens val), nya
  installationer har det avstängt.
- **Ärendet ur kalendern utan fråga:** godkänns en resa automatiskt som Arbete av din regel,
  Hem ↔ kontor, historiken eller hemresan, och en kalenderbokning stämmer starkt med resan
  (eller vistelsen/dagarna efter), blir ärendet kalenderns (AI-formulerat om det klarat
  namnkontrollen, annars bokningens rubrik). Resan märks *Ärende ur kalendern* i månadens
  genomgång och du kan rätta med ett tryck (*Ärendet stämmer inte*): bokningen väljs då inte
  igen för resan, ärendet blir regelns eller historikens och frågas om.

### 5. Lunchregeln

Korta turer från kontoret mitt på dagen är oftast lunch (`classify/lunch.py`, påslagen som
standard, *Inställningar → Lunchregeln*). Båda resorna blir privata utan fråga när **hela**
mönstret stämmer:

1. Utresan börjar vid ett av arbetsgivarens kontor (bekräftad plats) en arbetsdag (dagarna i
   kontorstiden, inte röda dagar).
2. Den är kort: högst 10 minuter (när körtiden är känd) och 8 km.
3. Stoppet varar mellan 20 och 45 minuter på samma ställe (okänt stopp räknas aldrig).
4. Återresan är lika kort tillbaka till samma kontor, och allt sker mellan 11:00 och 13:00.

Regeln gäller inte om stället är en bekräftad kund, ett kontor, hemmet eller ett hotell, har
ett företag, om du har en egen regel för det, om du tidigare har sagt (eller importen säger)
att en resa dit var i tjänsten, om kartan säger kontor/industri/lager, eller om en
kalenderbokning stämmer med någon av resorna. Kartans restaurang, kafé eller butik stärker
signalen. Du får ingen fråga om utresan medan regeln väntar på återresan; kommer ingen
återresa inom 45 + 10 + 10 minuter hanteras utresan som vanligt. I manuellt läge blir
lunchresorna förslag på Privat. Gränserna ändras under *Inställningar → Lunchregeln*.
Regeln kräver att minst ett kontor finns som bekräftad plats.

**Lunch hemma.** Går turen från kontoret hem och tillbaka till samma kontor enligt mönstret
gör regeln inte resorna privata av sig själv. Har du valt att resor mellan hemmet och kontoren
är tjänsteresor (regel K) godkänns de inte heller automatiskt som tjänst: DriveLog väntar på
återresan och ställer sedan en fråga (*Kort tur hem mitt på dagen – lunch hemma eller
tjänst?*). Utan det valet gäller de vanliga reglerna.

## Skattemässig bedömning

Att en resa är arbetsrelaterad betyder inte automatiskt att den är en tjänsteresa.
Skatteverket: *"Resor du gör till och från ditt arbete räknas som privata resor."* och
tjänsteresa är en resa *"till ett annat ställe än där hen normalt utför sina
arbetsuppgifter (tjänstestället)"*.

Som standard är inget tjänsteställe konfigurerat, och då tillämpas ingen arbetsreseregel –
resor till dina kontor föreslås utifrån platstyp, historik och kalender, och du avgör själv
vad som redovisas. Vill du att systemet ska uppmärksamma resor mellan bostaden och ett
ordinarie tjänsteställe anger du under *Inställningar → Tjänsteställe*:

- ordinarie tjänsteställe (en plats ur platsregistret) med giltighetsperiod – flera perioder
  kan läggas upp när tjänstestället byts,
- om bostaden är tjänsteställe. Med konfiguration föreslås
resor mellan bostaden och tjänstestället som privata, men **alltid med krav på ditt
godkännande** och en förklarande notering – systemet fattar aldrig det beslutet åt dig. En
manuell regel för rutten går före.

DriveLog ger ingen skatterådgivning. Vilket tjänsteställe som gäller för dig avgör du
tillsammans med din arbetsgivare eller rådgivare.

## Syften

Ärendets text väljs i den här ordningen (kategorin avgörs som ovan):

0. Din egen regel med fast text i läget **"Använd alltid texten"**.
1. **Kalendern + AI först:** en bokning som stämmer med resan i tid och plats (stark träff –
   platsen, företaget, orten i rubriken eller beskrivningen, eller en extern deltagares domän
   som stämmer med resmålets företag; aldrig tid ensam, aldrig ett onlinemöte, aldrig en privat
   bokning). För utresan till en ort där du blir kvar får AI bokningarna för hela vistelsen
   och skriver en sammanfattande kommentar ("Rest till Jönköping för intervjuer och kundbesök
   med Lennart"), och motsvarande för hemresan. En utresa utan egen bokning samma dag bedöms
   mot bokningarna de två följande dagarna på resmålets ort ("Rest till Helsingborg inför möte
   med Lennart på måndag"). Utan AI: bokningens rubrik, kompletterad med platsen vid behov.
2. Din egen regel med fast text (standardläget **"när kalendern inte säger något annat"**).
3. Återresa ("Hemresa efter …"), hotell under tjänsteresa, ett dominerande bekräftat ärende.
4. En bokning där bara tiden stämmer: förslag att bekräfta.
5. AI:ns fria förslag ur hela underlaget.
6. Reservmallen "Tjänsteresa till <plats>" (bara när AI har anropats); utan AI "Besök på
   <plats> – syfte behöver bekräftas."

Deltagare med samma e-postdomän som din arbetsgivare (*Inställningar → Kalender → Min
arbetsgivares e-postdomän*, ifylld av DriveLog ur kalendern) räknas som kollegor och skrivs
"med <namn>"; övriga är externa. Ett företagsnamn skrivs bara när det står i platsen,
rubriken eller beskrivningen, annars "Kundbesök i <ort>".

Alla förslag utom manuella regler och ärenden ur kalendern på automatiskt godkända resor
kräver bekräftelse. Ett syfte du själv skrivit eller godkänt skrivs aldrig över – inte heller
när kalendern ändras i efterhand; ett obekräftat förslag uppdateras vid nästa bedömning.

I telefonen kommer ärendeknapparna i ordningen: resans ärende ur kalendern, platsantagandet
"Kundbesök <namn>", dina tidigare bekräftade ärenden för rutten och målet, och AI:ns förslag
bara när du inte har några egna. "Alltid: …" erbjuds aldrig för ett ärende ur kalendern eller
från AI.

## Inlärning

Varje ändring du gör sparas med ursprunglig och ny kategori, ursprungligt och nytt syfte,
plats och platstyp, veckodag, tid, kalenderkontext och modellens tidigare bedömning.

Mönster per destination och per rutt räknas om **från grunden** efter varje beslut, ur två
separata källor:

- *bekräftat* – resor du godkänt eller korrigerat,
- *importerat* – källmärkt klassning ur importerad körjournal (väger lätt).

AI:ns egna obekräftade förslag räknas aldrig in, så en felaktig gissning kan inte förstärka
sig själv. Eftersom allt räknas om försvinner effekten av en korrigering helt när den ångras
(*Ångra ändring* i resedetaljen eller under *Platser → AI-minne*).

Platser lär sig koordinater automatiskt när en fordonsposition kan kopplas till en
importerad adress. Platstyper föreslås ur historiken (hem, hotell, kontor, kunder) men
gäller först när du godkänt dem.

## Sekretess mot AI-leverantören

**Fullständigt underlag** (standard):

| Skickas | Skickas aldrig |
| --- | --- |
| Namn och gatuadress för start, mål och mellanstopp – även hemmet och privata platser | Koordinater |
| Kartans namn och typ för okända ställen, platsanteckningar | Chassinummer (VIN), registreringsnummer |
| Dagens alla resor med tider, platser, kategorier och bekräftade ärenden; resan före och efter | API-nycklar, tokens, lösenord, Home Assistant-token |
| Historiken för samma mål och rutt, dina regler | Kalenderlänken |
| Kalenderbokningarna för dagen, och vid resor till en annan ort hela vistelsen och de närmaste dagarna: rubrik, plats, tvättad beskrivning och deltagarnas namn (kollega/extern och den externa e-postdomänen) | E-postadresser |
| Privata bokningar: bara tiden ("Privat bokning") | Privata bokningars rubrik, plats, beskrivning och deltagare |
| Arbetsgivarens namn och "Om mitt arbete" | Koder, mötes-ID och lösenord i beskrivningar och anteckningar |
| Lunchmönstret, regelmotorns signaler och förslag | Obekräftade AI-förslag på andra resor |

**Minimalt:**

| Skickas | Skickas aldrig |
| --- | --- |
| Platsetikett eller ort, platstyp | Koordinater |
| För nya obekräftade platser: kartans namn och typ (t.ex. `building=warehouse`) | |
| Veckodag, klockslag, sträcka, antal stopp | VIN, registreringsnummer |
| Regelmotorns signaler | Hemadress (visas som "Hemmet") |
| Kalenderrubrik, tid och plats för matchade bokningar (kan stängas av) | Privata platsers namn ("Privat plats") |
| Tidigare bekräftade syften för samma resa | Gatuadresser (om inte uttryckligen påslaget) |
| | Kalenderns brödtext (om inte uttryckligen påslaget; då de första 300 tecknen) |

På nivån Minimalt skickas rubrik och brödtext som de står i kalendern: koder, länkar,
e-postadresser eller lösenord som någon har skrivit där tas inte bort. Vill du att de tas bort,
välj Fullständigt underlag eller stäng av rubrik och brödtext.

## Koppla in en leverantör

*Integrationer → AI-leverantör*: välj leverantör, klistra in API-nyckeln, välj eventuellt
modell och klicka *Testa*. Nyckeln lagras krypterad och visas aldrig igen. Status blir
"Ansluten" först efter ett lyckat test.

- **Anthropic:** standardmodell `claude-opus-5-5`. Anrop görs via det officiella SDK:t med
  strukturerad JSON-utdata.
- **OpenAI:** ange den modell du vill använda.

Utan leverantör fungerar allt utom steg 2 – reglerna, syftesförslagen, lunchregeln och
inlärningen är oberoende av AI-leverantören.
