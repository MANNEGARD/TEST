# SMGD DriveLog

Självhostad svensk körjournal för tjänsteresor med egen bil – direkt på din Home Assistant-maskin.

- Hela DriveLog (API, webbapp och HTTPS-proxy) i ett tillägg – ingen separat server.
- Nås från internet på din egen domän, t.ex. `https://drivelog.smgd.se`, med automatiskt
  certifikat från Let's Encrypt.
- Håller domänens adress hos STRATO uppdaterad (DynDNS) om du vill.
- Ingenting öppnas mot internet förrän ett administratörskonto finns.
- Data och nycklar sparas i tilläggets data och följer med i Home Assistants säkerhetskopior.

Kräver Home Assistant OS eller Supervised (tillägg), en domän och att port 80 och 443 i routern
skickas vidare till Home Assistant-maskinen. Läs fliken **Dokumentation** innan du startar.
