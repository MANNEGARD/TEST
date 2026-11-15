# Installation och drift

DriveLog körs som en uppsättning containrar med Docker Compose: PostgreSQL, Redis, API,
bakgrundsarbetare (worker), schemaläggare (beat), webbapp och en omvänd proxy (Caddy) som
sköter HTTPS.

> **Två sätt att köra DriveLog.** Den här guiden gäller en egen server med Docker Compose.
> Har du Home Assistant OS eller Supervised kan hela DriveLog i stället köras som ett
> Home Assistant-tillägg – utan separat server, med SQLite, automatisk HTTPS och valfri
> STRATO DynDNS. Se [home-assistant-tillagg.md](home-assistant-tillagg.md).

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
för inloggningsspärren och ändringsloggen. Sätt gärna `FORWARDED_ALLOW_IPS` till proxyns
adress i `.env` (standard är `*`, vilket förutsätter att API:t bara nås via proxyn, som i den
medföljande Compose-filen där port 8000 inte publiceras).

## Utvecklingsläge utan Docker

```bash
scripts/dev.sh
```

Startar API:t på port 8000 med SQLite och webbappen på port 3000. Bakgrundsjobb körs då
direkt i API-processen (`DRIVELOG_JOBS_EAGER=true`); schemalagd insamling kräver worker och
beat.
