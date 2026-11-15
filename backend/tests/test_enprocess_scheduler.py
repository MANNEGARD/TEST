"""Enprocessläget: schemaläggaren i API-processen (app.scheduler)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import time_machine

from app import scheduler as sch

STHLM = ZoneInfo("Europe/Stockholm")


class Recorder:
    """Ersätter de riktiga jobben och räknar körningarna."""

    def __init__(self, clock=None) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self.clock = clock

    def job(self, name: str, *, fail: bool = False, sleep: float = 0.0):
        def fn() -> None:
            self.calls.append((name, self.clock() if self.clock else datetime.now(UTC)))
            if sleep:
                time.sleep(sleep)
            if fail:
                raise RuntimeError(f"{name} gick sönder")

        return fn

    def jobs(self, **overrides):
        out = {n: self.job(n) for n in ("tick", "calendar.sync_due", "maintenance")}
        out.update(overrides)
        return out

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def times(self, name: str) -> list[datetime]:
        return [t for n, t in self.calls if n == name]


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.now

    def advance(self, **delta) -> None:
        with self._lock:
            self.now += timedelta(**delta)


def local(*args) -> datetime:
    return datetime(*args, tzinfo=STHLM).astimezone(UTC)


def step(s: sch.InProcessScheduler, clock: FakeClock, *, minutes: int, every_seconds: int = 30) -> None:
    for _ in range(minutes * 60 // every_seconds):
        clock.advance(seconds=every_seconds)
        s.run_pending(clock())


# ----------------------------------------------------------------------------- klockslag


def test_next_daily_is_strictly_after_now_in_local_time():
    at = sch.MAINTENANCE_AT
    assert sch.next_daily(local(2026, 11, 15, 1, 0), at, STHLM) == local(2026, 11, 15, 2, 17)
    assert sch.next_daily(local(2026, 11, 15, 2, 17), at, STHLM) == local(2026, 11, 16, 2, 17)
    assert sch.next_daily(local(2026, 11, 15, 23, 59), at, STHLM) == local(2026, 11, 16, 2, 17)
    # Sommartid: 02:17 lokal tid är 00:17 UTC; vintertid 01:17 UTC.
    assert sch.next_daily(local(2026, 7, 1, 12, 0), at, STHLM) == datetime(2026, 7, 2, 0, 17, tzinfo=UTC)
    # Natten då klockan ställs fram (02:00 → 03:00) finns inte 02:17: körs ändå samma natt.
    spring = sch.next_daily(local(2027, 3, 27, 12, 0), at, STHLM)
    assert spring.date() == datetime(2027, 3, 28).date() and spring < local(2027, 3, 28, 4, 0)


# ----------------------------------------------------------------------------- schemat


def test_schedule_matches_celery_beat_over_a_day():
    start = local(2026, 11, 15, 10, 0)
    clock = FakeClock(start)
    rec = Recorder(clock)
    s = sch.InProcessScheduler(timezone="Europe/Stockholm", clock=clock, jobs=rec.jobs())
    s.plan(start)
    assert s.run_pending(start + timedelta(seconds=59)) == []
    step(s, clock, minutes=24 * 60)
    assert 1438 <= rec.count("tick") <= 1440
    assert 95 <= rec.count("calendar.sync_due") <= 96
    assert rec.times("maintenance") == [local(2026, 11, 16, 2, 17)]
    ticks = rec.times("tick")
    assert all(timedelta(seconds=60) <= b - a <= timedelta(seconds=90) for a, b in zip(ticks, ticks[1:], strict=False))


def test_maintenance_follows_clock_not_uptime():
    """Startar processen 02:00 körs underhållet redan 02:17 – inte först efter ett dygn."""
    start = local(2026, 11, 15, 2, 0)
    clock = FakeClock(start)
    rec = Recorder(clock)
    s = sch.InProcessScheduler(timezone="Europe/Stockholm", clock=clock, jobs=rec.jobs())
    s.plan(start)
    step(s, clock, minutes=20)
    assert rec.times("maintenance") == [local(2026, 11, 15, 2, 17)]


def test_failing_job_does_not_stop_the_others(caplog):
    start = local(2026, 11, 15, 10, 0)
    clock = FakeClock(start)
    rec = Recorder(clock)
    s = sch.InProcessScheduler(clock=clock, jobs=rec.jobs(tick=rec.job("tick", fail=True)))
    s.plan(start)
    step(s, clock, minutes=20)
    assert rec.count("tick") >= 19 and s.schedule["tick"].failures == rec.count("tick")
    assert rec.count("calendar.sync_due") >= 1  # körs trots att tick fallerar varje gång
    assert "Schemalagt jobb tick misslyckades" in caplog.text


def test_clock_jump_does_not_cause_a_burst():
    """Ställs systemklockan fram (t.ex. NTP efter start) körs inga ikappkörningar."""
    start = local(2026, 11, 15, 10, 0)
    clock = FakeClock(start)
    rec = Recorder(clock)
    s = sch.InProcessScheduler(clock=clock, jobs=rec.jobs())
    s.plan(start)
    clock.advance(hours=5)
    assert s.run_pending(clock()) == ["tick", "calendar.sync_due"]
    assert s.run_pending(clock() + timedelta(seconds=30)) == []
    assert s.schedule["tick"].next_run == clock() + sch.TICK_INTERVAL


def test_overdue_maintenance_is_caught_up_after_restart(tmp_path):
    start = local(2026, 11, 15, 10, 0)
    marker = tmp_path / sch.MARKER_NAME
    # Ingen markör (ny installation): nästa 02:17 som vanligt.
    s = sch.InProcessScheduler(marker_dir=tmp_path, clock=lambda: start, jobs=Recorder().jobs())
    s.plan(start)
    assert s.schedule["maintenance"].next_run == local(2026, 11, 16, 2, 17)
    # Senaste underhållet för två dygn sedan: tas igen strax efter start.
    marker.write_text((start - timedelta(days=2)).isoformat(), encoding="utf-8")
    s.plan(start)
    assert s.schedule["maintenance"].next_run == start + sch.CATCH_UP_DELAY
    # Kört i natt: ingen ikappkörning.
    marker.write_text((start - timedelta(hours=8)).isoformat(), encoding="utf-8")
    s.plan(start)
    assert s.schedule["maintenance"].next_run == local(2026, 11, 16, 2, 17)
    # Körningen skriver markören.
    s.schedule["maintenance"].next_run = start
    s.run_pending(start)
    assert datetime.fromisoformat(marker.read_text(encoding="utf-8")) == start


def test_default_clock_follows_time_machine():
    rec = Recorder()
    with time_machine.travel(local(2026, 11, 16, 2, 16, 30), tick=False) as traveller:
        s = sch.InProcessScheduler(jobs=rec.jobs())
        s.plan()
        traveller.shift(timedelta(seconds=31))
        assert s.run_pending() == ["maintenance"]
        traveller.shift(timedelta(seconds=30))
        assert s.run_pending() == ["tick"]


# ----------------------------------------------------------------------------- tråden


def wait_for(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_thread_runs_jobs_survives_errors_and_stops_cleanly():
    clock = FakeClock(local(2026, 11, 15, 10, 0))
    rec = Recorder(clock)
    jobs = rec.jobs(**{"calendar.sync_due": rec.job("calendar.sync_due", fail=True)})
    s = sch.InProcessScheduler(clock=clock, jobs=jobs, max_sleep=0.02)
    s.start()
    try:
        assert s.running
        for _ in range(5):
            clock.advance(minutes=15)
            assert wait_for(lambda n=rec.count("tick"): rec.count("tick") > n)
        assert rec.count("calendar.sync_due") >= 1 and s.running  # felet stoppade inte tråden
    finally:
        s.stop(timeout=5)
    assert not s.running
    before = len(rec.calls)
    clock.advance(hours=1)
    time.sleep(0.1)
    assert len(rec.calls) == before  # inget körs efter stopp


def test_jobs_never_overlap_and_stop_waits_for_running_job():
    clock = FakeClock(local(2026, 11, 15, 10, 0))
    active = 0
    max_active = 0
    lock = threading.Lock()
    done = []

    def slow() -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.2)
        with lock:
            active -= 1
        done.append(1)

    s = sch.InProcessScheduler(clock=clock, jobs={"tick": slow, "calendar.sync_due": slow, "maintenance": slow},
                               max_sleep=0.01)
    s.start()
    clock.advance(hours=3)  # tick och kalender förfallna samtidigt
    # Ett anrop utifrån samtidigt som tråden kör: väntar på låset i stället för att överlappa.
    outside = threading.Thread(target=s.run_pending)
    outside.start()
    assert wait_for(lambda: len(done) >= 1)
    s.stop(timeout=5)
    outside.join(5)
    assert max_active == 1
    assert not s.running and active == 0  # stop() väntade tills jobbet var klart
