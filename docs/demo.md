# Demo (klickbar webbapp utan server)

Demot är den riktiga webbappen – samma sidor och komponenter – byggd som statiska filer, där
servern är simulerad i webbläsaren med påhittade uppgifter. Det kan läggas upp på vilken
statisk värd som helst (t.ex. Netlify) och kräver ingen backend och ingen inloggning.

Överst på varje sida står **Demo – Påhittade uppgifter, inget sparas på någon server** med knappen
**Återställ demot**. Sidorna indexeras inte (`noindex` i sidan och `X-Robots-Tag` i `_headers`).

## Bygga och prova

```bash
cd frontend
npm run build:demo     # → out-demo/ (statiska filer, ca 22 MB, ca 1 200 filer)
npm run e2e:demo       # Playwright mot out-demo/ via en lokal filserver
```

Lägg upp **innehållet i `frontend/out-demo/`** som webbplatsens rot (på Netlify: dra katalogen
till *Deploys*, eller `netlify deploy --dir=frontend/out-demo --prod`). Inga omdirigeringar
eller serverfunktioner behövs; `_headers` och `404.html` följer med.

Utan flaggan byggs den vanliga appen precis som förut (`npm run build`). Demokoden laddas bara
när `NEXT_PUBLIC_DRIVELOG_DEMO=1` är satt vid bygget; annars viks kroken i
`src/lib/demo-hook.ts` bort och inget av `src/demo/` följer med.

## Hur det fungerar

| Del | Var |
| --- | --- |
| Flaggan, statisk export (`output: "export"`, `trailingSlash`, bara `.tsx` som sidor) | `frontend/next.config.ts` |
| Kroken: `api.ts` (alla `api.get/post/…`), importens uppladdning och exportfilens länk | `frontend/src/lib/demo-hook.ts` |
| Den simulerade servern, tillståndet (localStorage) och demots klocka | `frontend/src/demo/` |
| Startdata: inspelade svar från den riktiga backenden | `frontend/demo-static/startdata.json` |
| Exempelfilen (Excel skapad av backenden ur samma resor) | `frontend/demo-static/korjournal-exempel.xlsx` |
| Säkerhetsrubriker, `noindex`, `robots.txt` | `frontend/demo-static/_headers`, `robots.txt` |
| Bygger demot utan att röra det vanliga bygget i `.next/` (läggs undan och tillbaka) | `frontend/scripts/build-demo.mjs` |
| Samlar exporten i `out-demo/` och mäter storleken | `frontend/scripts/finish-demo.mjs` |

Inga anrop mot `/api` lämnar webbläsaren: `api.ts` skickar dem till den simulerade servern, och
ett skyddsnät fångar eventuella andra `fetch` mot `/api`. Demot läser bara sina egna statiska
filer (`/demo/startdata.json`, exempelfilen) och kartplattor från OpenStreetMap.

Dynamiska adresser (`/journal/<id>`, `/m/q/<id>`, `/m/month/<månad>`) är förbyggda för alla
resor och frågor i startdatat, för 40 reserverade id:n som demot delar ut till resor och frågor
som skapas under körning, och för månaderna 2025–2027. Direktlänk och omladdning fungerar.

Demots klocka börjar vid startdatats "nu" (måndag 16 november 2026 kl. 09.31) och går sedan i
vanlig takt, så att "i dag", vetotiden och månadskortet stämmer med uppgifterna.

## Vad som går att göra

- Svara på frågor – i Granskning, i mobilens snabbfråga (`/m/q/<id>`) och i demotelefonen under
  *Integrationer → Home Assistant*: kategori och ärende, *Alltid privat här*, företagsnamn,
  *Stämmer*/*Fel – privat* på antaget kundbesök, *Senare*. Nästa fråga skickas till telefonen.
- Godkänna och ändra resor (kategori, ärende, *Ange starttid* och ta bort den), lägga till en
  manuell resa. Ta bort en importerad eller manuellt inlagd resa, en i taget (*Ta bort resan*)
  eller flera i körjournalen (*Ta bort*); resor från bilen ger beskedet att de inte kan tas bort. *Ångra* i kvittot och *Ångra ändring* i resans ändringshistorik (och under
  *Platser → AI-minne*) återställer resan; besluten syns i ändringshistoriken.
- *Stämmer*/*Fel* i månadens genomgång (`/m/month/<månad>`).
- *Bilen står hemma nu* i Kom igång (det enda steget som återstår).
- Platser: bekräfta, byta namn och typ, skapa ny, godkänna eller avvisa förslag (förslaget
  försvinner ur listan).
- Inställningar: byta läge under *Automatik* (Kom igång försvinner i *Jag godkänner allt själv*),
  vetotid, kontorstid och övriga inställningar, fordonets namn och registreringsnummer,
  tjänsteställen och mätarställningar.
- Integrationer: *Synka nu* (inga nya resor sedan sist), *Testa* och *Skicka testnotis* (notisen
  syns i demotelefonen).
- Export: förhandsvisningen och kontrollerna räknas om ur resorna för vilken månad eller period
  som helst (i oktober visar en rad *Adress saknas*); *Skapa underlaget nu* på november prövar
  samma kontroller som månadsskiftet (öppna frågor och vetotid) och skapar annars underlaget,
  märkt *innan månaden var slut*; *Skapa Excel* och *Hämta underlaget* ger exempelfilen; *Stäng alla nedladdningslänkar*.
