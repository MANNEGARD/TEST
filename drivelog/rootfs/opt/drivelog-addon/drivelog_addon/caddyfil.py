"""Skriver Caddys konfiguration för tillägget.

Samma säkerhetshuvuden och gränser som deploy/Caddyfile (driftsättning på egen server), men
API och webbapp ligger i samma container (127.0.0.1) och Caddys data – certifikat och ACME-
konto – sparas under /data/caddy så att de följer med i Home Assistants säkerhetskopior och inte
begärs på nytt vid varje omstart (Let's Encrypt har gränser för hur ofta det får ske).
"""

from __future__ import annotations

import re

from .kontroll import SOKVAG as DYNDNS_KONTROLLSOKVAG

_UPPSTROM = re.compile(r"^127\.0\.0\.1:[0-9]{1,5}$")


def skapa_caddyfil(
    doman: str,
    *,
    lagring: str = "/data/caddy",
    api: str = "127.0.0.1:8000",
    webb: str = "127.0.0.1:3000",
    kontroll: str | None = None,
) -> str:
    """Returnerar Caddyfile-texten. ``doman`` ska redan vara kontrollerad (installningar).

    ``kontroll`` är adressen till startprogrammets kontrollserver (kontroll.py), t.ex.
    ``127.0.0.1:8099``."""
    if kontroll is not None and not _UPPSTROM.match(kontroll):
        raise ValueError("Ogiltig adress till kontrollservern")
    dyndns_block = ""
    if kontroll:
        # Kontrollen "leder domänen hit?" (DynDNS och AAAA-kontrollen): fråga och svar som
        # startprogrammet räknar ut – Caddy vet inte svaret själv (se kontroll.py).
        dyndns_block = (
            f"\thandle {DYNDNS_KONTROLLSOKVAG} {{\n"
            f"\t\treverse_proxy {kontroll}\n"
            "\t}\n\n"
        )
    return f"""# Skapas av tilläggets startprogram vid varje start – ändringar här skrivs över.
{{
	# Inget administrations-API: konfigurationen ändras bara genom att tillägget startas om.
	admin off
	persist_config off
	# Försök inte installera Caddys lokala rotcertifikat i containerns systemlager.
	skip_install_trust
	storage file_system {{
		root {lagring}
	}}
}}

{doman} {{
	encode zstd gzip

	header {{
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "same-origin"
		Permissions-Policy "geolocation=(), microphone=(), camera=()"
		-Server
	}}

	# Uppladdningar: importfiler är små; begränsa för att skydda servern.
	request_body {{
		max_size 60MB
	}}

{dyndns_block}	handle /api/v1/* {{
		reverse_proxy {api} {{
			# Ersätt (lägg inte till) klientens adress: ett X-Forwarded-For från internet
			# ska aldrig kunna förfalska avsändaren för API:ts gränser mot inloggningsförsök.
			header_up X-Forwarded-For {{remote_host}}
		}}
	}}

	handle {{
		reverse_proxy {webb} {{
			header_up X-Forwarded-For {{remote_host}}
		}}
	}}
}}
"""
