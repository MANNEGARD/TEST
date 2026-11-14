# Användning

## Import av historiska körjournaler

1. Exportera körjournalen från Volvo Cars-appen (XLSX).
2. Öppna **Import**, dra in en eller flera filer (XLSX eller CSV).
3. **Förhandsgranskningen** visar per fil: igenkänt format, antal rader, nya resor,
   dubbletter, rader med fel (med förklaring per rad), period, körsträcka, bränsle och el.
   Inga resor skapas förrän du bekräftar.
4. Välj **datatäckning**:
   - *Komplett fordonsjournal* – filen innehåller alla resor för perioden,
   - *Importerat urval* – t.ex. bara tjänsteresor (föreslås automatiskt när det finns luckor
     i mätarställningen),
   - *Ofullständig period*.
5. Välj om sammanslagningsreglerna ska tillämpas, och klicka **Bekräfta import**.

Efter importen visas hur många resor som skapades, hur många som slogs ihop automatiskt
(regel A), hur många sammanslagningsfrågor som väntar (regel B) och hur många kilometer som
saknar journalpost.

- **Dubbletter:** samma fil kan laddas upp igen utan att något händer. Samma resa i en annan
  export känns igen på starttid och mätarställning.
- **Ångra:** varje import kan ångras under *Import → Tidigare importer*. Resorna makuleras
  och kan importeras på nytt.
- **Originalen bevaras:** kategori, adresser, mätarställningar, sträcka, förbrukning och
  anteckningar sparas exakt som i filen, tillsammans med filens kontrollsumma och radnummer.
  Importerade kommentarer ändras aldrig automatiskt.
- Historiska importer ger aldrig pushnotiser.

## Platser – gör detta direkt efter första importen

Under **Platser** finns alla adresser ur journalen med besöksstatistik. Fliken *Förslag*
visar vad systemet tror sig ha lärt sig:

- **Platstyper:** hem (där dagens första resa oftast börjar och sista slutar), hotell, kontor
  och kunder (ur kommentarerna). Godkänn eller avvisa var och en.
- **Slå ihop:** grannadresser på samma gata som troligen är samma plats.

Ge platserna egna namn ("Örebrokontoret"), välj typ och sätt gärna ut dem på kartan – då kan
positioner från bilen kopplas till platsen. Bara **bekräftade** platser påverkar
klassificeringen. En plats markerad *Privat destination* ger privat klassning.

Under *AI-minne* visas inlärda mönster per plats och rutt, dina korrigeringar (som kan
ångras) och manuella regler ("resor hit är alltid Arbete med syftet …").

## Körjournalen

**Körjournal** är huvudtabellen: datum, tider, adresser, mätarställningar, avstånd,
kategori, syfte, AI-status, datakälla och granskningsstatus.

- Sortera, filtrera (period, kategori, status, källa, favoriter) och sök i adresser och syften.
- **Byt kategori med ett klick** direkt i raden; syftet redigeras på plats.
- Markera flera rader för massändring, godkännande, sammanslagning eller export.
- *Lägg till resa manuellt* för körningar som saknas.

### Granskningsstatus

| Status | Betydelse |
| --- | --- |
| Importerad | Kategorin kommer från den importerade körjournalen |
| Godkänd | Du har bestämt eller godkänt |
| Automatiskt klassad (preliminär) | Hög säkerhet – gäller tills vidare, särredovisas tills du godkänt |
| Förslag | Väntar på ditt godkännande |
| Behöver granskas | Underlaget räcker inte – du måste välja |

### Datakvalitet i gränssnittet

Faktiska värden visas som vanlig text. Uppskattade värden visas kursivt med "≈" och en
förklaring. Saknade värden visas som "–". En okänd starttid visas som "okänd" – aldrig som
ett gissat klockslag.

## Resedetaljer

Klicka på en resa för att se karta, mellanstopp, mätarställningar, förbrukning,
klassningens underlag, matchande Outlook-möten, liknande tidigare resor, ingående
originalresor och ändringshistorik.

- **Kartan** visar de punkter som faktiskt är kända. En linje mellan dem är inte den körda
  vägen, och det står utskrivet.
- **Varför?** Varje signal bakom förslaget visas med riktning och vikt.
- **Fördela privat/tjänst:** dela en körning i delsträckor (t.ex. 30 km tjänst + 12 km
  privat). Summan måste stämma med den uppmätta sträckan.
- **Sammanslagning:** ångra, eller dela vid ett visst stopp.
- **Ångra ändring** återställer både resan och det systemet lärt sig av ändringen.

## Sammanslagning av resor

| Stillestånd | Vad som händer |
| --- | --- |
| Högst 15 minuter | Resorna slås ihop automatiskt (kan ångras) |
| Över 15 och högst 60 minuter | Du får en fråga – i notis och i granskningscentret |
| Över 60 minuter | Ingenting automatiskt; markera resorna och välj *Slå samman* |

