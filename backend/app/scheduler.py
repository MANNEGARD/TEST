"""Schemaläggare i API-processen (enprocessläget, DRIVELOG_JOBS_EAGER=true).

I Compose-driften sköter Celery beat schemat (se ``app/worker/celery_app.py``). När allt
körs i en enda container – Home Assistant-tillägget eller ``scripts/dev.sh`` – finns ingen
beat och ingen worker; då kör den här modulen samma schema i en bakgrundstråd:

* ``tick`` varje minut (fordonsinsamling, notiser, schemalagda rapporter),
* ``calendar.sync_due`` var 15:e minut,
* ``maintenance`` en gång per dygn kl. 02:17 lokal tid (DRIVELOG_TIMEZONE).

Jobben körs ett i taget i schemaläggarens egen tråd, så de kan aldrig överlappa varandra.
Ett fel i ett jobb loggas och stoppar aldrig tråden. Det nattliga underhållet styrs av
klockslaget – inte av hur länge processen varit igång – och om processen var avstängd vid
förra tillfället (och underhållet inte körts på mer än ett dygn) körs det strax efter start.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("drivelog.scheduler")

TICK_INTERVAL = timedelta(minutes=1)
CALENDAR_INTERVAL = timedelta(minutes=15)
MAINTENANCE_AT = time(2, 17)
# Första körningarna efter start: låt API:t komma igång först.
FIRST_TICK_DELAY = timedelta(minutes=1)
FIRST_CALENDAR_DELAY = timedelta(minutes=2)
CATCH_UP_DELAY = timedelta(minutes=5)
# Underhåll som inte körts på så här länge tas igen strax efter start.
MAINTENANCE_OVERDUE = timedelta(hours=26)
MARKER_NAME = ".underhall-senast"
# Tråden vaknar minst så här ofta och läser om klockan (tål att systemklockan ställs om,
# t.ex. när en Raspberry Pi utan batteriklocka får rätt tid via NTP efter start).
MAX_SLEEP_SECONDS = 30.0


def next_daily(now: datetime, at: time, tz: ZoneInfo) -> datetime:
    """Nästa tillfälle (strikt efter ``now``) då klockan är ``at`` i tidszonen ``tz``.

    Hanterar sommar-/vintertid: finns klockslaget inte ett visst dygn (framflyttning)
    används det första giltiga ögonblicket efter det.
    """
    local = now.astimezone(tz)
    candidate_date = local.date()
    for _ in range(3):
        naive = datetime.combine(candidate_date, at)
        candidate = naive.replace(tzinfo=tz)
        # Normalisera via UTC så att en obefintlig lokal tid flyttas fram korrekt.
        candidate = candidate.astimezone(UTC)
        if candidate > now.astimezone(UTC):
            return candidate
        candidate_date += timedelta(days=1)
    raise AssertionError("next_daily hittade inget tillfälle")  # pragma: no cover


@dataclass
class ScheduledJob:
    name: str
    fn: Callable[[], object]
    next_run: datetime
    interval: timedelta | None = None  # None = dagligt klockslag (daily_at)
    daily_at: time | None = None
    runs: int = 0
    failures: int = 0
    last_started: datetime | None = None
    last_finished: datetime | None = None


def _default_jobs() -> dict[str, Callable[[], object]]:
    from app.worker import tasks

    return {"tick": tasks.tick, "calendar.sync_due": tasks.calendar_sync_due, "maintenance": tasks.maintenance}


@dataclass
class InProcessScheduler:
    """Kör DriveLogs schemalagda jobb i en bakgrundstråd.

    ``clock`` och ``jobs`` kan bytas ut i tester; ``run_pending`` kan anropas direkt med en
    given tidpunkt utan att någon tråd startas.
    """

    timezone: str = "Europe/Stockholm"
    marker_dir: Path | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    jobs: dict[str, Callable[[], object]] | None = None
    max_sleep: float = MAX_SLEEP_SECONDS

    def __post_init__(self) -> None:
        self._tz = ZoneInfo(self.timezone)
        self._stop = threading.Event()
        self._run_lock = threading.Lock()  # ett jobb i taget, även om run_pending anropas utifrån
        self._thread: threading.Thread | None = None
        self.schedule: dict[str, ScheduledJob] = {}

    # ------------------------------------------------------------------ schema

    def plan(self, now: datetime | None = None) -> None:
        """Lägger upp schemat utifrån ``now`` (startögonblicket)."""
        now = now or self.clock()
        fns = self.jobs if self.jobs is not None else _default_jobs()
        maintenance_next = next_daily(now, MAINTENANCE_AT, self._tz)
        last = self._read_marker()
        if last is not None and now - last > MAINTENANCE_OVERDUE:
            maintenance_next = min(maintenance_next, now + CATCH_UP_DELAY)
        self.schedule = {
            "tick": ScheduledJob("tick", fns["tick"], now + FIRST_TICK_DELAY, interval=TICK_INTERVAL),
            "calendar.sync_due": ScheduledJob(
                "calendar.sync_due", fns["calendar.sync_due"], now + FIRST_CALENDAR_DELAY, interval=CALENDAR_INTERVAL
            ),
            "maintenance": ScheduledJob(
                "maintenance", fns["maintenance"], maintenance_next, daily_at=MAINTENANCE_AT
            ),
        }

    def _reschedule(self, job: ScheduledJob, finished: datetime) -> None:
        if job.interval is not None:
            nxt = job.next_run + job.interval
            if nxt <= finished:
                # Jobbet tog längre tid än intervallet, eller klockan har hoppat fram:
                # inga ikappkörningar i följd, nästa körning ett helt intervall fram.
                nxt = finished + job.interval
            job.next_run = nxt
        else:
            assert job.daily_at is not None
            job.next_run = next_daily(finished, job.daily_at, self._tz)

    def run_pending(self, now: datetime | None = None) -> list[str]:
        """Kör alla jobb som är förfallna vid ``now``. Returnerar namnen på de körda jobben."""
        if not self.schedule:
            self.plan(now)
        explicit = now is not None
        ran: list[str] = []
        with self._run_lock:
            at = now if now is not None else self.clock()
            for job in list(self.schedule.values()):
                if self._stop.is_set() or job.next_run > at:
                    continue
                self._run(job)
                ran.append(job.name)
                # Nästa körning räknas från när jobbet blev klart (i tråden: klockan efteråt).
                finished = at if explicit else max(at, self.clock())
                self._reschedule(job, finished)
        return ran

    def _run(self, job: ScheduledJob) -> None:
        job.last_started = self.clock()
        job.runs += 1
        try:
            job.fn()
        except Exception:
            job.failures += 1
            logger.exception("Schemalagt jobb %s misslyckades", job.name)
        finally:
            job.last_finished = self.clock()
        if job.name == "maintenance":
            self._write_marker(job.last_finished)

    def seconds_until_next(self, now: datetime | None = None) -> float:
        now = now or self.clock()
        if not self.schedule:
            return 0.0
        soonest = min(j.next_run for j in self.schedule.values())
        return max(0.0, min(self.max_sleep, (soonest - now).total_seconds()))

    # ------------------------------------------------------------------ markör för underhållet

    def _marker(self) -> Path | None:
        return self.marker_dir / MARKER_NAME if self.marker_dir is not None else None

    def _read_marker(self) -> datetime | None:
        path = self._marker()
        if path is None:
            return None
        try:
            value = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def _write_marker(self, when: datetime) -> None:
        path = self._marker()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(when.astimezone(UTC).isoformat(), encoding="utf-8")
        except OSError:
            logger.warning("Kunde inte spara tidpunkten för senaste underhållet.")

    # ------------------------------------------------------------------ tråden

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.plan()
        self._thread = threading.Thread(target=self._loop, name="drivelog-scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception:  # skyddsnät: tråden får aldrig dö
                logger.exception("Schemaläggaren stötte på ett oväntat fel")
            self._stop.wait(self.seconds_until_next() if self.schedule else self.max_sleep)

    def stop(self, timeout: float = 30.0) -> None:
        """Stoppar tråden. Ett jobb som redan körs får gå klart (högst ``timeout`` sekunder)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("Schemaläggaren avslutades inte inom %s s (ett jobb körs fortfarande).", timeout)
        self._thread = None


def from_config() -> InProcessScheduler:
    from app.config import get_config

    cfg = get_config()
    return InProcessScheduler(timezone=cfg.timezone, marker_dir=cfg.data_dir)
