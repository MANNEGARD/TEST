# Ändringar

> Home Assistant visar **Uppdatera** så fort en ny version finns i repot, men avbilden byggs
> först efteråt. Vänta tills arbetsflödet *Home Assistant-tillägg* under repots Actions är grönt
> innan du trycker **Uppdatera**.

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
