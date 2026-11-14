# Excel-export

Exporten är en riktig `.xlsx`-fil byggd för att fungera som körjournal enligt
Skatteverkets rekommendationer – inte en kopia av Volvos export.

## Skatteverkets uppgifter

Skatteverket rekommenderar att en körjournal innehåller mätarställning vid årets början och
slut, och att den dessutom har: bilens registreringsnummer, aktuellt år, datum och
mätarställning vid resans start och slut, hur många kilometer varje resa är, start- och
slutadress, ärende/syfte, vilka platser, företag eller kontaktpersoner som besökts (behövs
inte vid privata resor) samt anteckningar om förare, tankning med mera. Journalen ska föras
löpande och omfatta alla resor, både privata och i tjänsten. En elektronisk körjournal ska
innehålla samma rubriker.

| Skatteverkets uppgift | I exporten |
| --- | --- |
| Registreringsnummer, år | *Översikt* |
| Mätarställning vid årets början och slut | *Översikt*, med källa – eller "Uppgift saknas" |
| Datum, mätarställning start/ankomst | *Körjournal* |
| Reslängd | *Körjournal* (km, uppdelat på tjänst och privat) |
| Resans start- och slutadress | *Körjournal* |
| Ärende och plats/företag/kontaktperson | *Körjournal*: Syfte och Besökta företag/kontaktpersoner |
| Anteckningar (förare, tankning m.m.) | *Körjournal* (förare, anteckningar) och *Bränsle och laddning* |

Registreringsnummer och förarens namn anges under *Inställningar*. Mätarställning vid
årsskiften hämtas från fordonsdata när den finns och kan annars registreras manuellt under
*Inställningar → Fordon → Mätarställningar*.

## Bladen

| Blad | Innehåll |
| --- | --- |
| **Översikt** | Fordon, period, förare, mätarställningar, summor i km och mil per kategori, antal resor, bränsle och el, samt en statusruta |
| **Körjournal** | En rad per journalresa: datum, start- och sluttid, adresser, mätarställningar, km, kategori, syfte, besökta företag/kontaktpersoner, förare, anteckningar, tjänst-/privat-km, antal delresor, datakälla och granskningsstatus |
| **Månadssammanställning** | Km och mil per månad och kategori, antal resor, årssumma |
| **Bränsle och laddning** | Liter och kWh per månad med täckningsgrad, tankningar, laddningar och kostnader |
| **Datakvalitet och avvikelser** | Resultatet av alla kontroller, dataluckor och oavstämda kilometer |
| **Källa och ändringshistorik** | Vilka originalresor varje journalresa består av (källa, originalfil och rad, originalanteckning) samt ändringshistorik |

Formateringen: frysta rubriker, filter, svenska datum- och talformat, anpassade
kolumnbredder och utskriftsinställningar (liggande, rubrikrad på varje sida, sidnummer).

## Formler

Summeringarna är äkta Excel-formler (`SUMIFS`/`COUNTIFS` mot bladet *Körjournal*), så de
följer med om du filtrerar eller justerar en rad. Varje formel har dessutom ett förberäknat
värde, så att filen visar rätt siffror även i program som inte räknar om formler (t.ex.
förhandsvisning på telefon). Testerna räknar om formlerna i LibreOffice och jämför med de
förberäknade värdena.

## Kontroller före export

*Export → Kontrollera* (eller automatiskt när filen skapas) går igenom journalen:

| Kontroll | Allvar |
| --- | --- |
| Syfte saknas för tjänsteresor | Fel |
| Föreslagna syften som inte bekräftats | Fel |
| Resor utan avgjord kategori | Fel |
| Preliminära klassningar som inte godkänts | Fel |
| Mätarställning saknas | Fel |
| Negativa körsträckor | Fel |
| Summeringar stämmer inte mot delsträckorna | Fel |
| Överlappande resor | Fel |
| Dubbletter | Fel |
| Oavstämda kilometer (öppna dataluckor) | Fel |
| Mätarställning vid periodens gräns saknas | Fel |
| Mätarställning och journal stämmer inte överens | Fel |
| Export av ett urval resor – ett urval kan aldrig stämmas av | Fel |
| Demonstrationsdata | Fel |
| Körsträcka avviker från mätarställningen på enskilda resor | Varning |
| Resor med osäker datakvalitet (rekonstruerade, okända tider) | Varning |
| Sammanslagna resor med lucka | Varning |
| Medvetet lämnade luckor | Varning |
| Perioden innehåller inga resor | Varning |
| Importerat urval | Varning eller information |
| Sammanfattad privat körning | Information |

Statusrutan på *Översikt* visar **Fullständig** endast när inga fel finns. Annars står
**OFULLSTÄNDIG – se Datakvalitet** tillsammans med bristerna. Filen går alltid att skapa –
bristerna döljs aldrig, de redovisas.

## Sammanslagna och blandade resor

En journalresa som består av flera originalresor exporteras som **en** rad. Kilometrarna
summeras per delsträcka, så ingenting räknas två gånger, och originalresorna går att följa i
bladet *Källa och ändringshistorik*. En resa med både privat körning och tjänstekörning får
sina kilometer fördelade i kolumnerna Tjänst-km och Privat-km.

## Okända och uppskattade uppgifter

- En starttid som källan inte känner till lämnas tom med en notering – den hittas aldrig på.
- Förbrukning redovisas bara där den är uppmätt per resa (importerad körjournal). Bilens
  snittförbrukning och tanknivå från API:t räknas inte som förbrukning per resa.
- Sammanfattade luckor ("Privat körning – sammanfattad") är tydligt märkta som sådana.

## Sekretess

Privata resor exporteras som standard utan detaljadresser (endast ort) och utan besökta
företag. Ändra under *Inställningar → Export*. Text som börjar med `=`, `+`, `-` eller `@`
skrivs som ren text, så en kommentar kan aldrig bli en formel.

Skapade filer lagras på servern tills du raderar dem under *Export*.

## Begränsningar

- Formlerna är verifierade i LibreOffice, inte i Microsoft Excel.
- En blandad resa räknas till den månad då den startade.
- Exporten skapas direkt när du klickar, inte som bakgrundsjobb.
- DriveLog avgör inte om journalen är skattemässigt godtagbar – den visar vad som finns och
  vad som saknas.
