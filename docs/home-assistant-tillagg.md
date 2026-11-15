# DriveLog som Home Assistant-tillägg

Det här är det enklaste sättet att köra DriveLog om du redan har Home Assistant OS (eller
Supervised): hela DriveLog körs som **ett tillägg** på Home Assistant-maskinen – ingen separat
server, ingen Docker Compose. Det andra sättet, en egen server med Docker Compose, beskrivs i
[installation.md](installation.md).

Du behöver bara tre saker: **Home Assistant**, din domän hos **STRATO** och routern hemma.

| Del | Vad den gör |
| --- | --- |
| Tillägget *SMGD DriveLog* | API, webbapp och HTTPS-proxy (Caddy) i en container. SQLite som databas; bakgrundsjobben körs i API:t. |
| Caddy i tillägget | Hämtar och förnyar certifikatet från Let's Encrypt automatiskt, tar emot port 80/443. |
| STRATO | DNS för `drivelog.smgd.se`. Tillägget kan hålla adressen uppdaterad (DynDNS). |
| Routern | Skickar port 80 och 443 vidare till Home Assistant-maskinen. |

> **Säkerhet:** Tillägget öppnar ingenting mot internet förrän ett administratörskonto finns.
> Nycklarna skapas slumpmässigt första gången och visas aldrig i loggen. API:t (port 8000) nås
> bara på Home Assistants interna nät.

## 1. Gör repot och avbilden publika (en gång)

Home Assistant hämtar tillägget från GitHub och den färdiga avbilden från GitHubs paketregister
(GHCR). Båda måste vara publika – Supervisor loggar inte in.

1. **Repot:** på GitHub, öppna `SMGD-MP/TEST` → **Settings** → **General** → längst ned under
   **Danger Zone** → **Change repository visibility** → **Make public**. Kontrollera först att
   inga hemligheter finns i historiken (repot innehåller inga lösenord eller nycklar).
2. **Vänta på bygget:** under **Actions** ska arbetsflödet **Home Assistant-tillägg** gå igenom på
   `main`. Sista jobbet, *Publicera avbilden till GHCR*, skapar paketet `drivelog`.
3. **Paketet:** öppna organisationen **SMGD-MP** → fliken **Packages** → **drivelog** →
   **Package settings** → under **Danger Zone** → **Change visibility** → **Public**.
   Syns inte valet *Public*: organisationens **Settings** → **Packages** → tillåt publika paket.
4. Kontrollera under **Package settings** att repot `SMGD-MP/TEST` är kopplat (det sker
   automatiskt via etiketten `org.opencontainers.image.source`).

Nya versioner publiceras när `version` i [`drivelog/config.yaml`](../drivelog/config.yaml) höjs
på `main` (en publicerad version skrivs aldrig över). Skriv då också en rad i
[`drivelog/CHANGELOG.md`](../drivelog/CHANGELOG.md).

## 2. STRATO: DynDNS för drivelog.smgd.se

Belagt i STRATOS hjälpartikel *So einfach richten Sie DynDNS für Ihre Domains ein*. Menynamnen
är STRATOS (på tyska).

1. Logga in i STRATOS kundinloggning. Finns inte subdomänen `drivelog` under `smgd.se` än:
   skapa den under **Domains → Domainverwaltung**.
2. **Domainverwaltung** → kugghjulet vid `drivelog.smgd.se` → fliken **DNS** → raden
   **DynDNS** → **verwalten** → slå på → **Einstellungen übernehmen**.
3. Sätt ett **DynDNS-lösenord**: **Sicherheit → Passwörter festlegen**. Använd ett eget,
   slumpat lösenord – inte huvudlösenordet.
4. **AAAA-post (IPv6):** under samma **DNS**-flik, raden **AAAA-Record** → **verwalten**. Finns
   en IPv6-adress där (STRATO lägger in en för sin parkeringssida) måste den bort eller ersättas
   med Home Assistant-maskinens egen publika IPv6-adress – annars försöker Let's Encrypt nå
   parkeringssidan och certifikatet utfärdas inte.
   *Inte belagt:* STRATOS hjälp beskriver hur en egen IPv6-adress sätts, men inte hur posten tas
   bort helt. Hittar du inget val för det, fråga STRATOS support om att ta bort AAAA-posten för
   subdomänen. Tillägget varnar i loggen så länge en AAAA-post leder någon annanstans.
