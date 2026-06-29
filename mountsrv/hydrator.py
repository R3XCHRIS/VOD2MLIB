"""Background size-hydration.

The mount only surfaces titles whose size Dispatcharr already knows (stored bitrate).
This drips Dispatcharr's own refresh tasks so the rest fill in over time — movie
detail (``refresh_movie_advanced_data``) and un-hydrated series
(``batch_refresh_series_episodes``) — paced by a rate limit and run on a schedule
(full pass on load, then at the configured off-peak times). Convention mirrors the
PiratesIRC plugins: a daemon thread (no Celery beat), HHMM ``scheduled_times`` +
timezone + a rate limit.

All config arrives via env (set by the plugin's enable action):
  VOD2MLIB_HYDRATE_CONCURRENCY  parallel get_vod_info fetches; 0 disables (default 8)
  VOD2MLIB_HYDRATE_ON_LOAD      "true" => full pass when the server starts (default true)
  VOD2MLIB_HYDRATE_TIMES        comma-separated HHMM, e.g. "0300,1500" (default "")
  VOD2MLIB_HYDRATE_TZ           IANA tz for the times (default America/Chicago)
  VOD2MLIB_HYDRATE_MAX_MINUTES  cap one pass to this many minutes (default 240; 0=off)
"""
import os
import time
import logging
import threading
import datetime

from vod2mlib_core.config import (
    ENV_HYDRATE_CONCURRENCY, ENV_HYDRATE_ON_LOAD, ENV_HYDRATE_TIMES,
    ENV_HYDRATE_TZ, ENV_HYDRATE_MAX_MINUTES,
)

logger = logging.getLogger("vod2mlib.hydrator")


def _parse_times(raw):
    out = []
    for tok in (raw or "").replace(" ", "").split(","):
        if len(tok) == 4 and tok.isdigit():
            h, m = int(tok[:2]), int(tok[2:])
            if 0 <= h < 24 and 0 <= m < 60:
                out.append((h, m))
    return out


