# SMGD DriveLog

Intelligent, självhostad körjournal för Volvo – byggd för att redovisa tjänsteresor med egen
bil till arbetsgivaren (milersättning). DriveLog samlar fordonsdata via Volvos officiella
API, importerar körjournaler från Volvo Cars-appen, matchar resor mot Outlook-kalendern,
föreslår kategori och syfte med en hybrid av regler och AI, frågar via interaktiva notiser i
Home Assistant när något är oklart – och skapar ett enkelt Excel-underlag med just de
uppgifter arbetsgivaren och Skatteverket behöver.

Endast tjänsteresor redovisas. Privat körning – och därmed luckor i mätarställningen mellan
resorna – är förväntad och behandlas aldrig som fel.

Applikationen är fristående. Home Assistant, Outlook, Volvo och AI-leverantör är
integrationer som kan slås på var för sig; allt fungerar även utan dem.

> **Grundprincip:** körjournalens korrekthet och spårbarhet går före automation.
> Faktiska, uppskattade och saknade uppgifter hålls alltid isär, originaldata bevaras,
> och varje beslut kan korrigeras och ångras.

## Innehåll

| Del | Plats | Teknik |
| --- | --- | --- |
| Backend (API, bakgrundsjobb) | [`backend/`](backend/) | Python 3.13, FastAPI, SQLAlchemy 2, Alembic, Celery, PostgreSQL, Redis |
| Webbapp (PWA) | [`frontend/`](frontend/) | Next.js, TypeScript, Tailwind CSS, Recharts, MapLibre GL |
| Home Assistant-integration | [`custom_components/smgd_drivelog/`](custom_components/smgd_drivelog/) | Custom integration, installeras via HACS |
| Drift | [`docker-compose.yml`](docker-compose.yml), [`deploy/`](deploy/) | Docker Compose, Caddy (HTTPS), backup-skript |
| Dokumentation | [`docs/`](docs/) | Svenska guider |

## Snabbstart

```bash
cp .env.example .env
```

Fyll i `POSTGRES_PASSWORD`, `DRIVELOG_SECRET_KEY`, `DRIVELOG_ENCRYPTION_KEYS`, `DRIVELOG_DOMAIN`
och `DRIVELOG_BASE_URL` (se kommentarerna i filen), och starta sedan:

```bash
docker compose up -d --build
```

Öppna `https://<din-domän>` och skapa det första kontot. Fullständig anvisning finns i
[docs/installation.md](docs/installation.md).

## Dokumentation

| Guide | Innehåll |
| --- | --- |
| [Installation och drift](docs/installation.md) | Krav, miljövariabler, start, uppgradering, felsökning |
| [Användning](docs/anvandning.md) | Import, körjournal, granskning, platser, export – steg för steg |
| [Arkitektur och tekniska beslut](docs/arkitektur.md) | Datamodell, resenivåer, sammanslagning, klassificering, inlärning |
| [Analys av historiska underlag](docs/analys-historik.md) | Vad importmotorn är byggd utifrån |
| [Volvo API](docs/volvo-api.md) | Utvecklarkonto, anslutning, vad API:t kan och inte kan |
| [Microsoft Outlook](docs/microsoft-graph.md) | Appregistrering, behörigheter, matchning |
| [Home Assistant](docs/home-assistant.md) | Installation via HACS, notiser, push/pull-läge |
| [AI-klassificering](docs/ai-klassificering.md) | Regler, AI-lager, trösklar, sekretess |
| [Excel-export](docs/excel-export.md) | Det enkla underlaget för milersättning, och den valfria detaljerade exporten |
| [Säkerhet och sekretess](docs/sakerhet.md) | Autentisering, kryptering, vad som skickas vart |
| [Säkerhetskopiering](docs/backup.md) | Backup, återställning, nyckelhantering |

## Utveckling

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Testerna körs mot SQLite i minnet. Sätt `DRIVELOG_TEST_DATABASE_URL` för att köra samma svit
mot PostgreSQL. `scripts/dev.sh` startar backend och webbapp lokalt, och `scripts/e2e.sh` kör
webbappens helhetstest (Playwright) mot en ny databas.

API:t är dokumenterat med OpenAPI på `/api/v1/docs` (i utvecklingsläge; i produktion är sidan
stängd om inte `DRIVELOG_PUBLIC_DOCS=true`).

## Status för integrationerna

Integrationskoden, anslutningsflödena och testerna är färdiga. Verifiering mot de riktiga
tjänsterna kräver dina egna konton och nycklar – se respektive guide för exakt vad som
behövs och vad som ännu inte kunnat prövas mot verklig tjänst. En integration visas aldrig
som ansluten förrän en riktig anslutning har lyckats; utan nycklar finns ett tydligt märkt
demoläge med ett separat demofordon.
