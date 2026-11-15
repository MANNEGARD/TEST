# SMGD DriveLog

DriveLog körs här i sin helhet: API:t, webbappen och en HTTPS-proxy (Caddy) som hämtar
certifikat från Let's Encrypt. Databasen (SQLite), nycklarna och certifikaten ligger i appens
egen datakatalog och tas med i Home Assistants säkerhetskopior.

Den fullständiga guiden – STRATO, routern, notiser och felsökning – finns i repot:
[docs/home-assistant-tillagg.md](https://github.com/SMGD-MP/TEST/blob/HEAD/docs/home-assistant-tillagg.md).

## Innan du startar

1. **Domänen** (t.ex. `drivelog.smgd.se`) måste peka på hemmets publika IPv4-adress – antingen
   genom att appen sköter STRATO DynDNS (se nedan) eller genom att routern gör det.
   Domänen får **inte** ha någon AAAA-post (IPv6) – ta bort den som STRATO lägger in för sin
   parkeringssida, annars kan certifikatet inte utfärdas.
2. **Routern** ska skicka port **80** och **443** (TCP, och gärna 443 UDP) vidare till
   Home Assistant-maskinens lokala adress.
3. Ingen annan app får använda port 80 eller 443 på maskinen (t.ex. NGINX Proxy Manager).

## Inställningar (fliken Konfiguration)

| Fält | Betydelse |
| --- | --- |
| **Domän** (`domain`) | Domänen DriveLog nås på, t.ex. `drivelog.smgd.se` (utan `https://`). |
| **Administratörens e-post** (`admin_email`) | E-post för det första kontot – blir inloggningen. |
| **Administratörens lösenord** (`admin_password`) | Lösenord för det första kontot, minst 12 tecken. |
| **STRATO DynDNS** (`strato_dyndns`) | På: appen håller domänens A-post hos STRATO uppdaterad. Av: något annat (t.ex. routern) sköter det. |
| **STRATO-användarnamn** (`strato_dyndns_user`) | Domänen i ditt STRATO-paket, t.ex. `smgd.se`. |
| **STRATO DynDNS-lösenord** (`strato_dyndns_password`) | DynDNS-lösenordet du har satt hos STRATO. |
| **Loggnivå** (`log_level`) | `debug`, `info`, `warning` eller `error`. |

Tomma fält räknas som ej ifyllda. Kontot skapas **bara** härifrån: så länge det inte finns
startar appen bara API:t internt, och loggen säger vad du ska fylla i – webbappen och proxyn
startas inte, så ingenting är åtkomligt från internet. När kontot finns, töm
**Administratörens lösenord** och byt lösenord inne i DriveLog.

## Första start

1. Fyll i fälten och tryck **Spara**.
2. Fliken **Information**: slå på **Starta vid uppstart** och **Vakthund**, tryck **Starta**.
3. Fliken **Logg**. När det fungerar står där:
   - att nycklarna och kontot skapades (bara första gången),
   - `API:t är redo. Home Assistant-integrationen når det på http://<värdnamn>:8000` – spara
     värdnamnet, det behövs för notiserna,
   - `DriveLog startas på https://<din domän>`.
4. Öppna `https://<din domän>` och logga in. Första gången kan det ta någon minut innan
   certifikatet är på plats.

## Notiser i Home Assistant

Installera integrationen **SMGD DriveLog** via HACS (samma repo) och ange som adress
`http://<värdnamn>:8000` – värdnamnet från loggen, t.ex. `xxxxxxxx-drivelog`. API-token skapas
i DriveLog under **Integrationer → Home Assistant**. Port 8000 publiceras inte på maskinen och
nås inte från internet.

## STRATO DynDNS

Med **STRATO DynDNS** påslaget kontrollerar appen var femte minut att domänen fortfarande
leder hit, och skickar en uppdatering till STRATO bara när den inte gör det (och en gång per
dygn för säkerhets skull). Ingen annan tjänst tillfrågas. Fel som kräver åtgärd (fel lösenord,
okänd domän) stoppar uppdateringarna tills appen startas om – upprepade felaktiga försök kan
annars få STRATO att spärra domänen. DynDNS måste ingå i ditt STRATO-paket.

## Säkerhetskopior och uppdateringar

Appen stoppas en kort stund medan Home Assistant tar en säkerhetskopia, så att databasen
alltid är hel. Kopian innehåller databasen, filerna, certifikaten **och krypteringsnyckeln** –
förvara den som en hemlighet. Saknas nyckeln men databasen finns startar appen inte; återställ
då ur en säkerhetskopia.

**Uppdatera** visas så fort en ny version finns i repot, men avbilden byggs först efteråt:
vänta tills arbetsflödet *Home Assistant-tillägg* under repots **Actions** är grönt innan du
trycker **Uppdatera**. Databasen uppgraderas automatiskt vid start.

## Felsökning

- **`Det finns inget konto`:** fyll i e-post och lösenord för administratören, spara, starta om.
- **Certifikatet utfärdas inte:** kontrollera att domänen pekar på rätt adress (A-post), att
  ingen AAAA-post finns och att port 80 och 443 når Home Assistant-maskinen utifrån.
- **Fungerar utifrån men inte hemma:** routern saknar troligen "hårnåls-NAT". Se guiden.
- **Appen startar inte:** loggen börjar med vad som behöver rättas.
