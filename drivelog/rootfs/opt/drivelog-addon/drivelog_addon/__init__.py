"""Startprogram för Home Assistant-tillägget SMGD DriveLog.

Modulerna använder bara Pythons standardbibliotek, så att de kan provas utan Docker och
utan backendens beroenden:

- ``installningar`` läser och kontrollerar /data/options.json,
- ``hemligheter`` skapar nycklarna första gången och återanvänder dem sedan,
- ``caddyfil`` skriver proxyns konfiguration,
- ``dyndns`` håller domänens adress hos STRATO uppdaterad,
- ``startprogram`` startar och övervakar API, webbapp och proxy.
"""

ADDON_NAMN = "SMGD DriveLog"
