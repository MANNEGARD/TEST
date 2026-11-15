# Användning

DriveLog är byggt för ett enkelt flöde: **resorna kommer in → du bekräftar vilka som var
tjänsteresor och varför → en gång i månaden skapar du underlaget till arbetsgivaren.**

Du äger bilen och redovisar bara tjänsteresorna. Privat körning syns som luckor i
mätarställningen mellan resorna – det är väntat och ger aldrig fel eller varningar.

## Månadsrutinen

1. **Granskning** – gå igenom det som väntar: *Arbete eller privat?*, *Vad var syftet?*,
   *Ska resorna slås samman?* Med Home Assistant ansluten kommer samma frågor som notiser i
   telefonen under månaden, så att det mesta redan är klart.
2. **Export** – välj månad, kontrollera förhandsvisningen och klicka *Skapa Excel*.
3. Skicka filen till arbetsgivaren.

## Hur resorna kommer in

| Källa | Hur |
| --- | --- |
| **Import** | Ladda upp körjournalen från Volvo Cars-appen (XLSX). Verifierade resor med tider, adresser, mätarställning och förbrukning |
| **Volvo API** | Bilen läses av automatiskt och resor rekonstrueras. Positionen rapporteras först när en körning avslutats, så starttiden kan vara okänd – komplettera med import när du vill ha exakta uppgifter |
| **Manuellt** | *Körjournal → Lägg till resa* |

När samma resa senare importeras från Volvos körjournal ersätter den verifierade resan den
rekonstruerade, och dina beslut följer med.

## Import

1. Öppna **Import** och dra in en eller flera filer (XLSX eller CSV).
2. **Förhandsgranskningen** visar per fil: igenkänt format, nya resor, dubbletter och rader
   med fel (med förklaring per rad), samt period, körsträcka, bensin och el. Inga resor
   skapas förrän du bekräftar.
3. Klicka **Bekräfta import**.

- **Dubbletter:** samma fil kan laddas upp igen utan att något händer. Samma resa i en annan
  export känns igen på starttid och mätarställning.
- **Ångra:** varje import kan ångras under *Tidigare importer*.
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
- Markera flera rader för att sätta kategori eller syfte, godkänna, slå samman eller exportera.

| Status | Betydelse |
| --- | --- |
| Importerad | Kategorin kommer från den importerade körjournalen |
| Godkänd | Du har bestämt eller godkänt |
| Automatiskt klassad (preliminär) | Systemet är säkert, men resan tas med i underlaget först när du godkänt |
| Förslag | Väntar på ditt godkännande |
| Behöver granskas | Underlaget räcker inte – du väljer |

Faktiska värden visas som vanlig text, uppskattade kursivt med "≈", och saknade som "–".
En okänd starttid visas som "okänd", aldrig som ett gissat klockslag.

## Resedetaljer

Överst: karta, sträcka, kategori och syfte. Under hopfällda rubriker finns varför systemet
föreslog som det gjorde, matchande Outlook-möten, liknande tidigare resor, ingående
originalresor och ändringshistorik.

- **Kartan** visar de punkter som faktiskt är kända. En linje mellan dem är inte den körda
  vägen, och det står utskrivet.
- **Delvis privat?** Dela resan i delsträckor (t.ex. 30 km tjänst + 12 km privat). Summan
  måste stämma med den uppmätta sträckan, och bara tjänstedelen hamnar i underlaget.
- **Ångra ändring** återställer både resan och det systemet lärt sig av ändringen.

## Sammanslagning av resor

| Stillestånd | Vad som händer |
| --- | --- |
| Högst 15 minuter | Resorna slås ihop automatiskt (kan ångras) |
| Över 15 och högst 60 minuter | Du får en fråga |
| Över 60 minuter | Inget automatiskt; markera resorna och välj *Slå samman* |

Den sammanslagna resan visar mellanstoppen ("via …"), summerar sträckan och behåller första
starttid och sista sluttid. Originalresorna finns alltid kvar, och gränserna ändras under
*Inställningar → Sammanslagning av resor*.

Två resor slås inte ihop automatiskt om bilen har körts däremellan (kilometer saknas i
mätarställningen), och en privat delsträcka blir aldrig tjänst genom en sammanslagning.
Saknar en importerad resa klockslag (bara datum) går stilleståndet inte att bedöma; då ställs
ingen fråga, men du kan fortfarande slå samman manuellt.

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

## Översikten

Månadens tjänstemil, antal tjänsteresor, beräknad milersättning och vad som väntar på
granskning – samt diagram över tjänstekörning över tid och vanligaste destinationerna.
Milersättningen räknas bara på godkända resor. Mer statistik (resvägar, månadsjämförelse,
förbrukning) och *Veckan i korthet* (veckans tjänstemil, vad som väntar på granskning och
vanligaste destinationer) finns under hopfällda rubriker.

## Export

Se [Excel-export](excel-export.md). I korthet: **Export → välj månad → Skapa Excel.** Filen
innehåller ett blad med månadens tjänsteresor och en summering med mil och ersättning.
Vill du bara redovisa vissa resor markerar du dem i körjournalen och väljer *Exportera valda*.

## Notiser i telefonen

Med Home Assistant ansluten kommer frågorna som interaktiva notiser, och svaren sparas direkt
i journalen. Obesvarade frågor ligger kvar under *Granskning*. Tysta timmar, max antal per
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
| Sammanslagning av resor | Gränserna 15 och 60 minuter |
| Förslag och AI | Trösklar, strikt läge, vad som får skickas till AI-leverantören |
| Notiser | Typer, tysta timmar, max per dygn, påminnelser, veckosammanställning |
| Kalender | Tidsfönster för matchning |
| Tjänsteställe | Avancerat: ordinarie tjänsteställe, bostaden som tjänsteställe, förmånsbil och krav på godkännande vid skattemässig osäkerhet |
| Förkortningar | Egna förkortningar i kommentarer, t.ex. ortsförkortningar |
| Sekretess | Karta, geokodning, gallring, om originalfiler sparas vid import, radering av historik |
| Säkerhet | Lösenord, API-tokens |
| Säkerhetskopia | Skapa och hämta kopior |

### Fullständig körjournal

Behöver du i stället en fullständig körjournal där även privata resor redovisas och allt
stäms av mot mätarställningen – till exempel för en förmånsbil – väljer du *Fullständig
körjournal* under *Milersättning och export → Vad redovisas?*. Då visas luckor i
mätarställningen som något att reda ut. Välj samtidigt *Detaljerad* under *Exportens utseende*
om du vill ha den utförliga arbetsboken med flera blad – de två valen är oberoende av varandra.
