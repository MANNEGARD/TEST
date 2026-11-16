# SMGD Hem – Home Assistant på LG-TV:n (webOS)

En webOS-app som styr Home Assistant med TV:ns fjärrkontroll. Den är en vanlig webbapp
(HTML, CSS och JavaScript utan byggsteg), så samma kod går att köra på tre sätt:

| Sätt | Passar när |
| --- | --- |
| **Paketerad webOS-app** (`.ipk`) | Appen ska ligga i TV:ns appmeny och starta direkt |
| **Serverad från Home Assistant** (`/local/`) | Du vill slippa paketera: öppna adressen i TV:ns webbläsare eller i valfri webbläsare |
| **Vilken webbserver som helst** | Du vill prova i datorn eller mobilen (mus, tangentbord och pekskärm fungerar också) |

## Det här kan appen

- **Rum**: enheterna grupperade efter områdena i Home Assistant, plus flikar för lampor,
  reglage, klimat, media, säkerhet, kameror, scener, sensorer och mer.
- **Favoriter**: markera ett kort och tryck på den gula knappen. Favoriterna visas först.
- **Styrning**: tänd och släck, ljusstyrka, färg och vitt ljus, termostat och läge,
  persienner, lås, larm (utan kod), dammsugare, mediaspelare med volym och källa,
  scener, skript, knappar, val- och sifferreglage.
- **Kameror** med stillbild som uppdateras, och direktvisning i detaljvyn.
- **Väder** i hörnet och en prognos i detaljvyn.
- **Live**: allt uppdateras direkt via Home Assistants WebSocket-API. Appen ansluter
  automatiskt igen om förbindelsen bryts eller när TV:n vaknar.
- Dolda, avstängda och diagnostik- och konfigurationsentiteter visas inte.

## Fjärrkontrollen

| Knapp | Gör |
| --- | --- |
| Pilar | Flytta markeringen |
| **OK** | Slå på eller av, eller kör en scen eller ett skript. Andra kort öppnar detaljer |
| **Håll OK** | Öppna detaljer för kortet |
| **Röd** | Öppna detaljer |
| **Grön** | Inställningar (server, uppdatera, logga ut) |
| **Gul** | Lägg till eller ta bort favorit |
| **Blå** | Läs in allt på nytt |
| ▶ ⏸ ⏩ ⏪ | Styr mediaspelaren som är markerad |
| **Bakåt** | Stäng panelen, gå till flikraden, avsluta appen |

Med Magic Remote, mus eller pekskärm: klicka för huvudfunktionen, håll in eller högerklicka
för detaljer. På tangentbordet fungerar Esc, F (favorit), R (uppdatera), S (inställningar)
och I (detaljer).

## Logga in

Appen frågar efter adressen till Home Assistant, till exempel `http://192.168.1.10:8123`,
och sedan något av följande:

- **Användarnamn och lösenord**, med samma inloggning som Home Assistants egen. Den
  fungerar alltid när appen serveras från Home Assistant, eftersom webbläsaren då ser samma
  ursprung. Tvåstegsverifiering stöds. Appen sparar bara en förnyelsebar inloggning, inte
  lösenordet, och återkallar den vid utloggning.
- **Långlivad åtkomsttoken**: skapas i Home Assistant under *Profil → Säkerhet →
  Långlivade åtkomsttoken*. Det är det säkraste valet för den paketerade appen, eftersom
  WebSocket-anslutningen inte omfattas av webbläsarens ursprungsregler.

Om lösenordsinloggningen i den paketerade appen svarar *Kunde inte nå Home Assistant*,
använd en token i stället, eller tillåt appens ursprung i `configuration.yaml`:

```yaml
http:
  cors_allowed_origins:
    - "null"   # webOS-appar som körs från fil skickar ursprunget "null"
```

Det kan vara jobbigt att skriva en lång token med fjärrkontrollen. Lägg den då i
`config.json` innan du paketerar (se nedan). Filen är undantagen i `.gitignore`.

## 1. Paketera och installera på TV:n

Det här behövs en gång:

1. Installera **Developer Mode** från LG Content Store på TV:n, logga in med ett
   LG-utvecklarkonto och slå på *Dev Mode Status* och *Key Server*.
2. Installera webOS CLI på datorn:

   ```bash
   npm install -g @webos-tools/cli
   ```

3. Lägg till TV:n. Använd IP-adressen och lösenfrasen som Developer Mode-appen visar:

   ```bash
   ares-setup-device --add tv --info "{'host':'192.168.1.20','port':'9922','username':'prisoner'}"
   ```

   ```bash
   ares-novacom --device tv --getkey
   ```

Bygg och installera:

```bash
cd webos
./package.sh tv
```

Vill du baka in adress och token så att appen startar inloggad:

```bash
cp config.example.json config.json
```

Fyll i `hassUrl` och `token` och kör `./package.sh tv` igen. Den som har ipk-filen kan
läsa token, så dela inte en sådan fil.

> Developer Mode-sessionen på TV:n löper ut efter ett tag och måste förlängas i
> Developer Mode-appen. Annars försvinner utvecklarappar från TV:n.

## 2. Servera från Home Assistant (ingen paketering)

Kopiera innehållet i `webos/` till `config/www/tv/` på Home Assistant-maskinen, till exempel
med tillägget *Samba share* eller *File editor*. Öppna sedan:

```
http://<home-assistant>:8123/local/tv/index.html
```

Adressen fylls i automatiskt och lösenordsinloggningen fungerar direkt. Filer under
`/local/` är publika, så lägg aldrig `config.json` med token där.

## 3. Prova i datorn

```bash
cd webos
python3 -m http.server 8080
```

Öppna `http://localhost:8080` och logga in med en token, eller med lösenord om
`cors_allowed_origins` innehåller `http://localhost:8080`.

## Filer

| Fil | Innehåll |
| --- | --- |
| `appinfo.json` | webOS-manifest (app-id `se.smgd.hem`, 1920×1080, Bakåt-knappen hanteras av appen) |
| `index.html`, `css/app.css` | Gränssnittet, byggt för TV-avstånd med tydlig fokusmarkering |
| `js/ha.js` | Inloggning (lösenord med tvåstegsverifiering, token, förnyelse) och WebSocket-klient |
| `js/nav.js` | Rumslig navigering med piltangenterna |
| `js/app.js` | Flikar, kort, detaljpaneler och tangenthantering |
| `js/icons.js` | Egna ikoner, så att inget behöver hämtas från internet |
| `package.sh` | Bygger `.ipk` och installerar på TV:n med ares-cli |
| `smgd.project.json` | Byggbeskrivning för SMGD Developer Lab (webOS-webbapp, inget byggsteg) |

Koden är skriven utan `async/await` och `?.`, så den fungerar på webOS 4 (2018) och nyare.
