# SMGD Transkribering

Självhostad webbtjänst för att transkribera svenska tal till text, med talaruppdelning.
Den körs på **transcribe.smgd.se**, bredvid DriveLog på samma VPS.

- **Live-utkast:** medan du spelar in visas texten löpande, uppdelad per talare.
- **Exakt slutversion:** när du trycker *Stoppa* transkriberas hela inspelningen på nytt.
  Texten kommer från Whisper large-v3, och talarna tas från en talaruppdelning av hela ljudet.
  Whisper-texten kopplas sedan till talarna ord för ord.
- **Ljudkällor:** datorns mikrofon, eller ljudet från en flik (Teams, Meet, Zoom i
  webbläsaren) blandat med mikrofonen.
- **Spara och sortera:** alla transkriberingar sparas med ljud. Du kan ordna dem i mappar,
  sätta taggar, söka i fritext och sortera på datum, titel eller längd.
- **Redigera:** du kan döpa talare ("Talare 1" → "Anna"), rätta text, byta talare på ett
  stycke och klicka på en tid för att spela upp ljudet därifrån.
- **Export:** textfil (.txt), med eller utan tidsstämplar och talarnamn.
- **Konton:** flera användare med egna konton. Nya konton skapas bara via inbjudningslänk
  från en administratör.

## Varför molntjänster (och varför de är gratis)

Servern (STRATO VPS M: 2 kärnor, 4 GB RAM) delas med DriveLog och klarar inte att köra
Whisper large med god hastighet. Det tunga arbetet görs därför av två tjänster med gratiskvoter:

| Steg | Tjänst | Gratis |
| --- | --- | --- |
| Live-text + talare i realtid, talaruppdelning av hela inspelningen | Deepgram nova-3 (svenska) | 200 USD startkredit, ungefär 200 timmar svenska |
| Exakt text i slutversionen | Whisper large-v3 hos Groq | Ungefär 2 timmar ljud per timme och 8 timmar per dygn |

Når Groq gränsen väntar tjänsten automatiskt och fortsätter sedan. Status visas på sidan.
Under **Administration → Förbrukning** ser du hur mycket som använts per månad.

Ljudet skickas alltså till Deepgram och Groq för transkribering. Vill du hålla allt
privat senare, kan du köra KB-Whisper (KBLabs svenska Whisper, ännu bättre på svenska) på en
egen dator med grafikkort. Tjänsten pratar samma standard (OpenAI-kompatibelt API), så det
räcker att ändra `WHISPER_API_URL` och `WHISPER_MODEL`.

Servern behöver ungefär 100 MB RAM. ffmpeg körs bara kort efter varje inspelning.

## Installation på VPS:en (bredvid DriveLog)

### 1. Skaffa nycklar (gratis)

1. **Deepgram:** skapa ett konto på <https://console.deepgram.com>, gå till *API Keys* och välj
   *Create a New API Key*.
2. **Groq:** skapa ett konto på <https://console.groq.com> och skapa en nyckel under *API Keys*.

### 2. DNS

Skapa en A-post för `transcribe.smgd.se` som pekar på VPS:ens IP-adress, samma adress som
`drivelog.smgd.se`.

### 3. DriveLog skickar trafiken vidare

DriveLog äger port 80/443 på servern. Från version 1.4.0 kan DriveLogs Caddy hämta certifikat
även för `transcribe.smgd.se` och skicka trafiken vidare till transkriberingen. Kör samma rad
som när DriveLog installerades, med tillägget sist:

```bash
curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/deploy/server.sh | sudo bash -s -- drivelog.smgd.se --extra-doman transcribe.smgd.se=transcribe:8080
```

Skriptet skapar Docker-nätverket `smgd` och startar om DriveLog på det.

### 4. Starta transkriberingen

```bash
sudo mkdir -p /opt/transcribe && cd /opt/transcribe
```

```bash
sudo curl -fsSLO https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/transcribe/docker-compose.yml
```

```bash
sudo curl -fsSL -o .env https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/transcribe/.env.example
```

Öppna `.env` och klistra in nycklarna vid `DEEPGRAM_API_KEY=` och `WHISPER_API_KEY=`:

```bash
sudo nano .env
```

```bash
sudo docker network create smgd 2>/dev/null; sudo docker compose pull && sudo docker compose up -d
```

### 5. Skapa ditt administratörskonto

Vid första starten skriver tjänsten ut en inbjudningslänk:

```bash
sudo docker compose logs transcribe | grep inbjudan
```

Öppna länken och skapa kontot. Bjud sedan in andra under **Administration**.

### Uppdatera

```bash
cd /opt/transcribe && sudo docker compose pull && sudo docker compose up -d
```

### Utan DriveLog på servern

Då behövs en egen Caddy på port 80/443. Ladda också ner `Caddyfile` och
`docker-compose.fristaende.yml`, sätt `TRANSCRIBE_DOMAIN` i `.env` och starta:

```bash
sudo docker compose -f docker-compose.yml -f docker-compose.fristaende.yml up -d
```

## Drift

- **Data:** allt ligger i Docker-volymen `transcribe_transcribe-data`. Det är databasen
  (`transcribe.db`) och ljudet (`audio/<id>/audio.m4a`, cirka 30 MB per timme).
- **Säkerhetskopia:**

  ```bash
  sudo docker run --rm -v transcribe_transcribe-data:/data -v "$PWD":/backup alpine tar czf /backup/transcribe-backup.tgz -C /data .
  ```

- **Glömt lösenord:**

  ```bash
  sudo docker compose exec transcribe node dist/cli.js nytt-losenord din@epost.se
  ```

- **Ny administratörsinbjudan från terminalen:**

  ```bash
  sudo docker compose exec transcribe node dist/cli.js inbjudan admin
  ```

## Tips för bästa resultat

- Fyll i **namn och facktermer** när du startar en inspelning, till exempel
  "Anna Lindqvist, Kvarnholmen, EBITDA". Det ger rätt stavning i slutversionen. Fältet kan
  ändras i efterhand, och sedan väljer du *Kör om*.
- En bra mikrofon nära talarna ger mest. Vid Teams-möten i webbläsaren: välj *Flik + mikrofon*,
  välj mötesfliken och kryssa i *Dela även flikens ljud*. Det fungerar i Chrome och Edge på dator.
- Bryts nätet under inspelningen fortsätter ljudet att spelas in i webbläsaren och skickas
  när anslutningen är tillbaka. Stängs fliken kan inspelningen fortsätta eller avslutas
  från transkriberingens sida.
- Skärmen hålls vaken under inspelningen. På mobilen behöver sidan ändå vara öppen i förgrunden.

## Utveckling

```bash
npm ci && npm test
```

Servern startas med `npm run dev` (`TRANSCRIBE_DATA_DIR=./data`, port 8080). Utan nycklar
fungerar allt utom själva transkriberingen.

| Del | Fil |
| --- | --- |
| HTTP-server, säkerhetshuvuden | `src/server.ts` |
| REST-API | `src/api.ts` |
| Live-inspelning (WebSocket → WAV + Deepgram) | `src/live.ts` |
| Slutversion (kö, ffmpeg, Whisper, talare) | `src/jobs.ts` |
| Sammanslagning, filtrering av Whisper-påhitt, export | `src/merge.ts` |
| Webbapp (inga byggsteg) | `web/` |
