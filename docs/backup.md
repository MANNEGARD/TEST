# Säkerhetskopiering och återställning

Tre saker behöver skyddas:

1. **Databasen** – resor, platser, beslut, inställningar, krypterade tokens.
2. **Filer** – uppladdade originalexporter (`/data/imports`) och skapade Excel-filer (`/data/exports`).
3. **`.env`** – särskilt `DRIVELOG_ENCRYPTION_KEYS`. Utan nyckeln kan lagrade OAuth-tokens och
   API-nycklar inte dekrypteras efter en återställning. Förvara den separat från kopiorna.

## Rekommenderad rutin: `deploy/backup.sh`

Skriptet tar en `pg_dump` i custom-format, packar filkatalogerna, kontrollerar att
databaskopian går att läsa och gallrar kopior äldre än 30 dagar.

```bash
deploy/backup.sh
```

Schemalägg med cron på värden (exempel: varje natt 03:00):

```bash
0 3 * * * /srv/drivelog/deploy/backup.sh >> /var/log/drivelog-backup.log 2>&1
```

| Variabel | Standard | Betydelse |
| --- | --- | --- |
| `BACKUP_DIR` | `./backups` | Målkatalog |
| `BACKUP_KEEP_DAYS` | `30` | Antal dagar kopior sparas |

Kopiera katalogen vidare till en annan maskin eller krypterad molnlagring – en kopia på
samma disk skyddar inte mot diskhaveri. Kopiorna innehåller rörelsemönster och
kalenderuppgifter; behandla dem som känsliga.

## Återställning

```bash
deploy/restore.sh backups/drivelog-db-20261114T030000Z.dump backups/drivelog-files-20261114T030000Z.tar.gz
```

Skriptet ber om bekräftelse, stoppar applikationen, ersätter databasen, packar upp filerna
och startar om. Kontrollera efteråt:

1. Logga in och jämför antal resor och senaste resa med före.
2. *Integrationer*: är status "Ansluten" fungerar krypteringsnyckeln. Visas "Behöver anslutas
   på nytt" saknas rätt nyckel – anslut integrationerna igen.

**Öva återställningen** på en testmaskin minst en gång. En kopia som aldrig prövats är ingen
garanti.

## Säkerhetskopia från webbgränssnittet

*Inställningar → Säkerhetskopia* skapar en portabel zip-fil (en JSON-rad per databasrad plus
originalfilerna) som kan laddas ner. Den är praktisk före en uppgradering eller för flytt
mellan miljöer, och återställs från kommandoraden:

```bash
docker compose exec api python -m app.cli restore /data/backups/drivelog-backup-<tid>.zip
```

Kommandot vägrar skriva över en databas som innehåller data om inte `--force` anges, och
kontrollerar radantalen mot kopians manifest. Inloggningssessioner följer inte med.

## Nyckelrotation

`DRIVELOG_ENCRYPTION_KEYS` kan innehålla flera nycklar, kommaseparerade. Den första används
för ny kryptering, alla används för dekryptering. Lägg den nya nyckeln först, starta om, och
anslut om integrationerna (eller låt tokens förnyas) innan den gamla tas bort.

## Radera data

*Inställningar → Sekretess* kan permanent radera fordonsobservationer, kalenderposter,
exporterade filer, notishistorik eller all resehistorik för ett fordon. Raderingen loggas
och kan inte ångras – ta en säkerhetskopia först om du är osäker.
