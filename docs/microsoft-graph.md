# Microsoft Outlook (Microsoft Graph)

DriveLog läser din Outlook-kalender för att ge resorna kontext: ett möte nära resans
ankomst, på en plats som stämmer med målet, är en stark indikation på tjänsteresa och ger
ett förslag till syfte. Kalendern är en informationskälla – aldrig ett bevis och aldrig en
instruktion till systemet.

## Vad som krävs

- Ett Microsoft-konto (privat eller arbets-/skolkonto).
- En appregistrering i Microsoft Entra (kostnadsfri).
- För arbetskonton: att organisationen tillåter användare att ge samtycke till appar som
  läser kalendern. Kräver organisationen administratörsgodkännande behöver IT godkänna appen.

## 1. Registrera appen

1. Gå till [entra.microsoft.com](https://entra.microsoft.com) → *Identity* → *Applications* →
   *App registrations* → *New registration*.
2. **Name:** t.ex. `SMGD DriveLog`.
3. **Supported account types:** *Accounts in any organizational directory and personal
   Microsoft accounts* (då fungerar både privata och arbetskonton).
4. **Redirect URI:** plattform *Web* och adressen som visas i DriveLog under
   *Integrationer → Microsoft Outlook*, normalt
   `https://<din-domän>/api/v1/integrations/microsoft/callback`.
5. Registrera. Kopiera **Application (client) ID**.
6. *Certificates & secrets* → *New client secret*. Kopiera **Value** direkt (visas bara en gång).
7. *API permissions* → *Add a permission* → *Microsoft Graph* → *Delegated permissions* →
   lägg till `Calendars.Read` och `offline_access`. (`openid` och `email` begärs också vid
   inloggningen; de är standardbehörigheter och kräver inget administratörsgodkännande.)

## 2. Anslut i DriveLog

1. *Integrationer → Microsoft Outlook* → fyll i applikations-ID och klienthemlighet. Tenant
   lämnas som `common` (eller ange organisationens tenant-id om appen bara ska fungera där).
2. *Anslut* → logga in hos Microsoft och godkänn.
3. Välj vilka kalendrar som ska användas och klicka *Synka nu*.

## Behörigheter

| Behörighet | Varför |
| --- | --- |
| `Calendars.Read` | Läsa rubrik, tid, plats och onlinemötesflagga. Endast läsning |
| `offline_access` | Förnya åtkomsten utan ny inloggning |
| `openid`, `email` | Visa vilket konto som är anslutet |

Inget skrivs till kalendern. Händelsernas brödtext hämtas inte alls om du inte slår på
*Spara förhandsvisning av brödtext* under *Inställningar → Kalender*.

## Hur matchningen fungerar

En bokning kopplas till en resa bara när **både** tid och plats talar för det:

- **Tid:** resan anländer från två timmar före till 45 minuter efter mötets start (starkast
  inom en halvtimme före), anländer under mötet, eller avgår i anslutning till mötets slut
  (stöd för återresor). Fönstren är inställbara.
- **Plats:** koordinater om de finns, annars jämförs platsnamn, företag, gata och ort –
  egna förkortningar (*Inställningar → Förkortningar*) expanderas först.

Ett möte samma dag men långt från resan i tid matchas inte. Onlinemöten väger för lätt för
att ensamma ge en träff. Heldagsposter, inställda möten och poster markerade som *Ledig*
ignoreras. Privata bokningar (känslighet *Privat*) talar för privat resa.

Har en bokning en fysisk plats på en **annan ort** samtidigt som resan ankommer någon
annanstans skapas ärendet *Konflikt mellan kalender och fordonsdata* i granskningscentret,
och resan klassas inte automatiskt.

I resedetaljen visas alla kandidater med poäng. Du kan välja en annan träff eller avvisa
den föreslagna – ditt val behålls vid senare synk.

## Drift

- Synk sker var 15:e minut i bakgrunden (60 dagar bakåt, 14 dagar framåt – inställbart).
- Åtkomsttoken förnyas automatiskt; den nya uppdateringstoken sparas varje gång. Tokens
  lagras krypterade.
- Vid 429 från Microsoft respekteras `Retry-After`.
- Återkallas åtkomsten visas *Behöver anslutas på nytt* – klicka *Anslut* igen.
- *Koppla bort* raderar tokens och alla lagrade kalenderposter.

## Inte verifierat mot verklig tjänst

Flödet är testat mot simulerade Graph-svar (OAuth med PKCE, sidindelning, tokenrotation,
`invalid_grant`, throttling). Inloggning mot en riktig tenant, organisationers
samtyckespolicyer och privata Microsoft-kontons tokenlivslängd har inte kunnat prövas utan
dina uppgifter.
