"""Startar och övervakar DriveLog i tilläggets enda container.

Ordning:

1. Inställningarna läses och kontrolleras, nycklarna skapas eller läses in.
2. API:t startas (databasmigrering, sedan uvicorn på port 8000) och startprogrammet väntar
   på hälsokontrollen.
3. ``/api/v1/auth/setup-status`` avgör om ett konto finns. Finns inget – och kunde inget skapas
   från inställningarna – startas varken webbapp eller proxy: ingenting blir då nåbart från
   internet utan konto. API:t fortsätter att gå (bara på Home Assistants interna nät) så att
   tillägget inte startas om i en slinga; loggen säger vad ägaren ska fylla i.
4. Webbappen (bara på 127.0.0.1) och sedan Caddy (80/443, automatisk HTTPS) startas, och
   DynDNS-uppdateraren om den är påslagen.
5. Dör någon av processerna avslutas allt med felkod så att Supervisor startar om tillägget.
   SIGTERM ger en ren nedstängning i omvänd ordning.

Processerna körs som var sin icke-root-användare. Caddy får bara förmågan att binda port
80/443 (CAP_NET_BIND_SERVICE som "ambient capability" via setpriv) – inget annat.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import caddyfil, hemligheter, installningar
from .dyndns import Uppdaterare

logger = logging.getLogger("drivelog.addon")

API_PORT = 8000
WEBB_PORT = 3000

ANVANDARE_API = "drivelog"
ANVANDARE_WEBB = "drivelog-web"
ANVANDARE_CADDY = "caddy"


@dataclass
class Sokvagar:
    data: Path = Path("/data")
    runtime: Path = Path("/run/drivelog")
    app: Path = Path("/app")
    webb: Path = Path("/opt/drivelog/web")
    api_start: tuple[str, ...] = ("/usr/local/bin/docker-entrypoint.sh", "api")
    node: str = "/usr/local/bin/node"
    caddy: str = "/usr/local/bin/caddy"
    path: str = "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"

    @property
    def options(self) -> Path:
        return self.data / "options.json"

    @property
    def caddy_data(self) -> Path:
        return self.data / "caddy"


class Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Starta = Callable[..., Process]
HamtaJson = Callable[[str, float], tuple[int, dict | None]]


CAP_NET_BIND_SERVICE = 10


def har_cap_net_bind_service(status: Path = Path("/proc/self/status")) -> bool:
    """Finns CAP_NET_BIND_SERVICE i containerns begränsningsmängd (Dockers standard)?"""
    try:
        for rad in status.read_text().splitlines():
            if rad.startswith("CapBnd:"):
                return bool(int(rad.split()[1], 16) >> CAP_NET_BIND_SERVICE & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


def lagsta_oprivilegierade_port(path: Path = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")) -> int:
    """Docker sätter normalt 0 i containerns nätverksnamnrymd (då behövs ingen förmåga alls)."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 1024


def starta_process(
    namn: str,
    kommando: list[str],
    *,
    miljo: dict[str, str],
    katalog: Path | None,
    anvandare: str | None,
    lag_portar: bool = False,
) -> Process:
    """Startar en process; som root byts användare med setpriv (util-linux)."""
    if anvandare and os.geteuid() == 0:
        prefix = [
            "setpriv",
            f"--reuid={anvandare}",
            f"--regid={anvandare}",
            "--init-groups",
            "--no-new-privs",
        ]
        if lag_portar and har_cap_net_bind_service():
            prefix += ["--inh-caps=+net_bind_service", "--ambient-caps=+net_bind_service"]
        elif lag_portar and lagsta_oprivilegierade_port() > 80:
            logger.error(
                "Containern saknar CAP_NET_BIND_SERVICE och tillåter inte port 80/443 utan den – "
                "Caddy kommer inte att kunna starta."
            )
        kommando = [*prefix, "--", *kommando]
    logger.debug("Startar %s", namn)
    return subprocess.Popen(  # noqa: S603 - fasta kommandon, ingen skalinterpretation
        kommando, env=miljo, cwd=str(katalog) if katalog else None, umask=0o027
    )


