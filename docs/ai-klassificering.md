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

**Tidpunkt och veckodag används aldrig som privatindikator.** Kvällar och helger är vanliga
tjänsteresetider i det historiska underlaget.

Signalerna vägs samman till en kategori och en *beslutsindikator* (0–1). Den är ett
kalibrerbart tröskelvärde, inte en statistisk sannolikhet. Indikatorn begränsas uppåt när
underlaget är svagt:

- enbart importerad historik som stöd → högst 0,75 (kan aldrig bli automatisk klassning),
- osäker datakvalitet (rekonstruerad resa, okänd starttid, saknad mätarställning) → högst 0,70,
- motstridiga signaler eller kalenderkonflikt → högst 0,60 och obligatorisk granskning.

### 2. AI-lagret (valfritt)

Är en AI-leverantör ansluten (Anthropic eller OpenAI, utbytbart) anropas den **endast** när
reglerna inte räcker: kategorin är oavgjord trots att det finns underlag, eller syftet
saknas. Den anropas aldrig helt utan underlag (då kan den bara gissa) och aldrig för
skattemässiga gränsfall (de avgör du).

Modellen får regelmotorns signaler och ett minimalt underlag och svarar med strukturerad
JSON. Svaret passerar sedan skyddsräcken:

- **AI kan föreslå, aldrig besluta.** Ett rent AI-förslag ger som mest "begär godkännande".
- **AI kan inte vända ett regelbeslut.** Avviker bedömningen flaggas resan för granskning.
- **Inga påhittade uppgifter.** Ett genererat syfte prövas i två steg. (1) Varje namn, ort,
  företag, förkortning och siffra måste finnas i underlaget (platsnamn, orter, företag,
  kalenderrubrik, tidigare bekräftade syften); allmänna ord och deras sammansättningar
  ("Projektmöte", "Avstämningsmöte") är tillåtna. (2) Själva *ärendetypen* – kundbesök, möte,
  utbildning, personalärende, övernattning och liknande – godtas bara när den har stöd i
  underlaget: en matchad kalenderbokning, ett tidigare bekräftat syfte för platsen eller
  rutten, platsens typ (kund, kontor, hotell) eller regelmotorns eget förslag. Särskilda
  ärenden som "styrelsemöte" eller "upphandling" måste stå i underlaget. Annars förkastas
  förslaget och regelmotorns försiktiga "… – syfte behöver bekräftas" står kvar; avvisningen
  syns i resans underlag.
- **Indata är data, inte instruktioner.** Kalenderrubriker och kommentarer skickas avgränsade
  och kan inte ändra utfallet även om modellen skulle "lyda" dem – valideringen sker efteråt.
- **Fel hos leverantören** påverkar inte journalen: reglernas resultat gäller.

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
strängare regeltabell i `classify/autopilot.py`: *Resa till arbetet*, *Privat plats*, *Din
regel för …*, *Som de 3 senaste gångerna*, *Hemresa efter …* och *Antaget kundbesök*. Allt
annat blir en fråga. Tabellen och spärrarna beskrivs i
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
  fabriceringsskyddet ovan, annars "Kundbesök <namn>". Utan leverantör blir det en fråga.

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

Förslag byggs i den här ordningen, och det första som har stöd används:

1. Manuell regel för platsen.
2. Rubriken på den matchande Outlook-bokningen, kompletterad med platsen vid behov
   ("Personalmöte på Örebrokontoret.").
3. Återresa: "Hemresa efter …" med utresans bekräftade syfte, annars "Hemresa från <ort>.".
4. Hotell under tjänsteresa: din egen standardformulering om en finns.
5. Ett dominerande, tidigare bekräftat syfte för samma resa.
6. Känd plats men oklart syfte: "Besök på <plats> – syfte behöver bekräftas."
7. Annars inget förslag alls.

Alla förslag utom manuella regler kräver bekräftelse. Ett syfte du själv skrivit eller
godkänt skrivs aldrig över. Motiveringen sparas tillsammans med förslaget.

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

| Skickas | Skickas aldrig |
| --- | --- |
| Platsetikett eller ort, platstyp | Koordinater |
| För nya obekräftade platser: kartans namn och typ (t.ex. `building=warehouse`) | |
| Veckodag, klockslag, sträcka, antal stopp | VIN, registreringsnummer |
| Regelmotorns signaler | Hemadress (visas som "Hemmet") |
| Kalenderrubrik, tid och plats för matchade bokningar (kan stängas av) | Privata platsers namn ("Privat plats") |
| Tidigare bekräftade syften för samma resa | Gatuadresser (om inte uttryckligen påslaget) |
| | Kalenderns brödtext (om inte uttryckligen påslaget) |

## Koppla in en leverantör

*Integrationer → AI-leverantör*: välj leverantör, klistra in API-nyckeln, välj eventuellt
modell och klicka *Testa*. Nyckeln lagras krypterad och visas aldrig igen. Status blir
"Ansluten" först efter ett lyckat test.

- **Anthropic:** standardmodell `claude-opus-5-5`. Anrop görs via det officiella SDK:t med
  strukturerad JSON-utdata.
- **OpenAI:** ange den modell du vill använda.

Utan leverantör fungerar allt utom steg 2 – reglerna, syftesförslagen och inlärningen är
oberoende av AI-leverantören.
