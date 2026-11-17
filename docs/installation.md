# Installation och drift

DriveLog körs som en uppsättning containrar med Docker Compose: PostgreSQL, Redis, API,
bakgrundsarbetare (worker), schemaläggare (beat), webbapp och en omvänd proxy (Caddy) som
sköter HTTPS.

> **Två sätt att köra DriveLog.** Den här guiden gäller en egen server med Docker Compose.
> Har du Home Assistant OS kan hela DriveLog i stället köras som en app i Home Assistant
> (före 2026.2 kallat tillägg) – utan separat server, med SQLite, automatisk HTTPS och valfri
> STRATO DynDNS. Se [home-assistant-tillagg.md](home-assistant-tillagg.md).

## Enklast: en rad på en hyrd server

Har du ingen publik adress hemma (t.ex. Starlink eller mobilt bredband, där operatören delar
adress mellan kunder) är en liten hyrd server (VPS) det enklaste sättet att få DriveLog på din
egen domän. Du behöver en Linux-server med Ubuntu och minst 1 GB minne.

1. Peka domänens **A-post** på serverns adress (för `smgd.se`: hos STRATO) och se till att det
   inte finns någon **AAAA-post** som pekar någon annanstans.
2. Logga in på servern och klistra in:

```bash
curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/deploy/server.sh | sudo bash -s -- drivelog.smgd.se
```

Skriptet installerar Docker om det saknas, kontrollerar att domänen pekar på servern, frågar
efter e-post och lösenord för ditt konto, startar den färdiga avbilden (samma som Home
Assistant-appen: API, webbapp och proxy i en container, med SQLite) och väntar tills
`https://drivelog.smgd.se` svarar med giltigt certifikat. Lösenordet sparas inte i klartext.

