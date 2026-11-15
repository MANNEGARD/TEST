# SMGD DriveLog

DriveLog körs här i sin helhet: API:t, webbappen och en HTTPS-proxy (Caddy) som hämtar
certifikat från Let's Encrypt. Databasen (SQLite), nycklarna och certifikaten ligger i
tilläggets egen datakatalog och tas med i Home Assistants säkerhetskopior.

Den fullständiga guiden – STRATO, routern, notiser och felsökning – finns i repot:
[docs/home-assistant-tillagg.md](https://github.com/SMGD-MP/TEST/blob/main/docs/home-assistant-tillagg.md).

## Innan du startar

1. **Domänen** (t.ex. `drivelog.smgd.se`) måste peka på hemmets publika IPv4-adress – antingen
   genom att tillägget sköter STRATO DynDNS (se nedan) eller genom att routern gör det.
   Domänen får **inte** ha en AAAA-post (IPv6) som pekar någon annanstans, t.ex. STRATOS
   parkeringssida – då kan certifikatet inte utfärdas.
2. **Routern** ska skicka port **80** och **443** (TCP, och gärna 443 UDP) vidare till
   Home Assistant-maskinens lokala adress. Port 80 behövs för att Let's Encrypt ska kunna
   kontrollera domänen och för att skicka vidare till HTTPS.
3. Inga andra tillägg får använda port 80 eller 443 på maskinen (t.ex. NGINX Proxy Manager).

## Inställningar

| Inställning | Betydelse |
| --- | --- |
| `domain` | Domänen DriveLog nås på, t.ex. `drivelog.smgd.se` (utan `https://`). |
| `admin_email` | E-post för det första administratörskontot. |
| `admin_password` | Lösenord för det första kontot, minst 12 tecken. |
| `strato_dyndns` | På: tillägget håller domänens A-post hos STRATO uppdaterad. Av: något annat (t.ex. routern) sköter det. |
| `strato_dyndns_user` | Domänen i ditt STRATO-paket, t.ex. `smgd.se`. |
| `strato_dyndns_password` | DynDNS-lösenordet du har satt hos STRATO. |
| `log_level` | `debug`, `info`, `warning` eller `error`. |

`admin_email` och `admin_password` behövs **bara tills kontot finns**. Är de tomma och finns
inget konto startar tillägget bara API:t internt, och loggen säger vad du ska fylla i – webbappen
och proxyn startas inte, så ingenting är åtkomligt från internet utan konto. När kontot finns kan
du tömma lösenordsfältet; byt sedan lösenord inne i DriveLog.

## Första start

1. Fyll i inställningarna och tryck **Spara**.
2. Tryck **Starta** och öppna fliken **Logg**. Där står bland annat:
   - att nycklarna skapades (bara första gången),
   - `API:t är redo. Home Assistant-integrationen når det på http://<värdnamn>:8000` – spara
     värdnamnet, det behövs för notiserna,
   - att DriveLog startas på `https://<din domän>`.
3. Öppna `https://<din domän>` och logga in. Första gången kan det ta någon minut innan
   certifikatet är på plats.

## Notiser i Home Assistant

Installera integrationen **SMGD DriveLog** via HACS (samma repo) och ange som adress
`http://<värdnamn>:8000` – värdnamnet från loggen, t.ex. `xxxxxxxx-drivelog`. Trafiken går då
direkt på Home Assistants interna nät. API-token skapas i DriveLog under
**Integrationer → Home Assistant**. Port 8000 publiceras inte på maskinen och nås inte från
internet.

## STRATO DynDNS

Med `strato_dyndns` påslaget kontrollerar tillägget var femte minut att domänen fortfarande
leder hit, och skickar en uppdatering till STRATO bara när den inte gör det (och en gång per
dygn för säkerhets skull). STRATO sätter då hemmets publika IPv4-adress. Ingen annan tjänst
tillfrågas. Fel som kräver åtgärd (fel lösenord, okänd domän) stoppar uppdateringarna tills
tillägget startas om – upprepade felaktiga försök kan annars få STRATO att spärra domänen.

## Säkerhetskopior och uppdateringar

Tillägget stoppas en kort stund medan Home Assistant tar en säkerhetskopia, så att databasen
alltid är hel. Kopian innehåller databasen, uppladdade filer, certifikaten **och
krypteringsnyckeln** – förvara den som en hemlighet. Uppdateringar visas i Home Assistant när
en ny version publicerats; databasen uppgraderas automatiskt vid start.

## Felsökning

- **Certifikatet utfärdas inte:** kontrollera att domänen pekar på rätt adress (A-post), att
  ingen AAAA-post finns och att port 80 och 443 når Home Assistant-maskinen utifrån.
- **Fungerar utifrån men inte hemma:** routern saknar troligen "hårnåls-NAT". Testa från
  mobilnätet, eller se guiden.
- **Tillägget startar inte:** loggen börjar med vilken inställning som behöver rättas.
