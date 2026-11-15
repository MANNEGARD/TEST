# Installation och drift

DriveLog körs som en uppsättning containrar med Docker Compose: PostgreSQL, Redis, API,
bakgrundsarbetare (worker), schemaläggare (beat), webbapp och en omvänd proxy (Caddy) som
sköter HTTPS.

## Krav

- En server med Docker Engine 24+ och Docker Compose v2 (cirka 2 GB RAM räcker).
- Ett domännamn som pekar på servern, till exempel `journal.smgd.se`, och port 80/443
  öppna – Caddy hämtar då certifikat automatiskt från Let's Encrypt. För ett rent lokalt
  nät fungerar `localhost` (lokalt certifikat) eller en egen proxy framför.
- För integrationerna: egna konton hos Volvo Cars Developer Portal, Microsoft Entra
  (appregistrering) och eventuellt en AI-leverantör. Home Assistant behöver Companion-appen
  på telefonen. Se respektive guide.

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
| `DRIVELOG_DOMAIN` | Domänen Caddy ska servera, t.ex. `journal.smgd.se` |
| `DRIVELOG_BASE_URL` | Publik adress, t.ex. `https://journal.smgd.se`. Används i OAuth-redirects och notislänkar – måste stämma exakt med det du registrerar hos Volvo och Microsoft |
| `POSTGRES_PASSWORD` | Databaslösenord: `openssl rand -base64 32` |
| `DRIVELOG_SECRET_KEY` | Applikationshemlighet, minst 32 tecken: `openssl rand -hex 32` |
| `DRIVELOG_ENCRYPTION_KEYS` | Krypteringsnyckel för lagrade tokens och API-nycklar, se nedan |
| `DRIVELOG_TIMEZONE` | Standard `Europe/Stockholm` |

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
curl -fsS https://journal.smgd.se/api/v1/health/ready
```

## 3. Första kontot

Öppna webbadressen. Första gången visas *Skapa konto*; det kontot blir administratör.
Därefter går det inte att skapa konton utan inloggning. (Alternativt kan
`DRIVELOG_FIRST_ADMIN_EMAIL` och `DRIVELOG_FIRST_ADMIN_PASSWORD` anges i `.env`.)

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
- **API-dokumentation:** `https://<domän>/api/v1/docs`.
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