- **Uppdatera:** kör samma rad igen. Allt ditt ligger kvar i `/opt/drivelog/data`. Servern kan
  också göra det själv, se [Automatisk uppdatering](#automatisk-uppdatering).
- **Glömt lösenordet:** lägg till `--nytt-losenord` sist på raden.
- **Säkerhetskopia:** ta med `/opt/drivelog/data` i serverns säkerhetskopior (databasen,
  krypteringsnyckeln och certifikaten ligger där).
- **Loggen:** `docker logs --tail 50 drivelog`.

### Automatisk uppdatering

Vill du slippa köra raden själv varje gång en ny version kommer kan servern sköta det. Lägg
till `--automatisk-uppdatering` sist på samma rad (en gång räcker):

```bash
curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/deploy/server.sh | sudo bash -s -- drivelog.smgd.se --automatisk-uppdatering
```

Så här fungerar det:

- Ungefär var 15:e minut kontrollerar servern vilken version som erbjuds. Det är samma version
  som raden ovan installerar, och den erbjuds först när den är färdigbyggd och provstartad.
  Bara en **nyare** version installeras. Är det samma version händer ingenting, och DriveLog
  startas inte om.
- Före varje uppdatering stoppas DriveLog en kort stund och hela `/opt/drivelog/data` kopieras
  till `/opt/drivelog/kopior` (databasen, nycklarna, certifikaten och inställningarna). De tre
  senaste kopiorna sparas. Sedan startas den nya versionen, och servern kontrollerar att den
  svarar och att det verkligen är den nya versionen som kör.
- **Om den nya versionen inte startar** går servern själv tillbaka: den nya stoppas, datat
  läggs tillbaka från kopian och den förra versionen startas igen. Den misslyckade versionen
  prövas inte om och om igen, bara när en ännu nyare version kommer eller när du kör raden
  själv. Du behöver inte göra något, men titta gärna på läget (se nedan).
- **Om uppdateringen avbryts** (servern startas om eller strömmen går mitt i) gör servern
  klart vid nästa kontroll, eller när du kör raden själv: den förra versionen startas igen
  med datat från före försöket. Avbryts samma version två gånger prövas den inte igen förrän
  en nyare kommer. Saknas ditt data någon gång men säkerhetskopior finns, skapar raden inget
  nytt tomt konto. Den talar i stället om vilken kopia du ska lägga tillbaka.
- Servern kör en egen kopia av skriptet (`/opt/drivelog/server.sh`). Från internet hämtas bara
  versionsnumret och själva DriveLog-avbilden. Kopian förnyas när du kör raden själv.
- Du kan fortfarande köra raden för hand när du vill. Pågår en automatisk uppdatering just då
  väntar raden tills den är klar.

**Se läget** (automatik på eller av, vilken version som kör och vilken som erbjuds, senaste
kontroll, senaste uppdatering och senaste fel):

```bash
sudo /opt/drivelog/server.sh --lage
```

Samma sak fungerar alltid med raden ovan om du byter `drivelog.smgd.se --automatisk-uppdatering`
mot `--lage`. Hela loggen finns i `/opt/drivelog/uppdatering.log` (och med
`journalctl -t drivelog-uppdatering`).

**Slå av** automatiken (DriveLog fortsätter att köra som vanligt):

```bash
curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/deploy/server.sh | sudo bash -s -- --utan-automatisk-uppdatering
```

Säkerhetskopiorna i `/opt/drivelog/kopior` ligger på samma disk som DriveLog. De skyddar mot en
misslyckad uppdatering, inte mot att servern går sönder, så ta fortfarande med `/opt/drivelog`
i serverns vanliga säkerhetskopior.

### Extra domäner (fler tjänster på samma server)

DriveLog äger port 80 och 443 på servern. Ska en annan tjänst på samma server också nås med
HTTPS på en egen domän – t.ex. `transcribe.smgd.se` – låter du DriveLogs proxy (Caddy) hämta
certifikatet och skicka trafiken vidare till tjänstens container. Lägg till `--extra-doman`
sist på raden:

```bash
curl -fsSL https://raw.githubusercontent.com/SMGD-MP/TEST/HEAD/deploy/server.sh | sudo bash -s -- drivelog.smgd.se --extra-doman transcribe.smgd.se=transcribe:8080
```

- Formen är `<domän>=<värd>:<port>`: domänen som ska nås utifrån, och namnet på den andra
  tjänstens container och porten den lyssnar på (vanlig HTTP; WebSockets fungerar). Flera
  domäner: upprepa valet eller skriv dem med komma emellan. `localhost`, `127.0.0.1` och andra
  IP-adresser går inte som värd: inne i DriveLogs container är `127.0.0.1` DriveLog självt.
- **Domänen måste peka på servern**, precis som DriveLogs egen (A-post, och ingen AAAA-post som
  pekar någon annanstans) – annars kan certifikatet inte hämtas.
- Valet sparas i `/opt/drivelog/data/options.json` och gäller tills du ändrar det, även när
  raden körs igen utan valet och vid [automatisk uppdatering](#automatisk-uppdatering). Anger
  du `--extra-doman` igen ersätts listan. Ta bort alla med `--utan-extra-doman`.
- DriveLogs container ansluts till Docker-nätet **`smgd`** (skriptet skapar det om det saknas).
  Den andra tjänsten måste vara med i samma nät, som ett *externt* nät, för att DriveLog ska nå
  den på sitt namn. Den behöver inte publicera någon port på servern. I den andra tjänstens
  `docker-compose.yml`:

  ```yaml
  services:
    transcribe:
      container_name: transcribe
      networks: [smgd]
  networks:
    smgd:
      external: true
  ```

  Startas den andra tjänsten först: skapa nätet med `sudo docker network create smgd`.
- Den extra domänen får bara de allmänna säkerhetshuvudena (HSTS, `nosniff`, `Referrer-Policy`)
  – inte DriveLogs spärr för mikrofon och kamera eller förbudet mot inramning. Det bestämmer
  tjänsten själv.
- **Den extra tjänsten nås direkt från internet.** DriveLogs inloggning skyddar den inte, och
  den ligger på samma domän som DriveLog. Tjänsten måste själv ha inloggning och vara säker –
  lägg inte till en tjänst du inte litar på. Proxyn släpper igenom högst 50 MB per förfrågan.
- Ändrar du de extra domänerna (`--extra-doman` eller `--utan-extra-doman`) med automatisk
  uppdatering påslagen, förnyas först automatikens kopia av skriptet, så att den känner till
  dem. Går det inte avbryts raden innan något ändras – kör den igen om en stund.
- Kan skriptet inte koppla DriveLog till nätet `smgd` (eller inte läsa inställningsfilen) startar
  DriveLog ändå, med en varning; sammanfattningen säger då att den extra domänen inte når sin
  tjänst. Kör raden igen när felet är åtgärdat.

Resten av den här sidan beskriver den större uppsättningen med Docker Compose (PostgreSQL,
Redis och separata tjänster), som bygger avbilderna på servern.

## Krav

- En server med Docker Engine 24+ och Docker Compose v2. Driften klarar sig på 2 GB RAM, men
  första bygget behöver cirka 3 GB – lägg till 2 GB växlingsutrymme eller välj 4 GB.
- Ett domännamn som pekar på servern, till exempel `drivelog.smgd.se`, och port 80/443
  öppna – Caddy hämtar då certifikat automatiskt från Let's Encrypt. För ett rent lokalt
  nät fungerar `localhost` (lokalt certifikat) eller en egen proxy framför.
- För integrationerna: egna konton hos Volvo Cars Developer Portal, Microsoft Entra
  (appregistrering) och eventuellt en AI-leverantör. Home Assistant behöver Companion-appen
  på telefonen. Se respektive guide.

## Snabbaste vägen

På en server med Docker, när domänen (här `drivelog.smgd.se`) pekar på serverns adress:

```bash
git clone https://github.com/SMGD-MP/TEST.git drivelog
```

```bash
cd drivelog
```

```bash
deploy/install.sh drivelog.smgd.se
```

Skriptet skapar `.env` med slumpade hemligheter (befintliga värden skrivs aldrig över),
kontrollerar minne, DNS och att port 80/443 är lediga, bygger avbilderna, **skapar ditt
administratörskonto innan något öppnas mot internet**, startar allt och kontrollerar till sist
att HTTPS verkligen svarar med ett giltigt certifikat. Öppna sedan `https://drivelog.smgd.se`
och logga in. Kör samma kommando igen efter `git pull` när du uppgraderar.

- **DNS:** hos den som sköter domänens DNS (för `smgd.se`: STRATO) ska underdomänen
  `drivelog` ha en **A-post** som pekar på serverns publika IPv4-adress. Har underdomänen
  även en **AAAA-post** (IPv6) – STRATO lägger in en för sin parkeringssida – måste den tas
  bort eller pekas på serverns IPv6-adress. Annars kontrollerar Let's Encrypt fel server och
  certifikatet utfärdas aldrig. Skriptet varnar för båda fallen.
- **Minne:** första bygget av webbappen behöver cirka 3 GB. Har servern 2 GB lägger du till
  växlingsutrymme först (skriptet visar kommandot).
- **Privat repo:** `git clone` frågar efter inloggning. Använd ditt GitHub-användarnamn och en
  personlig åtkomsttoken med läsrätt till repot, eller ladda ned koden som zip från GitHub och
  packa upp den på servern.
- **Kontrollera installationen:** `deploy/smoke-test.sh https://drivelog.smgd.se` prövar att
  API:t och webbappen svarar över HTTPS, att säkerhetshuvudena finns och att inget går att
  läsa utan inloggning.

Resten av den här sidan beskriver samma steg för hand och vad som händer bakom.

## 1. Hämta koden och skapa `.env`

```bash
git clone https://github.com/SMGD-MP/TEST.git drivelog
```

```bash
cd drivelog
```

```bash
cp .env.example .env
```

Fyll i `.env`:

| Variabel | Beskrivning |
| --- | --- |
| `DRIVELOG_DOMAIN` | Domänen Caddy ska servera, t.ex. `drivelog.smgd.se` |
| `DRIVELOG_BASE_URL` | Publik adress, t.ex. `https://drivelog.smgd.se`. Används i OAuth-redirects och notislänkar – måste stämma exakt med det du registrerar hos Volvo och Microsoft |
| `POSTGRES_PASSWORD` | Databaslösenord: `openssl rand -hex 24` (endast bokstäver och siffror – lösenordet ingår i databasadressen och får inte innehålla `/`, `+` eller `@`) |
| `DRIVELOG_SECRET_KEY` | Applikationshemlighet, minst 32 tecken: `openssl rand -hex 32` |
| `DRIVELOG_ENCRYPTION_KEYS` | Krypteringsnyckel för lagrade tokens och API-nycklar, se nedan |
| `DRIVELOG_TIMEZONE` | Standardtidszon tills du väljer en egen under Inställningar (`Europe/Stockholm`) |

Skapa krypteringsnyckeln:

```bash
docker compose run --rm --no-deps --entrypoint python api -m app.cli generate-key
```

(Fyll i `POSTGRES_PASSWORD` först – Compose kräver den. Nyckeln kan också skapas utan Docker:
`python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.)

Klistra in värdet som `DRIVELOG_ENCRYPTION_KEYS`. **Spara nyckeln på ett säkert ställe
utanför servern.** Utan den går lagrade OAuth-tokens och API-nycklar inte att läsa efter en
återställning (resor och övrig data påverkas inte).

I produktionsläge vägrar API:t att starta om hemligheten är för kort, om krypteringsnyckel
saknas eller om `DRIVELOG_BASE_URL` inte använder https.

## 2. Starta

```bash
docker compose up -d --build
```

API-containern kör databasmigrationerna (`alembic upgrade head`) vid varje start. Kontrollera
att allt är uppe:

```bash
docker compose ps
```

```bash
curl -fsS https://drivelog.smgd.se/api/v1/health/ready
```

## 3. Första kontot

`deploy/install.sh` skapar kontot åt dig innan proxyn startas. Startar du för hand gäller
detta: så länge inget konto finns kan **vem som helst** som når adressen skapa det första
kontot, och det blir administratör. Skapa därför kontot innan systemet öppnas mot internet:

```bash
docker compose up -d db redis api
```

```bash
docker compose exec api python -m app.cli create-admin din@adress.se
```

```bash
docker compose up -d
```

(Alternativt kan `DRIVELOG_FIRST_ADMIN_EMAIL` och `DRIVELOG_FIRST_ADMIN_PASSWORD` anges i
`.env` före första starten.) När ett konto finns går det inte att skapa fler utan inloggning.

Glömt lösenordet? Återställ från servern:

```bash
docker compose exec api python -m app.cli create-admin din@adress.se
```

## 4. Kom igång

1. **Inställningar → Fordon och förare:** ange registreringsnummer, ditt namn och arbetsgivare
   – de hamnar i exportens sidhuvud.
2. **Inställningar → Milersättning och export:** kontrollera ersättningen per mil (25 kr som
   standard).
3. **Import:** ladda upp dina Volvo-exporter (se [Användning](anvandning.md)).
4. **Platser:** godkänn de föreslagna platstyperna (hem, kontor, kunder, hotell).
5. **Integrationer:** anslut Volvo, Outlook, Home Assistant och eventuell AI-leverantör.
6. **Export:** välj månad och skapa underlaget till arbetsgivaren.

## Installera som app på iPhone

Öppna webbadressen i Safari → Dela → *Lägg till på hemskärmen*. Appen öppnas då i helskärm.

## Tjänsterna

| Tjänst | Uppgift |
| --- | --- |
| `proxy` | Caddy: HTTPS, säkerhetshuvuden, routar `/api/v1/*` till API:t och resten till webbappen |
| `web` | Next.js-webbappen |
| `api` | REST-API (FastAPI) |
| `worker` | Bakgrundsjobb (Celery): fordonsinsamling, klassificering, notiser |
| `beat` | Schemaläggare: tick varje minut, kalendersynk var 15:e minut, nattligt underhåll |
| `db` | PostgreSQL 16 |
| `redis` | Kö för bakgrundsjobb och rate limiting |

Alla tjänster har `restart: unless-stopped` och health checks. Jobb kvitteras först när de
är klara, så ett jobb som avbryts av en omstart körs om.

## Uppgradering

```bash
git pull
```

```bash
docker compose up -d --build
```

Ta alltid en säkerhetskopia först, se [Säkerhetskopiering](backup.md).

## Loggar och felsökning

```bash
docker compose logs -f api worker
```

- **Integrationer:** sidan *Integrationer* visar status, senaste lyckade synk och senaste
  felmeddelande per integration.
- **API-dokumentation:** stängd i produktion. Sätt `DRIVELOG_PUBLIC_DOCS=true` i `.env` om du
  vill ha `https://<domän>/api/v1/docs`, eller ta fram beskrivningen med
  `docker compose exec api python -m app.cli openapi`.
- **Hälsa:** `/api/v1/health` (lever) och `/api/v1/health/ready` (databas och kö).
- Loggarna maskerar tokens, nycklar, lösenord och VIN.

## Köra bakom en egen proxy

Kör du redan Traefik/nginx kan `proxy`-tjänsten tas bort. Routa `/api/v1/*` till `api:8000`
och allt annat till `web:3000`, terminera TLS i din proxy och skicka `X-Forwarded-For` och
`X-Forwarded-Proto`.

DriveLog använder det sista värdet i `X-Forwarded-For` – det som din proxy själv lagt till –
för inloggningsspärren och ändringsloggen, men bara när anropet kommer från en adress i
`FORWARDED_ALLOW_IPS` (adresser eller nät, kommaseparerade). Övriga anrop räknas på sin egen
adress. Sätt gärna variabeln till proxyns adress i `.env` (standard är `*`, vilket förutsätter
att API:t bara nås via proxyn, som i den medföljande Compose-filen där port 8000 inte
publiceras).

## Utvecklingsläge utan Docker

```bash
scripts/dev.sh
```

Startar API:t på port 8000 med SQLite och webbappen på port 3000. Bakgrundsjobb körs då
direkt i API-processen (`DRIVELOG_JOBS_EAGER=true`); schemalagd insamling kräver worker och
beat.