Gränserna ändras under *Inställningar → Sammanslagning*. Den sammanslagna resan visar
mellanstoppen, summerar sträcka och förbrukning och behåller första starttid och sista
sluttid. Originalresorna finns alltid kvar.

Har delresorna olika kategorier slås de ändå ihop, men varje delsträcka behåller sin kategori
och resan måste fördelas manuellt. Saknas kilometer mellan resorna måste du uttryckligen
bekräfta en manuell sammanslagning – luckan döljs inte.

## Granskningscentret

**Granskning** samlar allt som behöver ditt beslut:

| Ärende | Åtgärd |
| --- | --- |
| Oklar kategori | Arbete / Privat |
| Oklart syfte | Välj ett tidigare syfte eller skriv ett nytt |
| Skattemässig bedömning | T.ex. resa mellan bostad och tjänsteställe – du avgör |
| Fördela privat/tjänst | Blandad resa |
| Förfrågan om sammanslagning | Ja / Nej |
| Saknad mätarställning, misstänkt datalucka | Komplettera eller notera |
| Konflikt mellan kalender och fordonsdata | Välj rätt bokning eller avvisa |
| Avvikelse, importproblem | Kontrollera |

Snabbflödet går igenom ärendena ett i taget med tangentbordet. Ärenden kan skjutas upp eller
avfärdas, och varje åtgärd kan ångras.

### Dataluckor

En lucka är kilometer i mätarställningen som saknar journalpost. För varje lucka kan du:

- **Markera som privat körning** – en sammanfattande privat post skapas (utan påhittade
  tider) så att mätarställningarna går ihop,
- **Lämna oredovisad** – luckan redovisas som oavstämd i exporten,
- komplettera genom att importera fler resor eller lägga in dem manuellt.

Privat körning räknas aldrig ut som "allt som inte är tjänst" – det kräver att perioden är
avstämd mot mätarställningen.

## Översikten

Nyckeltal för vald period (idag, vecka, månad, föregående månad, år eller eget intervall):
mil totalt, i tjänsten, privat och ej klassificerat; antal resor; bensin och el med
snittförbrukning; senaste mätarställning, position och synk; resor att granska. Diagrammen
visar sträcka över tid, privat mot arbete, bensin och el, destinationer, resvägar och
månadsjämförelser.

Förbrukning visas med **täckningsgrad**: snittet beräknas bara över sträcka där förbrukningen
är uppmätt per resa. Preliminärt klassade kilometer och ej journalförda kilometer
särredovisas, och en varning visas när perioden bara är ett importerat urval.

## Bränsle och energi

Registrera tankningar (liter, pris, mätarställning) och laddningar under **Bränsle &
energi**. Tankningar kan också föreslås ur bilens tanknivå – de märks då som uppskattade och
ersätter aldrig det du själv fört in. Skatteverket rekommenderar att liter och pris
antecknas vid tankning.

## Export

**Export** → välj period → *Kontrollera* visar brister innan filen skapas → *Skapa Excel*.
Se [Excel-export](excel-export.md).

## Notiser i telefonen

Med Home Assistant ansluten kommer frågorna som interaktiva notiser: *Arbete/Privat*,
*Vad var syftet?*, *Ska resorna slås samman?* samt en veckosammanställning. Svaren sparas
direkt i journalen. Obesvarade frågor ligger kvar i granskningscentret. Tysta timmar, max
antal per dygn och påminnelser ställs in under *Inställningar → Notiser*. Se
[Home Assistant](home-assistant.md).

## Demoläge

Utan nycklar kan varje integration provas i demoläge. Demodata hamnar på en separat
**demobil** som väljs i fordonsväljaren; en tydlig banderoll visas, exporten vattenmärks och
ingenting blandas med din riktiga journal. *Stäng av demoläge* tar bort demodatan.

## Inställningar i korthet

| Sektion | Innehåll |
| --- | --- |
| Fordon | Registreringsnummer, modell, förare, mätarställningar vid årsskifte |
| Tjänsteställe | Ordinarie tjänsteställe med giltighetstid, bostaden som tjänsteställe, bilförmån |
| Sammanslagning | Gränser för regel A och B, tolerans i mätarställning |
| AI | Trösklar, strikt läge, vad som får skickas till AI-leverantören |
| Notiser | Typer, tysta timmar, max per dygn, påminnelser, veckosammanställning |
| Kalender | Kalendrar, tidsfönster för matchning |
| Sekretess | Karta, geokodning, gallring, radering av historik |
| Export | Adresser för privata resor, ändringshistorik |
| Förkortningar | Egna förkortningar i kommentarer, t.ex. ortsförkortningar |
| Säkerhetskopia | Skapa och hämta kopior |
