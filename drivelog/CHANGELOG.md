# Ändringar

> Från 1.0.2 visar Home Assistant **Uppdatera** först när den nya versionen är färdigbyggd och
> går att hämta – det går bra att trycka direkt.

## 1.0.3

- **Adressvakt när STRATO DynDNS är avslaget.** Har du satt domänens A-post för hand (t.ex.
  för att DynDNS inte ingår i ditt STRATO-paket) kontrollerar appen nu var 15:e minut att
  domänen fortfarande leder hem. Gör den inte det står det i loggen vad A-posten pekar på och
  vad du ska göra – första gången och sedan högst en gång per dygn.
- Har kontrollen fungerat tidigare och misslyckas tre gånger i rad (cirka 45 minuter) visas
  också en **avisering i Home Assistant**: *DriveLog: &lt;domän&gt; leder inte längre hem*. Den
  försvinner av sig själv när domänen leder hem igen. Appen behöver därför åtkomst till Home
  Assistants API (`homeassistant_api`) – Home Assistant kan be dig godkänna det vid uppdateringen.
- Ingen ny tjänst tillfrågas: kontrollen är densamma som DynDNS-delen använder och går bara till
  din egen domän. Varningen för en AAAA-post körs nu i samma vakt, inte bara en gång vid start.

## 1.0.2

- **Glömt lösenord kan återställas i appen.** Skriv ett nytt lösenord i *Administratörens
  lösenord*, spara och starta om appen: lösenordet sätts för kontot med adressen i
  *Administratörens e-post* (kontot skapas om det saknas), alla inloggade sessioner för kontot
  avslutas och loggen bekräftar det. Fältet tillämpas bara när det har **ändrats** sedan det
  senast användes, så ett lösenord som står kvar skriver aldrig över ett som du har bytt inne i
  DriveLog. Appen sparar bara ett nyckelat kontrollvärde, aldrig lösenordet.
- Ett för kort lösenord eller en felaktig adress i fälten stoppar inte längre appen när kontot
  redan finns – DriveLog startar som vanligt och loggen säger att lösenordet inte ändrades.
- Första gången efter uppdateringen från 1.0.1 ändras inget lösenord om fältet redan är ifyllt
  (appen kan inte veta om det är ett gammalt lösenord). Vill du återställa: skriv ett nytt.
- En ny version erbjuds i Home Assistant först när avbilden är publicerad och går att hämta
  utan inloggning, så installation och uppdatering misslyckas inte längre med
  `manifest unknown` medan en version byggs.

## 1.0.1

- Säkerhet och robusthet efter granskning: första kontot skapas bara från appens inställningar
  (inte via API:t av en annan app), förfalskad `X-Forwarded-For` direkt mot port 8000 kringgår
  inte längre inloggningsspärren, `/data` ägs av root med databasen i `/data/app`, nycklarna
  läses bara säkert och ersätts aldrig tyst när databasen finns, alla fält syns direkt i fliken
  Konfiguration, DynDNS-kontrollen är fråga–svar och godtar bara publika adresser, och
  AAAA-varningen gäller även när routern sköter DynDNS.

## 1.0.0

- Första versionen av SMGD DriveLog som Home Assistant-tillägg: API, webbapp och Caddy
  (automatisk HTTPS via Let's Encrypt) i en container, med SQLite och bakgrundsjobben i API:t.
- Webbapp och proxy startas först när ett administratörskonto finns.
- Nycklarna skapas slumpmässigt första gången och sparas i tilläggets data (följer med i
  Home Assistants säkerhetskopior).
- Valfri STRATO DynDNS-uppdatering som bara frågar STRATO, med varning för AAAA-poster.
- Färdiga avbilder för amd64 och aarch64.
