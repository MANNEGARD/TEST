# DriveLog som app i Home Assistant

Det här är det enklaste sättet att köra DriveLog om du redan har Home Assistant OS: hela DriveLog
körs som **en app** (hette *tillägg* före Home Assistant 2026.2) på Home Assistant-maskinen –
ingen separat server, ingen Docker Compose. Det andra sättet, en egen server med Docker Compose,
beskrivs i [installation.md](installation.md).

Du behöver tre saker: **Home Assistant**, din domän hos **STRATO** och **routern** hemma.

| Del | Vad den gör |
| --- | --- |
| Appen *SMGD DriveLog* | API, webbapp och HTTPS-proxy (Caddy) i en container. SQLite som databas; bakgrundsjobben körs i API:t. |
| Caddy i appen | Hämtar och förnyar certifikatet från Let's Encrypt automatiskt, tar emot port 80/443. |
| STRATO | DNS för `drivelog.smgd.se`. Appen kan hålla adressen uppdaterad (DynDNS). |
| Routern | Skickar port 80 och 443 vidare till Home Assistant-maskinen. |

> **Säkerhet:** Appen öppnar ingenting mot internet förrän administratörskontot finns, och
> kontot skapas **bara** från appens inställningar – aldrig via webben eller av en annan app.
> Nycklarna skapas slumpmässigt första gången och visas aldrig i loggen. API:t (port 8000) nås
> bara på Home Assistants interna nät.

Menynamnen nedan är Home Assistants svenska namn (engelska inom parentes), hämtade ur Home
Assistants egna översättningar för version 2026.10/2026.11.

## 1. GitHub: gör repot och avbilden publika (en gång)

Home Assistant hämtar appen från GitHub och den färdiga avbilden från GitHubs paketregister
(GHCR). Båda måste vara publika – Home Assistant loggar inte in.

1. **Repot:** öppna `SMGD-MP/TEST` → **Settings** → **General** → längst ned under
   **Danger Zone** → **Change repository visibility** → **Make public**.
2. **Vänta på bygget:** fliken **Actions** → arbetsflödet **Home Assistant-tillägg** ska vara
   grönt för repots standardgren. Sista jobbet, *Publicera avbilden till GHCR*, skapar paketet
   `drivelog`.
3. **Paketet** (SMGD-MP är ett personligt konto): öppna
   <https://github.com/SMGD-MP?tab=packages> → **drivelog** → **Package settings** → under
   **Danger Zone** → **Change visibility** → **Public** → skriv `drivelog` och bekräfta. Det går
   inte att göra paketet privat igen.
   *Om repot i stället ägs av en organisation:* paketet finns under organisationens
   **Packages**, och syns inte valet *Public* måste publika paket först tillåtas under
   organisationens **Settings → Packages**.

## 2. STRATO: domänens adress

Menynamnen är STRATOS (på tyska).

1. Logga in hos STRATO. Finns inte subdomänen `drivelog` under `smgd.se` än: skapa den under
   **Domains → Domainverwaltung**.
2. **Domainverwaltung** → kugghjulet vid `drivelog.smgd.se` → fliken **DNS**.
3. **DynDNS** (hoppa över om routern redan sköter DynDNS mot STRATO): raden **DynDNS** →
   **verwalten** → slå på → **Einstellungen übernehmen**. Sätt sedan ett eget, slumpat
   **DynDNS-lösenord** under **Sicherheit → Passwörter festlegen** (inte huvudlösenordet).
   *Syns ingen DynDNS-rad* ingår DynDNS inte i ditt STRATO-paket (det finns t.ex. i
   PowerWeb-paketen och i STRATO Domain). Låt då routern sköta adressen, eller fråga STRATO.
4. **AAAA-post (IPv6) – ta bort den.** Raden **AAAA-Record** → **verwalten**. Finns en
   IPv6-adress där (STRATO lägger in en för sin parkeringssida) ska den bort – annars försöker
   Let's Encrypt nå parkeringssidan och inget certifikat utfärdas. Domänen `smgd.se` har redan det
   läget (bara en A-post, ingen AAAA). Hittar du inget val för att ta bort posten: be STRATOS
   support ta bort AAAA-posten för `drivelog.smgd.se`. Appen varnar i loggen så länge en sådan
   post finns – också när DynDNS sköts av routern.

Kontrollera efteråt (i en terminal, eller på en webbsida för DNS-uppslag):
`nslookup drivelog.smgd.se` ska ge hemmets publika IPv4-adress och
`nslookup -type=AAAA drivelog.smgd.se` ska inte ge någon adress. Ändringar kan ta en stund.

## 3. Routern: port 80 och 443