- Import: förhandsgranskningen visar ett inspelat exempel oavsett vilken fil som väljs.
- Inställningar → Sammanslagning: gränser för tjänsteresor och privata resor sparas.
- Inställningar → Förslag och AI: underlagsnivån (Fullständigt är valt) och *Om mitt arbete*
  sparas; Lunchregeln: gränserna sparas och mönstret följer med; Kalender: *Läs hela bokningen*
  är påslagen och arbetsgivarens e-postdomän (exempelakeri.se) är ifylld av "DriveLog";
  Tjänsteställe: *Resor mellan hemmet och våra kontor …* är påslaget.
- AI-genomgången: *Gå igenom väntande med AI* i Granskning, *Gå igenom med AI* i körjournalens
  massfält och *Bedöm igen* på resan. Demot har ingen AI-leverantör, så genomgången säger att bara
  reglerna används och varje resa blir *Oförändrad*.
- *Ärendet stämmer inte* på resan Hemmet → Exempel Åkeri AB den 3 november, vars ärende kom ur en
  (påhittad) kalenderbokning när resan godkändes av en regel – på resans sida, i månadskortet
  (*Ärende ur kalendern: 1 resa*) och i månadens genomgång.
- *Ladda ner felsökningsfil* under *Integrationer → Volvo* ger en liten påhittad fil med demobilens
  resor och avläsningar (samma form som serverns fil, utan chassinummer, registreringsnummer,
  positioner och adresser).
- *Företagslogga i underlaget* under *Inställningar → Milersättning och export*: en logga kan
  laddas upp, visas, bytas och tas bort, men den sparas inte – den finns bara tills sidan laddas
  om, och exempelfilen får ingen logga.
- *Notis när DriveLog har uppdaterats* under *Inställningar → Notiser* går att slå av och på.
- Längst ned i sidomenyn står *DriveLog demo*; den leder till *Inställningar → Om DriveLog*, som i
  demot inte har något versionsnummer eller ändringslogg.

Utöver inspelningen lägger demot till det som 1.3.0 visar: tre importerade resor i början av
oktober (Exempel Snickeri AB och Exempel Lager AB) med adresser men utan positioner – kartan
visar *Positionen slås upp*, och Exempel Lager AB saknar adress i underlaget – samt ett uppehåll
i hämtningen från Volvo på resan till Ekvägen 14 den 14 november. Omgången AI lägger till de nya
inställningarna (underlagsnivå, lunchregeln, hela bokningen, hem ↔ kontor) och ärendet ur
kalendern med en påhittad bokning på resan den 3 november. Omgången drift ger de tolv senaste
resorna från bilen en uppskattad bensinförbrukning (≈), som servern räknar fram ur bilens
avläsningar, och slår på valen för uppdateringsnotisen och loggan. De importerade resorna har de
tre sista av de 40 reserverade id:na, så att deras sidor är förbyggda.

Följderna syns som i appen: listor, granskningens räknare, översikten (sträckor, ersättning, att
granska, diagrammet) och månadskortets *N resor väntar på ditt svar*. Allt sparas i webbläsarens
localStorage; *Återställ demot* börjar om.

## Vad som inte simuleras

Det här svarar **Det här går inte att prova i demot**: att ansluta, koppla bort eller byta
demoläge för Volvo, Outlook, Home Assistant och AI (därför finns ingen AI-leverantör i demot och
AI-genomgången kör bara reglerna), importens *Bekräfta*, säkerhetskopior,
tokens, lösenord, radera historik, att lägga till, ändra eller ta bort tankning och laddning,
sammanslagning och delning av resor, egna regler och mönster under Platser och andra ändringar
som inte står ovan. Statistik som inte räknas om (resvägar, energi, månadsjämförelsen, *Veckan i
korthet*) visar de inspelade siffrorna; destinationerna följer platsernas namn och resornas
kategori. Alla Excel-filer är samma exempelfil (oktober), och importens förhandsgranskning är
samma inspelade exempel oavsett fil. Sådant som backenden gör av sig själv efter ett svar (till
exempel att godkänna hemresan, skapa *Oktober uppdaterad*, slå upp adresser eller kontrollera en
månad igen vid månadsskiftet) simuleras inte. Importens *Bekräfta* går inte att prova, så att en
borttagen resa kommer tillbaka vid ny import syns inte i demot. *Logga ut* leder
till den vanliga inloggningssidan, där vilka uppgifter som helst släpper in.

Kalenderlänken (ICS) i Outlook-panelen visas alltid i läget *ingen länk*. *Spara* svarar
*Kalenderlänken går inte att prova i demot – den hämtar en riktig kalender.*, eftersom demot
aldrig hämtar någon kalender.

## Göra om startdatat

Startdatat skapas med backendens egna vägar mot en tom SQLite-databas: en simulerad bil (som
`FakeCar` i testerna) pollas genom insamlaren dag för dag i sex veckor (klockan styrs med
`time_machine`), adresser slås upp mot en låtsad karttjänst, schemaläggarens tick fattar besluten
och ställer frågorna, och ägaren svarar via API:t. Oktober stängs av sig själv. Därefter körs
den riktiga webbappen mot databasen och alla GET-svar sidorna gör spelas in med Playwright och
görs om till `startdata.json`. Hjälpskripten ligger utanför repot; alla namn, företag, adresser
och registreringsnummer är påhittade.
