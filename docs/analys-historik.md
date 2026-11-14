# Analys av de historiska körjournalerna

Importmotorn, datamodellen och klassificeringen är byggda utifrån de faktiska exporterna
från Volvo Cars-appen, inte utifrån ett konstruerat exempel. Det här dokumentet beskriver
vad analysen visade och vilka designbeslut den ledde till. Inga adresser, namn eller
kommentarer ur underlagen återges här eller någon annanstans i repot – testerna använder
anonymiserade filer med samma struktur (`backend/tests/fixtures/volvo_exports.py`).

## Underlag

Uppdraget beskriver åtta filer (mars–oktober 2026, 279 resor). Fem av dem laddades upp i
utvecklingssessionen (juni–oktober); mars, april och maj saknades. Analysen nedan avser de
fem filerna. Importmotorn hanterar båda formaten som beskrivs i uppdraget, så de tre
återstående filerna importeras på samma sätt – kör gärna testet mot samtliga originalfiler:

```bash
cd backend
DRIVELOG_REAL_EXPORT_DIR=/sökväg/till/originalfiler .venv/bin/python -m pytest tests/test_import.py -k real
```

Testet skriver inte ut något innehåll ur filerna.

| Mått (fem filer) | Värde |
| --- | --- |
| Resor | 203 |
| Körsträcka | 13 880,4 km |
| Bensin | 1 041,4 liter |
| El | 677,3 kWh |
| Olika kommentarstexter | 71 |
| Kategori | Alla "Arbete" |
| Olika adresser | 58 |

## Två exportformat

| | Äldre format | Nyare format |
| --- | --- | --- |
| Blad | `Sheet1` (+ tomma `Sheet2`, `Sheet3`) | `Trips` |
| Kolumner | 14 (med *Titel* och *Batteriregenerering*) | 12 |
| Datum | Text `2026-07-01 08:34`; enstaka rader som äkta datumceller | Text |
| Körtid | Tidsintervall-cell; enstaka rader som text `01:52 h` | Text `00:33 h` |
| Tal | Många decimaler (`41.244`) | En decimal, heltal blandat med decimaltal |
| Titel | Tom sträng eller tom cell | Saknas |

Formatet känns igen på kolumnrubrikerna (inte på bladnamn eller kolumnordning), så en
export med omkastade eller extra kolumner fungerar också. *Titel* sparas men används aldrig
som syfte.

## Iakttagelser som styrt designen

**1. Körtiden är inte tiden mellan start och slut.** I 52 av 203 rader är kolumnen *Tid*
mer än fem minuter kortare än *Stannade − Startade* – ibland flera timmar. Volvos app har
alltså redan slagit ihop delsträckor med stopp i vissa rader. Därför lagras körtid
(`duration_seconds`) separat från start- och sluttid, raderna flaggas
`source_includes_stops`, och importen räknar aldrig om tider.

**2. Mätarställningen är heltal, sträckan decimaltal.** Två på varandra följande resor kan
skilja ±1 km i mätarställning utan att någon körning saknas. Luckor och överlapp bedöms
därför med en tolerans (standard 2 km, inställbar).

**3. Underlagen är ett urval, inte en komplett journal.** Mellan resorna finns 69 luckor i
mätarställningen på sammanlagt cirka 8 000 km, av en total mätarsträcka på cirka 21 900 km.
Därför:

- varje import får en datatäckning (*Komplett fordonsjournal*, *Importerat urval*,
  *Ofullständig period*) och föreslås som urval när luckor finns,
- luckorna lagras som egna poster som syns i granskningscentret och i exporten,
- privat körning beräknas aldrig som "total körning minus arbetsresor".

**4. Alla resor är klassade Arbete.** Materialet är positivt referensmaterial för
tjänstekörning, inte ett balanserat träningsunderlag. Importerad historik räknas därför
separat från användarbekräftade beslut, väger lätt och kan aldrig ensam ge automatisk
klassning (se [AI-klassificering](ai-klassificering.md)).

**5. Tjänsteresor sker alla tider.** Resor startar alla veckodagar och mellan 00 och 23.
Tidpunkt och veckodag används därför aldrig som indikation på privat körning.

**6. Korta stopp är vanliga.** 11 par av på varandra följande resor har högst 15 minuters
stillestånd och 25 par mellan 15 och 60 minuter – precis de fall som regel A och B i
sammanslagningsmotorn hanterar. Historiska importer ger förslag i granskningscentret men
aldrig pushnotiser.

**7. Samma plats får flera adresser.** GPS-brus ger grannadresser på samma gata för samma
parkering. Platser skapas per adress, och platsregistret föreslår sammanslagning av
adresser på samma gata med närliggande nummer – användaren avgör.

**8. Kommentarerna är korta och personliga.** Förkortningar för orter och egna stavningar
förekommer. Originalkommentaren bevaras exakt; vid sidan av den härleds en normaliserad
nyckel (gemener, expanderade förkortningar – listan är inställbar) och en grov syftestyp
(kundbesök, möte, utbildning, personalärende, operativ insats, hemresa, hotell, kontorsarbete).
Bekräftade kommentarer återanvänds som standardformuleringar i syftesförslagen.

**9. Flerdagsresor med hotell.** Kedjor av typen hem → kontor → hotell → kontor → kund →
hem förekommer. Motorn känner igen återresor och hotellresor (även över flera dagar) och
föreslår en överordnad *Tjänsteresa* för kedjan.

## Verifierat mot originalfilerna

Med de fem filerna ger importen: 203 rader utan fel, 203 fysiska resor, 13 880,4 km
(identiskt med källan), 11 automatiska sammanslagningar enligt regel A, 25 frågor enligt
regel B och 58 platser. Upprepad import ger noll nya resor, och en ångrad import kan göras
om utan dubbletter.