def hamta_json(url: str, timeout: float) -> tuple[int, dict | None]:
    """GET mot en lokal adress. (0, None) om ingen svarar."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as svar:  # noqa: S310 - bara 127.0.0.1
            text = svar.read(65536)
            status = svar.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


@dataclass
class Startprogram:
    inst: installningar.Installningar
    nycklar: hemligheter.Hemligheter
    sokvagar: Sokvagar = field(default_factory=Sokvagar)
    starta: Starta = starta_process
    hamta: HamtaJson = hamta_json
    byt_anvandare: bool = True
    intervall: float = 2.0
    api_tidsgrans: float = 600.0
    webb_tidsgrans: float = 180.0
    stopp_tidsgrans: float = 8.0
    tidszon: str = field(default_factory=lambda: os.environ.get("TZ") or "Europe/Stockholm")
    version: str = field(default_factory=lambda: os.environ.get("DRIVELOG_ADDON_VERSION", "okänd"))
    processer: dict[str, Process] = field(default_factory=dict)
    lage: str = "startar"
    dyndns_token: str | None = None
    _dyndns_trad: threading.Thread | None = None

    # Miljöer ------------------------------------------------------------------------------------

    def _grundmiljo(self, hem: str) -> dict[str, str]:
        return {
            "PATH": self.sokvagar.path,
            "LANG": "C.UTF-8",
            "TZ": self.tidszon,
            "HOME": hem,
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def miljo_api(self) -> dict[str, str]:
        """Kontraktet mellan tillägget och backend (se docs/home-assistant-tillagg.md)."""
        d = str(self.sokvagar.data)
        miljo = self._grundmiljo(hem=d)
        miljo.update(
            {
                "DRIVELOG_ENVIRONMENT": "production",
                "DRIVELOG_DATABASE_URL": f"sqlite:///{d}/drivelog.sqlite3",
                "DRIVELOG_DATA_DIR": d,
                "DRIVELOG_JOBS_EAGER": "true",
                "DRIVELOG_REDIS_URL": "",
                "DRIVELOG_BASE_URL": f"https://{self.inst.domain}",
                "DRIVELOG_SECRET_KEY": self.nycklar.secret_key,
                "DRIVELOG_ENCRYPTION_KEYS": self.nycklar.encryption_keys,
                "DRIVELOG_TIMEZONE": self.tidszon,
                "DRIVELOG_LOG_LEVEL": self.inst.log_level.upper(),
                "FORWARDED_ALLOW_IPS": "127.0.0.1",
            }
        )
        if self.inst.har_adminuppgifter:
            miljo["DRIVELOG_FIRST_ADMIN_EMAIL"] = self.inst.admin_email or ""
            miljo["DRIVELOG_FIRST_ADMIN_PASSWORD"] = self.inst.admin_password or ""
        return miljo

    def miljo_webb(self) -> dict[str, str]:
        miljo = self._grundmiljo(hem="/tmp")  # noqa: S108 - webbappen sparar inget eget
        miljo.update(
            {
                "NODE_ENV": "production",
                "NEXT_TELEMETRY_DISABLED": "1",
                "PORT": str(WEBB_PORT),
                # Bara inifrån containern – utifrån nås webbappen via Caddy.
                "HOSTNAME": "127.0.0.1",
            }
        )
        return miljo

    def miljo_caddy(self) -> dict[str, str]:
        hem = str(self.sokvagar.caddy_data)
        miljo = self._grundmiljo(hem=hem)
        miljo.update({"XDG_DATA_HOME": f"{hem}/data", "XDG_CONFIG_HOME": f"{hem}/config"})
        return miljo

    # Förberedelser ------------------------------------------------------------------------------

    def forbered_data(self) -> None:
        """Rättigheter i /data (bara som root): API-användaren äger databasen och sina kataloger,
        Caddy sin egen katalog; options.json och nycklarna kan bara root läsa."""
        data = self.sokvagar.data
        self.sokvagar.runtime.mkdir(parents=True, exist_ok=True)
        self.sokvagar.caddy_data.mkdir(parents=True, exist_ok=True)
        if not (self.byt_anvandare and os.geteuid() == 0):
            return
        api = pwd.getpwnam(ANVANDARE_API)
        caddy = pwd.getpwnam(ANVANDARE_CADDY)
        if self.sokvagar.options.exists():
            os.chmod(self.sokvagar.options, 0o600)
        os.chown(data, api.pw_uid, api.pw_gid)
        # Klistrig katalog: API-användaren kan skapa filer men inte byta namn på eller ta bort
        # filer som ägs av root (options.json, hemligheter/) eller av Caddy.
        os.chmod(data, 0o1711)  # noqa: S103 - bara genomgång för andra, ingen läsrätt
        for namn in os.listdir(data):
            if namn.startswith("drivelog.sqlite3") or namn in ("imports", "exports", "backups"):
                _chown_rekursivt(data / namn, api.pw_uid, api.pw_gid)
        _chown_rekursivt(self.sokvagar.caddy_data, caddy.pw_uid, caddy.pw_gid)
        os.chmod(self.sokvagar.caddy_data, 0o700)
        os.chmod(self.sokvagar.runtime, 0o755)  # noqa: S103 - bara Caddyfile, ingen hemlighet

    def skriv_caddyfil(self) -> Path:
        if self.inst.strato_dyndns:
            self.dyndns_token = secrets.token_urlsafe(24)
        text = caddyfil.skapa_caddyfil(
            self.inst.domain,
            lagring=str(self.sokvagar.caddy_data),
            api=f"127.0.0.1:{API_PORT}",
            webb=f"127.0.0.1:{WEBB_PORT}",
            dyndns_token=self.dyndns_token,
        )
        path = self.sokvagar.runtime / "Caddyfile"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o644)
        return path

    # Start och väntan ---------------------------------------------------------------------------

    def _starta(self, namn: str, kommando: list[str], miljo, katalog, anvandare, lag_portar=False) -> None:
        self.processer[namn] = self.starta(
            namn,
            kommando,
            miljo=miljo,
            katalog=katalog,
            anvandare=anvandare if self.byt_anvandare else None,
            lag_portar=lag_portar,
        )

    def _dog(self) -> str | None:
        for namn, proc in self.processer.items():
            kod = proc.poll()
            if kod is not None:
                return f"{namn} avslutades oväntat (kod {kod})"
        return None

    def _vanta_pa(self, url: str, tidsgrans: float, stopp: threading.Event, beskrivning: str) -> bool:
        slut = time.monotonic() + tidsgrans
        while not stopp.is_set():
            status, _ = self.hamta(url, 5)
            if status == 200:
                return True
            fel = self._dog()
            if fel:
                logger.error("%s innan %s blev klar.", fel, beskrivning)
                return False
            if time.monotonic() > slut:
                logger.error("%s svarade inte inom %d sekunder.", beskrivning, tidsgrans)
                return False
            stopp.wait(self.intervall)
        return False

    def behover_konto(self) -> bool | None:
        status, data = self.hamta(f"http://127.0.0.1:{API_PORT}/api/v1/auth/setup-status", 10)
        if status != 200 or not isinstance(data, dict) or "needs_setup" not in data:
            return None
        return bool(data["needs_setup"])

    def starta_dyndns(self, stopp: threading.Event) -> None:
        if not self.inst.strato_dyndns or not self.dyndns_token:
            logger.info("STRATO DynDNS är avslaget – domänens adress sköts någon annanstans (t.ex. av routern).")
            return
        uppdaterare = Uppdaterare(
            self.inst.domain,
            self.inst.strato_dyndns_user or "",
            self.inst.strato_dyndns_password or "",
            self.dyndns_token,
            version=self.version,
        )
        self._dyndns_trad = threading.Thread(target=uppdaterare.kor, args=(stopp,), name="dyndns", daemon=True)
        self._dyndns_trad.start()

    # Huvudflödet --------------------------------------------------------------------------------

    def kor(self, stopp: threading.Event) -> int:
        try:
            return self._kor(stopp)
        finally:
            self.stang_ned()

    def _kor(self, stopp: threading.Event) -> int:
        logger.info("SMGD DriveLog-tillägget %s startar för https://%s", self.version, self.inst.domain)
        for varning in self.inst.varningar:
            logger.warning(varning)
        for namn in self.nycklar.nyskapade:
            logger.info("%s skapades och sparades i %s (rättigheter 600).", namn, self.sokvagar.data / hemligheter.KATALOG)
        self.forbered_data()

        self._starta("API", list(self.sokvagar.api_start), self.miljo_api(), self.sokvagar.app, ANVANDARE_API)
        if not self._vanta_pa(
            f"http://127.0.0.1:{API_PORT}/api/v1/health/ready", self.api_tidsgrans, stopp, "API:t"
        ):
            return 0 if stopp.is_set() else 1
        logger.info(
            "API:t är redo. Home Assistant-integrationen når det på http://%s:%d (internt nät).",
            socket.gethostname(),
            API_PORT,
        )

        behov = self.behover_konto()
        if behov is None:
            logger.error("API:t svarade inte som väntat på /api/v1/auth/setup-status.")
            return 1
        if behov:
            self.lage = "sparrad"
            if self.inst.har_adminuppgifter:
                logger.error(
                    "Kontot kunde inte skapas från inställningarna – se API:ts logg ovan. "
                    "Webbappen och proxyn startas inte."
                )
            else:
                logger.warning(
                    "Det finns inget konto i DriveLog än. Fyll i 'admin_email' och 'admin_password' "
                    "(minst 12 tecken) under tilläggets Konfiguration och starta om tillägget. Tills dess "
                    "startas varken webbappen eller proxyn – ingenting är öppet mot internet."
                )
        else:
            if self.inst.admin_password:
                logger.info(
                    "Kontot finns. 'admin_password' behövs inte längre – du kan tömma fältet i inställningarna."
                )
            self._starta(
                "webbappen",
                [self.sokvagar.node, "server.js"],
                self.miljo_webb(),
                self.sokvagar.webb,
                ANVANDARE_WEBB,
            )
            if not self._vanta_pa(
                f"http://127.0.0.1:{WEBB_PORT}/api/healthz", self.webb_tidsgrans, stopp, "Webbappen"
            ):
                return 0 if stopp.is_set() else 1
            caddyfile = self.skriv_caddyfil()
            self._starta(
                "Caddy",
                [self.sokvagar.caddy, "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
                self.miljo_caddy(),
                self.sokvagar.caddy_data,
                ANVANDARE_CADDY,
                lag_portar=True,
            )
            self.lage = "igang"
            logger.info(
                "DriveLog startas på https://%s. Första gången hämtar Caddy ett certifikat från Let's "
                "Encrypt – det kräver att domänen pekar hit och att port 80 och 443 når Home Assistant.",
                self.inst.domain,
            )
            self.starta_dyndns(stopp)

        while not stopp.is_set():
            fel = self._dog()
            if fel:
                logger.error("%s – tillägget stängs av så att Home Assistant kan starta om det.", fel)
                return 1
            stopp.wait(self.intervall)
        logger.info("Stänger ned DriveLog.")
        return 0

    def stang_ned(self) -> None:
        for namn in reversed(list(self.processer)):
            proc = self.processer[namn]
            if proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=self.stopp_tidsgrans)
            except subprocess.TimeoutExpired:
                logger.warning("%s stängde inte inom %d sekunder och avbryts.", namn, self.stopp_tidsgrans)
                proc.kill()
                proc.wait()


def _chown_rekursivt(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)
    if path.is_dir() and not path.is_symlink():
        for rot, kataloger, filer in os.walk(path):
            for namn in kataloger + filer:
                os.chown(os.path.join(rot, namn), uid, gid, follow_symlinks=False)


def main(sokvagar: Sokvagar | None = None) -> int:
    sokvagar = sokvagar or Sokvagar()
    logging.basicConfig(format="[drivelog-tillägg] %(levelname)s %(message)s", level=logging.INFO, force=True)
    stopp = threading.Event()

    def _signal(signum, _frame) -> None:
        logger.info("Fick %s.", signal.Signals(signum).name)
        stopp.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    try:
        inst = installningar.las(sokvagar.options)
    except installningar.OgiltigaInstallningar as exc:
        logger.error("Inställningarna behöver rättas: %s", exc)
        return 1
    logging.getLogger().setLevel(inst.python_loggniva)
    try:
        sokvagar.data.mkdir(parents=True, exist_ok=True)
        nycklar = hemligheter.las_eller_skapa(sokvagar.data)
    except (hemligheter.TrasigaHemligheter, OSError) as exc:
        logger.error("%s", exc)
        return 1
    return Startprogram(inst, nycklar, sokvagar=sokvagar).kor(stopp)