1. Ge Home Assistant-maskinen en **fast lokal adress** (DHCP-reservation i routern).
2. Skapa **portvidarebefordring**: extern port **80/TCP** → maskinens adress port 80, och
   **443/TCP** (gärna även **443/UDP**) → port 443.
3. Ingen annan app får använda 80 eller 443 på maskinen (t.ex. NGINX Proxy Manager).

**Når port 80/443 inte fram?** Titta på routerns WAN-adress (internetadress) på routerns
statussida:

- **`10.x`, `172.16.x`–`172.31.x` eller `192.168.x`:** det sitter en router till framför din
  (oftast operatörens box) – *dubbel NAT*. Vidarebefordra 80/443 även i den boxen till din
  router, eller ställ boxen i *bryggläge*.
- **`100.64.x`–`100.127.x`, eller en annan adress än `nslookup` ger efter en DynDNS-uppdatering:**
  operatören delar adressen mellan flera kunder (*CGNAT*). Be operatören om en publik
  IPv4-adress (ofta gratis eller en liten avgift). Utan den fungerar DriveLog bara hemma.

## 4. Home Assistant: installera och starta appen

1. Lägg till förrådet: öppna länken
   <https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FSMGD-MP%2FTEST>
   och tryck **Lägg till** – eller gör det för hand: **Inställningar → Appar → Installera app**
   (*Settings → Apps → Install app*) → menyn **⋮** uppe till höger → **Kodförråd**
   (*Repositories*) → klistra in `https://github.com/SMGD-MP/TEST` → **Lägg till** → stäng.
   (Före Home Assistant 2026.2 hette det **Inställningar → Tillägg → Tilläggsbutik**.)
2. Ladda om sidan, sök efter **SMGD DriveLog** i **Appbutiken** och tryck **Installera**.
   Avbilden hämtas – ingenting byggs på maskinen.
3. Fliken **Konfiguration**, fyll i:

   | Fält | Värde |
   | --- | --- |
   | **Domän** | `drivelog.smgd.se` |
   | **Administratörens e-post** | din e-postadress – blir inloggningen |
   | **Administratörens lösenord** | minst 12 tecken |
   | **STRATO DynDNS** | på om appen ska sköta adressen (steg 2.3), annars av |
   | **STRATO-användarnamn** | domänen i paketet: `smgd.se` (bara med DynDNS) |
   | **STRATO DynDNS-lösenord** | DynDNS-lösenordet från steg 2.3 (bara med DynDNS) |
   | **Loggnivå** | `info` |

   Tryck **Spara**. (Alla fält syns direkt; reglaget *Visa oanvända valfria
   konfigurationsalternativ* behövs inte.)
4. Fliken **Information**: slå på **Starta vid uppstart** och **Vakthund** (*Watchdog*), tryck
   **Starta**.
5. Fliken **Logg**. När allt fungerar står det, i den här ordningen:
   - `DRIVELOG_SECRET_KEY skapades och sparades …` (bara första gången),
   - `Administratörskontot <din adress> skapades.` och
     `Lösenordet för <din adress> sattes från appens inställningar.` (bara första gången),
   - `API:t är redo. Home Assistant-integrationen når det på http://<värdnamn>:8000` – skriv upp
     värdnamnet, t.ex. `82cbe348-drivelog`,
   - `DriveLog startas på https://drivelog.smgd.se`,
   - med DynDNS: `STRATO har satt …` eller `… leder hit – ingen uppdatering behövs`.

   Står det `Det finns inget konto i DriveLog än`: e-post eller lösenord saknas – fyll i, spara
   och starta om. Webbapp och proxy startar inte förrän kontot finns.
6. När kontot finns: töm gärna **Administratörens lösenord** och tryck **Spara** (ingen omstart
   behövs). Lösenordet byter du sedan inne i DriveLog. Kontot finns kvar i databasen.

   Så fungerar fältet: lösenordet i **Administratörens lösenord** tillämpas bara när det har
   **ändrats** sedan appen senast använde det. Står samma lösenord kvar och du har bytt lösenord
   inne i DriveLog, gäller alltså ditt nya lösenord även efter omstart. Appen sparar inte
   lösenordet – bara ett kontrollvärde (en nyckelad hash i `/data/hemligheter`) som inte går att
   räkna tillbaka eller använda för att pröva lösenord.

### 4.7 Glömt lösenordet

1. **Inställningar → Appar → SMGD DriveLog**, fliken **Konfiguration**.
2. Kontrollera att **Administratörens e-post** är adressen du loggar in med.
3. Skriv ett **nytt** lösenord (minst 12 tecken) i **Administratörens lösenord** – inte samma
   som stod där förut. Tryck **Spara**.
4. Fliken **Information**: tryck **Starta om**.
5. Fliken **Logg**: vänta på `Lösenordet för <din adress> sattes från appens inställningar.`
   och `DriveLog startas på https://drivelog.smgd.se`.
