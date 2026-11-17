"""Skriver Caddys konfiguration för tillägget.

Samma säkerhetshuvuden och gränser som deploy/Caddyfile (driftsättning på egen server), men
API och webbapp ligger i samma container (127.0.0.1) och Caddys data – certifikat och ACME-
konto – sparas under /data/caddy så att de följer med i Home Assistants säkerhetskopior och inte
begärs på nytt vid varje omstart (Let's Encrypt har gränser för hur ofta det får ske).

Extra domäner (inställningen ``extra_domains``) får egna block som skickar vidare till en annan
container, t.ex. transcribe.smgd.se → transcribe:8080 på Docker-nätet ``smgd`` (deploy/server.sh).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .installningar import ar_forbjuden_vard
from .kontroll import SOKVAG as DYNDNS_KONTROLLSOKVAG

_UPPSTROM = re.compile(r"^127\.0\.0\.1:[0-9]{1,5}$")
# Extra domäner (installningar.ExtraDoman) skickas till en annan container på Dockers nät, så
# värden får vara ett namn – men samma strikta tecken som i installningar, så att ingenting kan
# skjutas in i Caddyfilen (mellanslag, klamrar, radbrytningar, citattecken). Värdar som är DriveLog
# självt, IP-adresser och värdmaskinen avvisas också här (installningar.ar_forbjuden_vard).
_EXTRA_DOMAN = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_EXTRA_UPPSTROM = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}:([0-9]{1,5})$")


def _extra_block(doman: str, uppstrom: str) -> str:
    """Ett eget webbplatsblock för en extra domän. Bara de allmänna säkerhetshuvudena: varken
    DriveLogs Permissions-Policy (tjänsten bakom kan behöva mikrofon och skärmdelning) eller
    X-Frame-Options – det avgör tjänsten själv. WebSockets fungerar som de är i reverse_proxy."""
    port = _EXTRA_UPPSTROM.fullmatch(uppstrom)
    if not _EXTRA_DOMAN.fullmatch(doman) or ".." in doman:
        raise ValueError("Ogiltig extra domän")
    if port is None or not 1 <= int(port.group(1)) <= 65535:
        raise ValueError("Ogiltig adress till tjänsten bakom en extra domän")
    if ar_forbjuden_vard(uppstrom.rpartition(":")[0]):
        raise ValueError("Tjänsten bakom en extra domän får inte vara DriveLog självt, en IP-adress eller värden")
    return f"""
{doman} {{
	encode zstd gzip

	header {{
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "same-origin"
		-Server
	}}

	request_body {{
		max_size 50MB
	}}

	reverse_proxy {uppstrom} {{
		header_up X-Forwarded-For {{remote_host}}
	}}
}}
"""


def skapa_caddyfil(
    doman: str,
    *,
    lagring: str = "/data/caddy",
    api: str = "127.0.0.1:8000",
    webb: str = "127.0.0.1:3000",
    kontroll: str | None = None,
    extra: Iterable[tuple[str, str]] = (),
) -> str:
    """Returnerar Caddyfile-texten. ``doman`` ska redan vara kontrollerad (installningar).

    ``kontroll`` är adressen till startprogrammets kontrollserver (kontroll.py), t.ex.
    ``127.0.0.1:8099``. ``extra`` är par av (domän, ``värd:port``) för extra domäner som
    skickas vidare till andra tjänster (inställningen ``extra_domains``)."""
    if kontroll is not None and not _UPPSTROM.fullmatch(kontroll):
        raise ValueError("Ogiltig adress till kontrollservern")
    extra_block = "".join(_extra_block(d, u) for d, u in extra)
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
{extra_block}"""
