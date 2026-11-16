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
  manuell resa. *Ångra* i kvittot och *Ångra ändring* i resans ändringshistorik (och under
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
  som helst; *Skapa Excel* och *Hämta underlaget* ger exempelfilen; *Stäng alla nedladdningslänkar*.
- Import: förhandsgranskningen visar ett inspelat exempel oavsett vilken fil som väljs.

Följderna syns som i appen: listor, granskningens räknare, översikten (sträckor, ersättning, att
granska, diagrammet) och månadskortets *N resor väntar på ditt svar*. Allt sparas i webbläsarens
localStorage; *Återställ demot* börjar om.

## Vad som inte simuleras

Det här svarar **Det här går inte att prova i demot**: att ansluta, koppla bort eller byta
demoläge för Volvo, Outlook, Home Assistant och AI, importens *Bekräfta*, säkerhetskopior,
tokens, lösenord, radera historik, att lägga till, ändra eller ta bort tankning och laddning,
sammanslagning och delning av resor, egna regler och mönster under Platser och andra ändringar
som inte står ovan. Statistik som inte räknas om (resvägar, energi, månadsjämförelsen, *Veckan i
korthet*) visar de inspelade siffrorna; destinationerna följer platsernas namn och resornas
kategori. Alla Excel-filer är samma exempelfil (oktober), och importens förhandsgranskning är
samma inspelade exempel oavsett fil. Sådant som backenden gör av sig själv efter ett svar (till
exempel att godkänna hemresan eller skapa *Oktober uppdaterad*) simuleras inte. *Logga ut* leder
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