6. Logga in med det nya lösenordet. Alla inloggade sessioner för kontot (t.ex. på en borttappad
   telefon) har avslutats; API-token för Home Assistant fortsätter att fungera.
7. Töm gärna fältet igen och tryck **Spara**.

Står adressen inte på något konto skapas ett nytt administratörskonto med den adressen, och
loggen varnar för det. Säger loggen att lösenordet *inte* ändrades, se felsökningen (steg 8).

## 5. Öppna DriveLog

Gå till **https://drivelog.smgd.se** och logga in. Första gången tar det upp till någon minut
innan certifikatet är utfärdat. Lägg gärna till webbappen på telefonens hemskärm.

## 6. Notiser i Home Assistant

1. Installera integrationen **SMGD DriveLog** via HACS (se [home-assistant.md](home-assistant.md)).
2. Skapa en API-token i DriveLog: **Integrationer → Home Assistant → Skapa API-token**.
3. **Inställningar → Enheter och tjänster → Lägg till integration → SMGD DriveLog**:
   - Adress: `http://<värdnamn>:8000` – värdnamnet från appens logg. Trafiken stannar då i
     Home Assistant och går inte ut på internet.
   - API-token från punkt 2.

## 7. Säkerhetskopior och uppdateringar

- **Säkerhetskopior:** Home Assistants vanliga säkerhetskopior (**Inställningar → System →
  Säkerhetskopior**) tar med appens data: databasen, filerna, certifikaten och
  **krypteringsnyckeln**. Appen stoppas några sekunder medan kopian tas. Förvara kopiorna säkert
  och ha minst en utanför maskinen.
- **Återställning:** nyckel och databas hör ihop och återställs tillsammans. Saknas nyckeln men
  databasen finns vägrar appen starta (`nyckeln … saknas men databasen finns`) i stället för att
  tyst skapa en ny – återställ då appen ur en säkerhetskopia.
- **Uppdatera:** Home Assistant visar **Uppdatera** så fort en ny version finns i repot – men
  avbilden byggs först efteråt. **Vänta tills arbetsflödet *Home Assistant-tillägg* under
  Actions är grönt** för repots standardgren innan du trycker **Uppdatera** (annars misslyckas
  hämtningen med `manifest unknown`; försök då igen senare). Databasen migreras automatiskt.

För den som ger ut en ny version: höj `version` i [`drivelog/config.yaml`](../drivelog/config.yaml)
och skriv en rad i [`drivelog/CHANGELOG.md`](../drivelog/CHANGELOG.md) på repots standardgren.
En publicerad version skrivs aldrig över.

## 8. Felsökning