5. I tilläggets inställningar (steg 4): `strato_dyndns` på, `strato_dyndns_user` = **domänen i
   ditt paket** (`smgd.se`) och `strato_dyndns_password` = DynDNS-lösenordet.

Sköter routern redan DynDNS mot STRATO (många routrar kan det) låter du `strato_dyndns` vara av.

Kontrollera med `nslookup drivelog.smgd.se` (ska ge hemmets publika IPv4-adress) och
`nslookup -type=AAAA drivelog.smgd.se` (ska inte ge någon främmande adress). Ändringar hos STRATO
kan ta en stund att slå igenom.

## 3. Routern: port 80 och 443

1. Ge Home Assistant-maskinen en **fast lokal adress** (DHCP-reservation i routern).
2. Skapa **portvidarebefordring**: extern port **80/TCP** → maskinens adress port 80, och
   **443/TCP** (gärna även **443/UDP** för HTTP/3) → port 443.
3. Inget annat tillägg får använda 80 eller 443 på maskinen (t.ex. NGINX Proxy Manager).

**Ingen publik IPv4-adress?** Vissa operatörer (särskilt fiber via vissa stadsnät och mobilt
bredband) delar en adress mellan många kunder ("CGNAT"). Jämför adressen routern visar som sin
WAN-adress med den `nslookup` ovan ger efter en DynDNS-uppdatering: börjar routerns adress med
`100.64.`–`100.127.`, `10.` eller `192.168.`, eller skiljer de sig åt, kan port 80/443 inte nås
utifrån. Be då operatören om en publik IPv4-adress (ofta gratis eller en liten avgift). Utan
publik adress fungerar DriveLog bara hemma – inte via `drivelog.smgd.se`.

## 4. Home Assistant: installera tillägget

1. **Inställningar → Tillägg → Tilläggsbutik** → menyn **⋮** → **Förråd** → lägg till
   `https://github.com/SMGD-MP/TEST` → **Lägg till** → stäng.
2. Ladda om sidan, sök efter **SMGD DriveLog** och tryck **Installera** (avbilden hämtas –
   ingenting byggs på maskinen).
3. Fliken **Konfiguration**:
   - `domain`: `drivelog.smgd.se`
   - `admin_email` och `admin_password` (minst 12 tecken) – det första kontot
   - `strato_dyndns`, `strato_dyndns_user`, `strato_dyndns_password` – se steg 2
   - `log_level`: `info`

   Tryck **Spara**.
4. Fliken **Info**: slå på **Starta vid uppstart** och **Watchdog**, tryck **Starta**.
5. Fliken **Logg**. Leta efter:
   - `API:t är redo. Home Assistant-integrationen når det på http://<värdnamn>:8000` – skriv upp
     värdnamnet (t.ex. `82cbe348-drivelog`; prefixet beror på förrådets adress),
   - `DriveLog startas på https://drivelog.smgd.se`,
   - med DynDNS: `STRATO har satt …` eller `… leder hit – ingen uppdatering behövs`.

   Står det `Det finns inget konto i DriveLog än` saknas `admin_email`/`admin_password`; fyll i
   och starta om. Webbapp och proxy startar inte förrän kontot finns.

När kontot finns kan du tömma `admin_password` och spara – kontot finns kvar i databasen. Byt
sedan lösenord inne i DriveLog.

## 5. Öppna DriveLog

Gå till **https://drivelog.smgd.se** och logga in. Första gången tar det upp till någon minut
innan certifikatet är utfärdat. Installera gärna webbappen på telefonen (*Lägg till på
hemskärmen*).

## 6. Notiser i Home Assistant

1. Installera integrationen **SMGD DriveLog** via HACS (se [home-assistant.md](home-assistant.md)).
2. Skapa en API-token i DriveLog: **Integrationer → Home Assistant → Skapa API-token**.
3. **Inställningar → Enheter och tjänster → Lägg till integration → SMGD DriveLog**:
   - Adress: `http://<värdnamn>:8000` – värdnamnet från tilläggets logg (Supervisor ger varje
     tillägg värdnamnet *förrådets-id*`-drivelog` på det interna nätet). Trafiken stannar då i
     Home Assistant och går inte ut på internet.
   - API-token från punkt 2.

