# Excel-export

Exporten är underlaget du lämnar till din arbetsgivare för milersättning. Den är medvetet
enkel: **ett blad, endast tjänsteresor, bara de uppgifter som behövs.**

## Vad som ingår

Ett kompakt sidhuvud – titel, period, förare, arbetsgivare, bil och registreringsnummer –
följt av en rad per tjänsteresa:

| Kolumn | Innehåll |
| --- | --- |
| Datum | Resans datum |
| Tid | Start- och sluttid |
| Från | Startadress |
| Till | Slutadress (mellanstopp anges som "via …") |
| Ärende | Resans syfte, med besökt företag eller kontaktperson när det är ifyllt |
| Mätarställning start / slut | Ur bilens körjournal |
| Km | Körd sträcka, en decimal |

Under tabellen summeras antal resor, kilometer och mil, samt ersättningen
(mil × kronor per mil). Summorna är riktiga Excel-formler, så de följer med om du justerar en
rad, och de har förberäknade värden så att filen visar rätt även i förhandsvisning på telefon.

Filen är formaterad för utskrift på liggande A4: rubrikraden upprepas på varje sida,
sidorna numreras och summeringen hamnar aldrig ensam på en sista sida. En vanlig månad ryms
på en eller två sidor (ungefär 25 resor på första sidan och 30 på varje följande).

## Vad som inte ingår

- **Privata resor.** Du äger bilen och redovisar bara tjänsteresorna. Luckor i
  mätarställningen mellan två redovisade resor är privat körning – de är inga fel och syns
  inte i underlaget.
- **Resor som inte är avgjorda eller godkända.** De tas med först när du godkänt dem.
  Exportsidan visar hur många som väntar, med länk till granskningen.
- Teknisk information om datakälla, datakvalitet och ändringshistorik. Den finns kvar i
  appen (resedetaljen) för den som behöver spåra en uppgift.

För en blandad resa – där en del av sträckan var privat – redovisas bara tjänstekilometrarna.

## Så här gör du

1. Öppna **Export** och välj period (förvald: föregående månad).
2. Förhandsvisningen visar exakt de rader och summor som kommer med.
3. Klicka **Skapa Excel** och hämta filen.

Filen får namnet `Körjournal_<regnr>_<år-månad>.xlsx`. Tidigare skapade filer ligger kvar
under *Export* tills du raderar dem.

## Skatteverkets uppgifter

Skatteverket anger att en arbetsgivare som betalar ut skattefri ersättning behöver veta vem
som har rest, arbetsorten med adress, **datum och klockslag** för när resan påbörjades och
avslutades, anledningen till tjänsteresan, och – vid milersättning för egen bil – **antal
körda kilometer**. Skatteverket skriver också att uppgifterna kan lämnas via en körjournal.

Körjournalsidan rekommenderar därutöver registreringsnummer, år, mätarställning vid resans
start och slut, start- och slutadress, ärende samt besökta företag eller kontaktpersoner.
Den sidan är skriven för arbetsgivarens bil (förmånsbil), men uppgifterna är med här också
eftersom de gör underlaget lätt att kontrollera.

| Uppgift | I exporten |
| --- | --- |
| Vem som har rest | Sidhuvud: förare |
| Datum och klockslag för start och slut | Datum, Tid |
| Arbetsort med adress, start- och slutadress | Från, Till |
| Anledning till resan, besökta företag | Ärende |
| Antal körda kilometer | Km, med summa |
| Mätarställning vid start och slut | Mätarställning start / slut |
| Registreringsnummer, år | Sidhuvud |

Personnummer och bostadsadress skrivs inte ut – din arbetsgivare har redan de uppgifterna.

### Ersättning per mil

Standardvärdet är **25 kronor per mil**, vilket är Skatteverkets schablonbelopp för skattefri
bilersättning för egen bil inkomståren 2025 och 2026. Betalar arbetsgivaren ut mer ska den
överskjutande delen hanteras som lön. Beloppet ändras under *Inställningar → Milersättning
och export*, där du också kan välja att inte visa något belopp alls.

Att privata resor med egen bil inte behöver redovisas står inte uttryckligen på
Skatteverkets sidor; det följer av att kravet för egen bil gäller tjänsteresans kilometer.
DriveLog ger ingen skatterådgivning – stäm av med din arbetsgivare vid tveksamhet.

## Kontroller

Innan filen skapas kontrolleras tjänsteresorna i perioden:

| Kontroll | Följd |
| --- | --- |
| Syfte saknas eller är inte bekräftat | Fel – åtgärda i granskningen |
| Negativ sträcka, överlappande resor, dubbletter | Fel |
| Mätarställning saknas (t.ex. manuellt inlagd resa) | Varning |
| Resor som väntar på godkännande eller inte är avgjorda | Varning – de tas inte med |

Filen går alltid att skapa. Exportsidan visar vad som behöver åtgärdas.

## Detaljerad export (valfri)

Under *Inställningar → Milersättning och export* kan utseendet ändras till **Detaljerad**.
Då skapas den utförliga arbetsboken med sex blad: översikt, körjournal med alla resor,
månadssammanställning, bränsle och laddning, datakvalitet samt källa och ändringshistorik.
Den är tänkt för den som behöver en fullständig körjournal – till exempel för en förmånsbil,
där även privata resor ska redovisas och allt stämmas av mot mätarställningen. Välj då också
*Fullständig körjournal* under *Vad redovisas?*; först då behandlas luckor i mätarställningen
som något som ska redas ut.

## Övrigt

- Text som börjar med `=`, `+`, `-` eller `@` skrivs som ren text, så en kommentar kan aldrig
  bli en formel.
- Formlerna är kontrollerade i LibreOffice, inte i Microsoft Excel.
- En okänd starttid lämnas tom – den hittas aldrig på.
- Varje resas sträcka redovisas med en decimal, och summan är summan av de redovisade
  värdena. Den går alltså att kontrollräkna för hand. Översiktens milersättning räknas på
  samma sätt; den totala körsträckan i statistiken summerar däremot oavrundade värden och kan
  skilja någon tiondels kilometer.
- En resa som slutar efter midnatt visas som `23:37–02:08 (+1)`.