class Hydrator:
    def __init__(self):
        # Parallel get_vod_info fetches. The work is network-bound (~0.5s/round-trip),
        # so concurrency — not a per-second rate — is what determines throughput.
        # 0 disables hydration. This is also the provider-load throttle (N in flight).
        self.concurrency = max(0, int(os.environ.get(ENV_HYDRATE_CONCURRENCY, "8") or 0))
        self.on_load = os.environ.get(ENV_HYDRATE_ON_LOAD, "true").lower() == "true"
        self.times = _parse_times(os.environ.get(ENV_HYDRATE_TIMES, ""))
        self.tz = os.environ.get(ENV_HYDRATE_TZ, "America/Chicago")
        # A pass stops after this many minutes so a scheduled run can't bleed into
        # peak hours; it just resumes (where it left off) on the next scheduled time.
        # 0 = unbounded. Advanced env knob, not a UI field.
        self.max_minutes = float(os.environ.get(ENV_HYDRATE_MAX_MINUTES, "240") or 0)
        self._deadline = None
        self._thread = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._run_now = threading.Event()
        self._running = False
        self._last = None  # (reason, ts, movies, series)

    # --- lifecycle ---------------------------------------------------------------
    def start(self):
        if self.concurrency <= 0:
            logger.info("Hydration disabled (concurrency=0)")
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="vod2mlib-hydrator")
            self._thread.start()
            logger.info("Hydrator started: concurrency=%s on_load=%s times=%s tz=%s",
                        self.concurrency, self.on_load, self.times, self.tz)

    def stop(self):
        self._stop.set()
        self._run_now.set()

    def trigger_now(self):
        """Request an immediate pass (used by the 'Hydrate Now' action)."""
        if self.concurrency <= 0:
            return False
        self.start()                      # ensure the thread exists
        self._run_now.set()
        return True

    def status(self):
        nxt = self._next_run_iso()
        last = None
        if self._last:
            reason, ts, nm, ns = self._last
            last = {"reason": reason, "at": datetime.datetime.utcfromtimestamp(ts)
                    .replace(microsecond=0).isoformat() + "Z", "movies": nm, "series": ns}
        return {
            "enabled": self.concurrency > 0,
            "running": self._running,
            "concurrency": self.concurrency,
            "scheduled_times": ["%02d%02d" % t for t in self.times],
            "timezone": self.tz,
            "next_run": nxt,
            "last_pass": last,
        }

    # --- scheduling --------------------------------------------------------------
    def _now(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.datetime.now(ZoneInfo(self.tz))
        except Exception:
            return datetime.datetime.now(datetime.timezone.utc)

    def _next_run_iso(self):
        if not self.times:
            return None
        now = self._now()
        cands = []
        for h, m in self.times:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            cands.append(t)
        return min(cands).isoformat(timespec="minutes")

    def _loop(self):
        if self.on_load:
            self._run_pass("on-load")
        last_minute = None
        while not self._stop.is_set():
            triggered = self._run_now.wait(timeout=20)
            if self._stop.is_set():
                break
            if triggered:
                self._run_now.clear()
                self._run_pass("manual")
                continue
            now = self._now()
            key = (now.hour, now.minute)
            if key in self.times and last_minute != key:
                last_minute = key
                self._run_pass("scheduled")

    # --- the actual work ---------------------------------------------------------
    def _run_pass(self, reason):
        if self._running:
            return
        self._running = True
        self._deadline = (time.time() + self.max_minutes * 60) if self.max_minutes else None
        try:
            from django.db import close_old_connections
            close_old_connections()
            nm = self._drip_movies()
            ns = self._drip_series()
            capped = self._deadline is not None and time.time() >= self._deadline
            self._last = (reason + (" (time-capped)" if capped else ""),
                          time.time(), nm, ns)
            logger.info("Hydration pass (%s) done: %d movies, %d series refreshed%s",
                        reason, nm, ns, " [time-capped]" if capped else "")
        except Exception:
            logger.exception("Hydration pass failed")
        finally:
            try:
                from django.db import close_old_connections
                close_old_connections()
            except Exception:
                pass
            self._running = False

    def _expired(self):
        """Stop the current pass on shutdown or once the time cap is reached."""
        return self._stop.is_set() or (
            self._deadline is not None and time.time() >= self._deadline)

    def _drip_movies(self):
        try:
            from apps.vod.models import M3UMovieRelation
            from apps.vod.tasks import refresh_movie_advanced_data
            from .tree import _enabled, _MOVIE_SIZED
        except ImportError:
            try:
                from tree import _enabled, _MOVIE_SIZED  # noqa: F401
            except ImportError:
                return 0
            from apps.vod.models import M3UMovieRelation
            from apps.vod.tasks import refresh_movie_advanced_data
        base = M3UMovieRelation.objects.filter(**_enabled())
        all_ids = set(base.values_list("id", flat=True))
        sized = set(base.filter(**_MOVIE_SIZED).values_list("id", flat=True))
        todo = sorted(all_ids - sized)          # set-diff: JSON exclude() misses nulls

        def one(rid):
            from django.db import close_old_connections
            close_old_connections()
            try:
                refresh_movie_advanced_data(rid)   # self-throttled to 24h by Dispatcharr
                return 1
            finally:
                close_old_connections()
        return self._parallel(todo, one)

    def _parallel(self, items, work_one):
        """Run work_one(item) over items with bounded concurrency, deadline-aware.

        The work is network-bound (one get_vod_info round-trip each), so N in flight
        gives ~N x the throughput of a serial loop. ``concurrency`` is both the speed
        knob and the provider-load cap (never more than N requests outstanding).
        Returns the summed count work_one reports."""
        from concurrent.futures import ThreadPoolExecutor
        done = [0]
        lock = threading.Lock()

        def run(item):
            if self._expired():
                return
            try:
                k = work_one(item)
                if k:
                    with lock:
                        done[0] += k
            except Exception:
                logger.debug("hydration item failed", exc_info=True)

        if not items:
            return 0
        chunk = max(1, self.concurrency * 4)
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            for i in range(0, len(items), chunk):
                if self._expired():
                    break
                list(ex.map(run, items[i:i + chunk]))   # bounded in-flight per chunk
        return done[0]

    def _drip_series(self):
        try:
            from apps.vod.models import M3USeriesRelation
            from apps.vod.tasks import batch_refresh_series_episodes
            from .tree import _enabled
        except ImportError:
            try:
                from tree import _enabled  # noqa: F401
            except ImportError:
                return 0
            from apps.vod.models import M3USeriesRelation
            from apps.vod.tasks import batch_refresh_series_episodes
        # un-hydrated series (no episodes yet), grouped by account for the batch task
        rels = (M3USeriesRelation.objects.filter(**_enabled())
                .exclude(series__episodes__isnull=False)
                .values_list("m3u_account_id", "series_id").distinct())
        by_account = {}
        for acct_id, sid in rels:
            by_account.setdefault(acct_id, []).append(sid)
        tasks = []
        for acct_id, sids in by_account.items():
            for i in range(0, len(sids), 50):     # 50-series batch calls
                tasks.append((acct_id, sids[i:i + 50]))

        def one(t):
            acct_id, chunk = t
            from django.db import close_old_connections
            close_old_connections()
            try:
                batch_refresh_series_episodes(acct_id, chunk)
                return len(chunk)
            finally:
                close_old_connections()
        return self._parallel(tasks, one)