`https://drivelog.smgd.se` fungerar också som adress, men kräver att Home Assistant når sin egen
publika adress (se hårnåls-NAT nedan).

## 7. Säkerhetskopiering och uppgradering

- **Säkerhetskopior:** Home Assistants vanliga säkerhetskopior (**Inställningar → System →
  Säkerhetskopior**) tar med tilläggets data: databasen, uppladdade filer, certifikaten och
  **krypteringsnyckeln**. Tillägget stoppas några sekunder medan kopian tas, så att SQLite-
  databasen alltid är hel. Förvara kopiorna säkert (de innehåller nyckeln) och ha minst en kopia
  utanför maskinen, t.ex. via Home Assistants inbyggda moln- eller nätverkslagring.
- **Återställning:** återställ tillägget ur en säkerhetskopia; nyckel och databas hör ihop och
  återställs tillsammans.
- **Uppgradering:** när en ny version finns visas **Uppdatera** på tilläggets sida. Databasen
  migreras automatiskt vid start. Ta gärna en säkerhetskopia först (Home Assistant erbjuder det).

## 8. Felsökning

| Problem | Kontrollera |
| --- | --- |
| Loggen säger att en inställning behöver rättas | Meddelandet säger vilken; spara och starta om. |
| `Det finns inget konto` | Fyll i `admin_email` och `admin_password` och starta om. |
| Certifikatet utfärdas inte (Caddy loggar fel från Let's Encrypt) | A-posten pekar på hemmets publika IPv4; **ingen** främmande AAAA-post; port 80 och 443 vidarebefordrade; ingen CGNAT. Testa utifrån, t.ex. från mobilnätet: `http://drivelog.smgd.se` ska svara. |
| Tillägget startar inte: port 80/443 upptagen | Ett annat tillägg använder porten – stoppa det eller ändra dess port. |
| `STRATO godtog inte användarnamn eller lösenord` | `strato_dyndns_user` ska vara domänen i paketet (`smgd.se`), lösenordet DynDNS-lösenordet. Uppdateringarna står still tills tillägget startas om. |
| `… har en AAAA-post (IPv6) som inte leder hit` | Ta bort/ändra AAAA-posten hos STRATO (steg 2). |
| Fungerar från mobilnätet men inte hemma på wifi | Routern saknar **hårnåls-NAT** (NAT loopback): den kan inte skicka trafik till sin egen publika adress tillbaka in. Slå på det i routern om det går, eller låt hemmets DNS svara med maskinens lokala adress för `drivelog.smgd.se` (t.ex. en lokal DNS-post i routern). Tillägget märker också detta och uppdaterar då STRATO högst varannan timme. |
| Integrationen i Home Assistant når inte DriveLog | Använd `http://<värdnamn>:8000` från loggen; kontrollera att tillägget är igång. |

Mer om webbappen: [anvandning.md](anvandning.md). Om säkerhet: [sakerhet.md](sakerhet.md).

## Teknisk bakgrund

- Tilläggets filer finns i [`drivelog/`](../drivelog/): `config.yaml` (inställningar, portar,
  watchdog, säkerhetskopiering), `Dockerfile` och startprogrammet under `rootfs/`.
- Avbilden byggs av GitHub Actions ([`.github/workflows/addon.yml`](../.github/workflows/addon.yml))
  för amd64 och aarch64 och provstartas där som Supervisor skulle göra innan den publiceras.
- Startprogrammet sätter backendens miljövariabler (enprocessläge): `DRIVELOG_ENVIRONMENT=production`,
  `DRIVELOG_DATABASE_URL=sqlite:////data/drivelog.sqlite3`, `DRIVELOG_DATA_DIR=/data`,
  `DRIVELOG_JOBS_EAGER=true`, tom `DRIVELOG_REDIS_URL`, `DRIVELOG_BASE_URL=https://<domän>`,
  nycklarna ur `/data/hemligheter/`, första administratören ur inställningarna,
  `DRIVELOG_TIMEZONE` från Home Assistant och `FORWARDED_ALLOW_IPS=127.0.0.1`.
- API, webbapp och Caddy körs som var sin icke-root-användare. Caddy får bara förmågan att binda
  port 80/443 (`CAP_NET_BIND_SERVICE`, via `setpriv`).
