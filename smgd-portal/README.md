# smgd.se: inloggning och portal

Startsidan på **smgd.se**: en inloggning med lösenord och tvåstegsverifiering. Efter
inloggning visas en portal med länkar till subdomänerna (DriveLog, Home Assistant, VCI
Config och Developer Lab), och för varje subdomän en markering som visar om den svarar.

Inloggningen kontrolleras på servern av Netlifys edge-funktioner. Portalen finns aldrig som
en statisk fil, så den kan inte öppnas utan en giltig session.

## Säkerhet

| Skydd | Hur |
| --- | --- |
| Två faktorer | Lösenord **och** sexsiffrig TOTP-kod (RFC 6238) från en autentiseringsapp |
| Lösenordshash | PBKDF2-SHA256 med 100 000 iterationer och slumpat salt. Själva lösenordet sparas aldrig |
| Gissningsskydd | Efter 5 misslyckade försök inom 15 min spärras IP-adressen i 15 min. Dessutom en fördröjning på cirka 1 s vid fel och samma felmeddelande oavsett vilken faktor som var fel |
| Återanvändning av kod | Varje TOTP-kod går bara att använda en gång (sparas i Netlify Blobs) |
| Session | HMAC-signerad kaka `__Host-smgd_session` med `HttpOnly`, `Secure`, `SameSite=Lax`. Gäller i 12 h |
| CSRF | POST-anrop måste ha samma `Origin` som sidan |
| Tidsattacker | Alla jämförelser görs i konstant tid |
| Webbläsarskydd | Strikt CSP (inga inline-skript och inga externa källor), HSTS, `frame-ancestors 'none'`, COOP/CORP, `no-referrer`, `noindex` |
| Stängt läge | Om en hemlighet saknas går det inte att logga in alls |

**Logga ut alla sessioner:** sätt `PORTAL_SESSION_VERSION` till ett nytt värde, t.ex. `2`,
och gör en ny deploy.

## Installation

### Enklast: installationslänk

1. Sätt miljövariabeln `PORTAL_SETUP_TOKEN` till en lång slumpad sträng (minst 32 tecken) och deploya.
2. Öppna `https://smgd.se/setup#<nyckeln>`, välj lösenord, skanna QR-koden och skriv in koden.
3. Uppgifterna sparas i Netlify Blobs och `/setup` stängs för gott. Ta bort `PORTAL_SETUP_TOKEN` efteråt.

**Börja om från början:** radera blobben `credentials` i storen `smgd-portal-auth`
(Netlify → Data & Storage → Blobs) och kör installationslänken igen.

### Alternativ: miljövariabler

#### 1. Skapa hemligheterna (på din egen dator)

```bash
cd smgd-portal
npm install
npm run setup
```

Skriptet frågar efter ett lösenord och skriver ut tre miljövariabler, plus en QR-kod för
autentiseringsappen. Lösenordet lämnar aldrig datorn.

#### 2. Lägg in dem i Netlify

Gå till Netlify → projektet för smgd.se → **Project configuration → Environment variables**
och lägg till följande, med scope **Functions** och markerade som **secret**:

| Variabel | Innehåll |
| --- | --- |
| `PORTAL_PASSWORD_HASH` | `pbkdf2-sha256$…` |
| `PORTAL_TOTP_SECRET` | Base32-nyckeln för tvåstegsverifieringen |
| `PORTAL_SESSION_SECRET` | 32 slumpade bytes (base64) |
| `PORTAL_SESSION_VERSION` | Valfri. Standard är `1` |

#### 3. Deploya

Bygginställningar för projektet: *Base directory* `smgd-portal`. Allt annat följer av
`netlify.toml`.

**Byta bara lösenord:** kör `node scripts/setup.mjs --password` och ersätt
`PORTAL_PASSWORD_HASH`.

## Lägga till eller ändra subdomäner

Redigera [`netlify/edge-functions/lib/sites.ts`](netlify/edge-functions/lib/sites.ts).

## Tester

```bash
npm test
```

## Bra att veta

Portalen länkar vidare till subdomänerna, men inloggningen på smgd.se loggar inte in dig
automatiskt på dem. DriveLog och Home Assistant har fortfarande sina egna inloggningar.