| Problem | Gör så här |
| --- | --- |
| Loggen säger att en inställning behöver rättas | Meddelandet säger vilken; rätta, spara och starta om. |
| `Det finns inget konto` | Fyll i **Administratörens e-post** och **Administratörens lösenord**, spara, starta om. |
| Glömt lösenordet | Skriv ett nytt lösenord i **Administratörens lösenord**, spara och starta om appen (steg 4.7). |
| `Lösenordet ändrades inte` med `minst 12 tecken` eller `ser inte ut som en e-postadress` | Rätta fältet, spara och starta om. DriveLog är igång med det tidigare lösenordet under tiden. |
| `… det finns inget märke över när det senast tillämpades …` eller `… märkesfilen … var trasig` | Appen kan inte avgöra om lösenordet i fältet är nytt eller ett gammalt som blivit kvar (t.ex. första starten efter uppdatering från 1.0.1) och ändrar därför ingenting. Skriv ett **annat** nytt lösenord, spara och starta om. |
| `Det fanns inget konto med adressen … så det skapades nu som administratör` | Adressen i **Administratörens e-post** stod inte på något konto. Var det fel adress: logga in med det nya kontot och ta bort det, eller rätta adressen och återställ lösenordet för rätt konto. Har du inte själv ändrat inställningarna: kontrollera vem som har administratörsåtkomst till Home Assistant. |
| `Lösenordet för … kunde inte sättas` | Meddelandet efter kolonet säger varför. Finns redan ett konto startar DriveLog ändå med det tidigare lösenordet; nästa omstart försöker igen. |
| Certifikatet utfärdas inte (Caddy loggar fel från Let's Encrypt) | A-posten pekar på hemmets publika IPv4; **ingen** AAAA-post; port 80 och 443 vidarebefordrade (steg 3). Testa från mobilnätet: `http://drivelog.smgd.se` ska svara. |
| Appen startar inte: port 80/443 upptagen | En annan app använder porten – stoppa den. |
| `STRATO godtog inte användarnamn eller lösenord` | **STRATO-användarnamn** = domänen i paketet (`smgd.se`), lösenordet = DynDNS-lösenordet. Uppdateringarna står still tills appen startas om. |
| `STRATO känner inte till domänen …` (`nohost`) | DynDNS är inte påslaget för `drivelog.smgd.se`, eller ingår inte i paketet (steg 2.3). |
| `… har en AAAA-post (IPv6) som inte leder hit` | Ta bort AAAA-posten hos STRATO (steg 2.4). |
| `nyckeln … saknas men databasen finns` | Återställ appen ur en säkerhetskopia (steg 7). |
| Fungerar från mobilnätet men inte hemma på wifi | Routern saknar **hårnåls-NAT** (NAT loopback). Slå på det i routern om det går, eller låt routerns DNS svara med maskinens lokala adress för `drivelog.smgd.se`. Appen märker det och uppdaterar då STRATO högst varannan timme; en lokal DNS-post luras den inte av. |
| Integrationen når inte DriveLog | Använd `http://<värdnamn>:8000` från loggen; kontrollera att appen är igång. |

Mer om webbappen: [anvandning.md](anvandning.md). Om säkerhet: [sakerhet.md](sakerhet.md).

## Teknisk bakgrund

- Appens filer finns i [`drivelog/`](../drivelog/): `config.yaml` (inställningar, portar,
  watchdog, säkerhetskopiering), `Dockerfile` och startprogrammet under `rootfs/`. Avbilden
  byggs och provstartas av [`.github/workflows/addon.yml`](../.github/workflows/addon.yml) för
  amd64 och aarch64. Varje ny version byggs på de senaste rättade bas-avbilderna (Python,
  Node, Caddy); Actions i arbetsflödet är fästa vid commit-SHA.
- **Miljön till backend** (enprocessläge): `DRIVELOG_ENVIRONMENT=production`,
  `DRIVELOG_DATABASE_URL=sqlite:////data/app/drivelog.sqlite3`, `DRIVELOG_DATA_DIR=/data/app`,
  `DRIVELOG_JOBS_EAGER=true`, tom `DRIVELOG_REDIS_URL`, `DRIVELOG_BASE_URL=https://<domän>`,
  nycklarna ur `/data/hemligheter/`,
  `DRIVELOG_WEB_SETUP=false` (POST `/api/v1/auth/setup` svarar 403 `setup_disabled`),
  `DRIVELOG_TIMEZONE` från Home Assistant och `FORWARDED_ALLOW_IPS=127.0.0.1` (API:t litar bara
  på `X-Forwarded-For` från Caddy; anrop direkt mot port 8000 räknas på avsändarens egen adress).
- **Administratörskontot** skapas och lösenordet återställs av startprogrammet, när API:t är redo
  men innan webbapp och proxy startas, med `python -m app.cli create-admin <adress>` som
  API-användaren och med API:ts miljö. Lösenordet ges i miljövariabeln `DRIVELOG_ADMIN_PASSWORD`
  (aldrig på kommandoraden, aldrig till API-processen) och skrivs aldrig i loggen. Kommandot
  avslutar kontots inloggade sessioner, som ett lösenordsbyte i webbappen. Det körs bara när
  `HMAC-SHA256(nyckel ur secret_key, adress + lösenord)` skiljer sig från märket i
  `/data/hemligheter/admin_losenord_marke` – eller när märket saknas och det inte finns något
  konto alls. Saknas märket (eller är trasigt) men konton finns ändras ingenting; märket sparas
  för de nuvarande uppgifterna, så att bara ett nytt lösenord tillämpas.
- **Lagringen i /data:**

  | Sökväg | Ägare | Rättigheter | Innehåll |
  | --- | --- | --- | --- |
  | `/data` | root | 711 | – |
  | `/data/options.json` | root | 600 | inställningarna (skrivs av Home Assistant) |
  | `/data/hemligheter/` | root | 700 (filer 600) | `secret_key`, `encryption_keys`, `admin_losenord_marke` |
  | `/data/app/` | drivelog | 700 | databasen och API:ts filer |
  | `/data/caddy/` | caddy | 700 | certifikat och ACME-konto |

  Nycklarna läses bara om de är vanliga filer som ägs av root (inga symlänkar, inga hårda
  länkar), och en saknad nyckel skapas bara när databasen inte finns.
- API, webbapp och Caddy körs som var sin icke-root-användare. Caddy får bara förmågan att binda
  port 80/443 (`CAP_NET_BIND_SERVICE`, via `setpriv`).
- **Kontrollen "leder domänen hit?"** (DynDNS och AAAA-varningen) är fråga och svar: appen
  hämtar `/.well-known/drivelog-dyndns?n=<slumpvärde>` och godtar bara en HMAC som räknas ut med
  en nyckel som skapas vid varje start. Bara globala adresser godtas.
