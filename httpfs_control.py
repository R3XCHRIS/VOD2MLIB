"""HTTP-filesystem (rclone/Plex mount) control plane for VOD2MLIB.

This is the opt-in second output mode. The classic .strm/.nfo generator (plugin.py)
is untouched and still the default; everything here only runs when the user clicks
one of the [MOUNT] actions.

It manages a standalone ASGI child server (the ``mountsrv/`` subpackage, vendored from
VODFS — MIT, Copyright (c) 2026 OneHotTake) that exposes the live Dispatcharr VOD
library as a read-only HTTP filesystem. rclone mounts it; Plex reads real,
Plex-correctly-named files that 302-redirect into Dispatcharr's native VOD proxy.
Unlike .strm, this plays in Plex.

A plugin can't host a long-running endpoint inside Dispatcharr (the plugin API only
exposes settings + RPC actions, and the app server is WSGI), so the mount must run
as a child process the plugin owns. That makes lifecycle the hard part, so this
module is built around a small, identity-safe supervisor:

* **Identity** — the pid file records the child's pid + /proc start-time + cmdline.
  We only ever treat a process as "ours" if all three match, so a recycled PID is
  never mistaken for the server (and never killed).
* **Desired state** — ``httpfs_state.json`` holds {enabled, fail_count, last_error}.
  ``[MOUNT] Enable`` sets enabled; ``[MOUNT] Disable`` clears it. This is what lets
  the mount come back after a crash or container restart.
* **Reconcile** — one idempotent function compares desired vs. actual (is-ours +
  /healthz) and starts / restarts / stops / reports accordingly, under a file lock
  so concurrent callers can't double-spawn.
* **Supervision** — a celery-beat task runs reconcile on an interval; beat survives
  restarts and reloads its schedule from the DB, so it is the thing that brings the
  mount back unattended.
* **Crash-loop guard** — a single consecutive-failure counter (reset only by an
  explicit Enable) stops us busy-respawning a child that can't start.

The child needs uvicorn/fastapi/jinja2 (not in Dispatcharr's base image); they are
installed into Dispatcharr's interpreter on first Enable. The classic .strm mode has
no such dependency, so nothing is installed unless the user opts into the mount.
"""

import os
import json
import time
import fcntl
import signal
import socket
import logging
import subprocess
import urllib.request
from contextlib import contextmanager
from typing import Dict, Any, Optional


# Typed spawn outcomes so reconcile can tell a crash-loop (which counts toward the
# breaker) apart from a configuration/conflict error (which does not).
class _DepError(RuntimeError):
    """Required child dependencies are missing / can't be installed."""


class _UrlError(RuntimeError):
    """Configured Dispatcharr base URL is invalid."""


class _PortInUse(RuntimeError):
    """The mount port is already serving — likely a foreign process."""


class _ReadinessError(RuntimeError):
    """Child spawned but never answered /healthz — the crash-loop signal (E2)."""


class HttpfsControlMixin:
    """Mixed into the VOD2MLIB ``Plugin`` class. Adds the [MOUNT] actions and the
    identity-safe supervisor behind them."""

    # Vendored child server deps (absent from Dispatcharr's base image).
    _HTTPFS_DEPENDENCIES = ("uvicorn", "fastapi", "jinja2")
    # Dispatcharr's bundled interpreter (sys.executable in-container is uwsgi).
    _HTTPFS_PYTHON_EXE = "/dispatcharrpy/bin/python"
    _HTTPFS_DATA_DIR = "/data/plugins/vod2mlib"
    _HTTPFS_DEFAULT_PORT = 8889
    _HTTPFS_PLUGIN_KEY = "vod2mlib"
    # The cmdline marker that attributes a process to our child server.
    _HTTPFS_RUNNER_MARK = "standalone_runner.py"
    # Crash-loop breaker: stop auto-respawning after this many consecutive
    # readiness failures (~N minutes at the healthcheck interval). Reset by Enable.
    _HTTPFS_MAX_FAILURES = 5
    # Seconds to wait for /healthz after spawn before declaring a readiness failure.
    _HTTPFS_READY_TIMEOUT = 12.0
    # Seconds to wait after SIGTERM before escalating to SIGKILL.
    _HTTPFS_STOP_GRACE = 10.0
    # Beat supervision task identity + cadence.
    _HTTPFS_HEALTHCHECK_TASK = "vod2mlib.mount_healthcheck"        # PeriodicTask row
    _HTTPFS_HEALTHCHECK_CELERY = "vod2mlib.mount_healthcheck_task"  # Celery task name
    _HTTPFS_HEALTHCHECK_SECS = 60

    # --- file paths --------------------------------------------------------------

    @property
    def _httpfs_pid_file(self) -> str:
        os.makedirs(self._HTTPFS_DATA_DIR, exist_ok=True)
        return os.path.join(self._HTTPFS_DATA_DIR, "httpfs_server.pid")

    @property
    def _httpfs_state_file(self) -> str:
        os.makedirs(self._HTTPFS_DATA_DIR, exist_ok=True)
        return os.path.join(self._HTTPFS_DATA_DIR, "httpfs_state.json")

    @property
    def _httpfs_lock_file(self) -> str:
        os.makedirs(self._HTTPFS_DATA_DIR, exist_ok=True)
        return os.path.join(self._HTTPFS_DATA_DIR, "httpfs.lock")

    @staticmethod
    def _httpfs_atomic_write_json(path: str, obj: dict) -> None:
        """Write JSON atomically (temp + os.replace) — the state/pid files are read
        by multiple worker processes, so a partial write must never be observable."""
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # --- desired state + crash-loop counter (httpfs_state.json) ------------------

    def _httpfs_state(self) -> dict:
        try:
            with open(self._httpfs_state_file) as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("enabled", False)
                d.setdefault("fail_count", 0)
                d.setdefault("last_error", None)
                return d
        except (FileNotFoundError, ValueError, IOError):
            pass
        return {"enabled": False, "fail_count": 0, "last_error": None}

    def _httpfs_write_state(self, **changes) -> dict:
        st = self._httpfs_state()
        st.update(changes)
        self._httpfs_atomic_write_json(self._httpfs_state_file, st)
        return st

    # --- pid file with process identity (httpfs_server.pid, JSON) ----------------

    def _httpfs_read_pidstate(self) -> Optional[dict]:
        """Return {pid, starttime, cmd, port} for the tracked child, or None.

        Tolerates a legacy bare-integer pid file from older versions: identity is
        then unknown (starttime None) and falls back to cmdline attribution."""
        try:
            with open(self._httpfs_pid_file) as f:
                raw = f.read().strip()
        except (FileNotFoundError, IOError):
            return None
        if not raw:
            return None
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and "pid" in d:
                d.setdefault("starttime", None)
                d.setdefault("port", None)
                return d
        except ValueError:
            pass
        try:
            return {"pid": int(raw), "starttime": None, "port": None, "legacy": True}
        except ValueError:
            return None

    def _httpfs_save_pidstate(self, pid: int, port: int) -> None:
        self._httpfs_atomic_write_json(self._httpfs_pid_file, {
            "pid": pid,
            "starttime": self._httpfs_proc_starttime(pid),
            "cmd": self._httpfs_proc_cmdline(pid),
            "port": port,
        })

    def _httpfs_remove_pid_file(self) -> None:
        try:
            os.remove(self._httpfs_pid_file)
        except FileNotFoundError:
            pass

    # --- /proc identity helpers --------------------------------------------------

    @staticmethod
    def _httpfs_proc_state(pid: int) -> Optional[str]:
        """The single-letter process state from /proc/<pid>/stat (R/S/D/Z/...),
        or None if it can't be read."""
        try:
            with open("/proc/%d/stat" % pid) as f:
                data = f.read()
        except (FileNotFoundError, IOError, ProcessLookupError):
            return None
        try:
            return data.rsplit(")", 1)[1].split()[0]
        except IndexError:
            return None

    @staticmethod
    def _httpfs_proc_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # exists but owned by another uid
        # A zombie/defunct process (killed but not yet reaped by its parent worker)
        # still answers kill(0) and has a /proc entry — but it's dead. Treat Z/X as
        # not alive so reconcile respawns promptly instead of nursing a corpse.
        return HttpfsControlMixin._httpfs_proc_state(pid) not in ("Z", "X", "x")

    @staticmethod
    def _httpfs_proc_starttime(pid: int) -> Optional[int]:
        """Field 22 of /proc/<pid>/stat (start time in clock ticks since boot).

        Split on the last ')' so a comm containing spaces/parens can't shift the
        field offsets. After that split, field indexing restarts at 'state' (idx 0),
        so starttime (the 22nd overall field) sits at index 19."""
        try:
            with open("/proc/%d/stat" % pid) as f:
                data = f.read()
        except (FileNotFoundError, IOError, ProcessLookupError):
            return None
        try:
            after = data.rsplit(")", 1)[1].split()
            return int(after[19])
        except (IndexError, ValueError):
            return None

    @staticmethod
    def _httpfs_proc_cmdline(pid: int) -> str:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except (FileNotFoundError, IOError, ProcessLookupError):
            return ""

    def _httpfs_is_ours(self, state: Optional[dict] = None) -> bool:
        """True only if the tracked pid is alive AND verifiably our child server.

        Modern pid files carry a start-time: identity = pid alive + start-time
        match (defeats PID recycling). Legacy/unknown start-time falls back to a
        cmdline marker match. Either way, a recycled PID belonging to an unrelated
        process is rejected, so we never skip a needed restart and never kill a
        stranger."""
        if state is None:
            state = self._httpfs_read_pidstate()
        if not state:
            return False
        pid = state.get("pid")
        if not isinstance(pid, int) or not self._httpfs_proc_alive(pid):
            return False
        st = state.get("starttime")
        if st is not None:
            return self._httpfs_proc_starttime(pid) == st
        return self._HTTPFS_RUNNER_MARK in self._httpfs_proc_cmdline(pid)

    def _httpfs_is_running(self) -> bool:
        return self._httpfs_is_ours()

    # --- liveness over HTTP ------------------------------------------------------

    def _httpfs_healthy(self, port: int, timeout: float = 2.0) -> bool:
        """Does *something* answer GET /healthz with 200 on this port? Ownership-
        agnostic on purpose — combined with is-ours it distinguishes our healthy
        server, our zombie, and a foreign port holder."""
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/healthz" % port, timeout=timeout) as r:
                return getattr(r, "status", r.getcode()) == 200
        except Exception:
            return False

    # --- single-flight -----------------------------------------------------------

    @contextmanager
    def _httpfs_lock(self):
        """Exclusive cross-process lock so a user Enable and a beat healthcheck (or
        two workers) can't race into a double-spawn."""
        f = open(self._httpfs_lock_file, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()

    # --- stop (identity-safe, escalating) ----------------------------------------

    def _httpfs_stop_process(self, logger: logging.Logger, state: Optional[dict] = None) -> None:
        """Stop OUR child only: verify identity, SIGTERM, wait the grace window,
        escalate to SIGKILL, confirm exit, then clear the pid file. Refuses to
        signal any pid that fails the identity check."""
        if state is None:
            state = self._httpfs_read_pidstate()
        if not state:
            return
        pid = state.get("pid")
        if not self._httpfs_is_ours(state):
            logger.warning("Refusing to stop PID %s — not our mount server (identity mismatch)", pid)
            self._httpfs_remove_pid_file()
            return
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to mount server %d", pid)
        except ProcessLookupError:
            self._httpfs_remove_pid_file()
            return
        deadline = time.monotonic() + self._HTTPFS_STOP_GRACE
        while time.monotonic() < deadline:
            if not self._httpfs_proc_alive(pid):
                self._httpfs_remove_pid_file()
                return
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGKILL)
            logger.warning("Mount server %d ignored SIGTERM; sent SIGKILL", pid)
        except ProcessLookupError:
            pass
        for _ in range(25):  # ~5s for the kernel to reap before we clear tracking
            if not self._httpfs_proc_alive(pid):
                break
            time.sleep(0.2)
        self._httpfs_remove_pid_file()

    # --- dependency bootstrap ----------------------------------------------------

    def _httpfs_port(self, settings: Dict[str, Any]) -> int:
        try:
            return int(settings.get("httpfs_port") or self._HTTPFS_DEFAULT_PORT)
        except (TypeError, ValueError):
            return self._HTTPFS_DEFAULT_PORT

    def _httpfs_child(self, settings, path, method="GET", timeout=15):
        """Call the child server's own HTTP API (hydrator + stats)."""
        port = self._httpfs_port(settings)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")

    def _httpfs_missing_dependencies(self, python_exe: str):
        names = ",".join(repr(m) for m in self._HTTPFS_DEPENDENCIES)
        probe = ("import importlib.util,json;"
                 "print(json.dumps([m for m in [%s] "
                 "if importlib.util.find_spec(m) is None]))" % names)
        try:
            out = subprocess.run([python_exe, "-c", probe], capture_output=True, text=True)
            if out.returncode != 0:
                return None
            return json.loads((out.stdout or "[]").strip() or "[]")
        except Exception:
            return None

    def _httpfs_dep_red_alert(self, python_exe: str, missing, pip_error=None) -> str:
        try:
            host = socket.gethostname()
        except Exception:
            host = "<dispatcharr-container>"
        miss = " ".join(missing) if missing else " ".join(self._HTTPFS_DEPENDENCIES)
        pkgs = " ".join(self._HTTPFS_DEPENDENCIES)
        why = ""
        if pip_error:
            errlines = [l.strip() for l in pip_error.strip().splitlines() if l.strip()]
            pick = next((l for l in reversed(errlines) if l.lower().startswith("error")),
                        errlines[-1] if errlines else "")
            if pick:
                why = " (pip: %s)" % pick[:160]
        return (
            "VOD2MLIB mount can't start — required Python packages are missing: %s. "
            "The automatic install into %s failed%s. "
            "Fix it on your Docker host, then click [MOUNT] Enable again:  "
            "docker exec %s %s -m pip install %s  "
            "— if pip itself is missing, run 'docker exec %s %s -m ensurepip --upgrade' first."
            % (miss, python_exe, why, host, python_exe, pkgs, host, python_exe)
        )

    def _httpfs_ensure_dependencies(self, python_exe: str, logger: logging.Logger):
        missing = self._httpfs_missing_dependencies(python_exe)
        if missing == []:
            return None
        if missing is None:
            alert = self._httpfs_dep_red_alert(
                python_exe, self._HTTPFS_DEPENDENCIES,
                pip_error="Could not execute %s at all." % python_exe)
            logger.error(alert)
            return alert
        logger.info("Installing VOD2MLIB mount dependencies: %s", ", ".join(missing))
        pip_error = None
        try:
            subprocess.call([python_exe, "-m", "ensurepip", "--upgrade"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc = subprocess.run(
                [python_exe, "-m", "pip", "install", "-q", *self._HTTPFS_DEPENDENCIES],
                capture_output=True, text=True)
            if proc.returncode != 0:
                pip_error = (proc.stderr or proc.stdout or "").strip()
                logger.warning("pip install failed (rc=%d): %s", proc.returncode, pip_error[-800:])
        except Exception as e:
            pip_error = str(e)
            logger.warning("Dependency install errored (%s)", e)
        still = self._httpfs_missing_dependencies(python_exe)
        if still == []:
            return None
        alert = self._httpfs_dep_red_alert(python_exe, still, pip_error)
        logger.error(alert)
        return alert

    @staticmethod
    def _httpfs_validate_base_url(url: str):
        # One playback path → one URL validator.
        from vodlib.playback import validate_base_url
        return validate_base_url(url)

    def _httpfs_log_path(self) -> str:
        """Where the child's stdout/stderr is appended. A method (not an inline
        constant) so it's a single definition and overridable in tests."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mountsrv", "server.log")

    # --- spawn (raises typed errors; readiness-checked) --------------------------

    def _httpfs_spawn(self, settings: Dict[str, Any], logger: logging.Logger) -> int:
        """Start the child and block until it answers /healthz. Returns the pid.

        Raises a typed error so the caller can classify the failure:
          _UrlError / _DepError / _PortInUse  → configuration/conflict (not counted)
          _ReadinessError                     → spawned but never healthy (counted)
        """
        from vodlib.config import MountConfig
        cfg = MountConfig.from_settings(settings)
        port = cfg.port

        url_error = self._httpfs_validate_base_url(cfg.dispatcharr_url)
        if url_error:
            raise _UrlError(url_error)

        python_exe = self._HTTPFS_PYTHON_EXE
        dep_error = self._httpfs_ensure_dependencies(python_exe, logger)
        if dep_error:
            raise _DepError(dep_error)

        # Don't double-bind: if something already serves the port, it's a conflict,
        # not a crash-loop. (Our own healthy server never reaches here — reconcile
        # short-circuits O&P to NOOP.)
        if self._httpfs_healthy(port):
            raise _PortInUse("port %d is already serving" % port)

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        runner_path = os.path.join(plugin_dir, "mountsrv", "standalone_runner.py")
        if not os.path.exists(runner_path):
            raise _DepError("httpfs runner not found at %s" % runner_path)

        env = os.environ.copy()
        env.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
        app_dir = "/app"
        env["PYTHONPATH"] = app_dir if "PYTHONPATH" not in env else "%s:%s" % (app_dir, env["PYTHONPATH"])
        # The whole parent->child config contract, from the one schema.
        env.update(cfg.to_env())

        cmd = [python_exe, runner_path, "--port", str(port)]
        log_file = self._httpfs_log_path()
        with open(log_file, "a") as log:
            proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=log,
                                    start_new_session=True)
        self._httpfs_save_pidstate(proc.pid, port)

        deadline = time.monotonic() + self._HTTPFS_READY_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self._httpfs_remove_pid_file()
                raise _ReadinessError(
                    "mount server exited during startup (rc=%s); see mountsrv/server.log"
                    % proc.returncode)
            if self._httpfs_healthy(port):
                logger.info("Mount server ready PID %d on 0.0.0.0:%d", proc.pid, port)
                return proc.pid
            time.sleep(0.3)
        # Never became healthy — stop the half-started child and report E2.
        self._httpfs_stop_process(logger)
        raise _ReadinessError(
            "mount server did not become healthy within %.0fs; see mountsrv/server.log"
            % self._HTTPFS_READY_TIMEOUT)

    # --- reconcile (desired vs. actual) ------------------------------------------

    def _rc(self, action: str, port: Optional[int] = None, healthy: bool = False,
            pid: Optional[int] = None, reason: Optional[str] = None) -> Dict[str, Any]:
        """Structured reconcile result consumed by run()/Status/the healthcheck."""
        st = self._httpfs_state()
        if pid is None:
            pid = (self._httpfs_read_pidstate() or {}).get("pid")
        return {
            "action": action,
            "healthy": healthy,
            "pid": pid,
            "reason": reason,
            "fail_count": st.get("fail_count", 0),
            "max_failures": self._HTTPFS_MAX_FAILURES,
        }

    def _httpfs_maybe_adopt(self, port: int) -> None:
        """Healthy child but pid file lacks a start-time (legacy/bare-int): rewrite
        it with full identity once, no restart."""
        state = self._httpfs_read_pidstate()
        if state and isinstance(state.get("pid"), int) and state.get("starttime") is None:
            self._httpfs_save_pidstate(state["pid"], port)

    def _httpfs_reconcile(self, settings: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
        """Idempotent: make the actual mount match the desired state. Single-flighted."""
        with self._httpfs_lock():
            st = self._httpfs_state()
            port = self._httpfs_port(settings)
            desired = bool(st.get("enabled"))
            ours = self._httpfs_is_ours()
            healthy = self._httpfs_healthy(port)

            if not desired:
                if ours:
                    self._httpfs_stop_process(logger)
                    return self._rc("STOP", port)
                if healthy:
                    logger.warning("Port %d held by a non-ours process; leaving it alone", port)
                    self._httpfs_remove_pid_file()
                    return self._rc("CLEAN", port, healthy=True)
                self._httpfs_remove_pid_file()
                return self._rc("CLEAN", port)

            # desired == enabled
            if ours and healthy:
                self._httpfs_maybe_adopt(port)
                return self._rc("NOOP", port, healthy=True)
            if healthy and not ours:
                return self._rc("CONFLICT", port, healthy=True,
                                reason="port %d held by another process" % port)

            # A (re)start is needed: zombie (ours, not healthy) or dead.
            if st.get("fail_count", 0) >= self._HTTPFS_MAX_FAILURES:
                return self._rc("FAILED", port, reason=st.get("last_error"))

            restart = ours and not healthy
            if restart:
                self._httpfs_stop_process(logger)

            try:
                pid = self._httpfs_spawn(settings, logger)
                self._httpfs_write_state(fail_count=0, last_error=None)
                return self._rc("RESTART" if restart else "START", port, healthy=True, pid=pid)
            except _ReadinessError as e:
                new = self._httpfs_state().get("fail_count", 0) + 1
                self._httpfs_write_state(fail_count=new, last_error=str(e))
                logger.warning("Mount start failed (%d/%d): %s",
                               new, self._HTTPFS_MAX_FAILURES, e)
                action = "FAILED" if new >= self._HTTPFS_MAX_FAILURES else "START"
                return self._rc(action, port, reason=str(e))
            except (_DepError, _UrlError, _PortInUse) as e:
                # Configuration / conflict — a retry won't help, so don't count it.
                logger.error("Mount cannot start: %s", e)
                return self._rc("CONFLICT", port, reason=str(e))

    # --- beat supervision task ---------------------------------------------------

    def _httpfs_register_healthcheck(self, settings: Dict[str, Any], logger: logging.Logger) -> None:
        """Register the celery-beat task that reconciles the mount on an interval.
        Beat survives restarts and reloads from the DB, so this is what brings the
        mount back unattended."""
        try:
            from django_celery_beat.models import PeriodicTask, IntervalSchedule
        except ImportError:
            logger.warning("django-celery-beat not installed; mount auto-recovery is OFF "
                           "(the mount still runs but won't self-restart after a crash/restart).")
            return
        schedule, _ = IntervalSchedule.objects.get_or_create(
            every=self._HTTPFS_HEALTHCHECK_SECS, period=IntervalSchedule.SECONDS)
        PeriodicTask.objects.update_or_create(
            name=self._HTTPFS_HEALTHCHECK_TASK,
            defaults={
                "interval": schedule,
                "crontab": None,
                "task": self._HTTPFS_HEALTHCHECK_CELERY,
                "queue": "dvr",
                "kwargs": json.dumps({}),
                "enabled": True,
                "description": "VOD2MLIB mount supervision (every %ds)" % self._HTTPFS_HEALTHCHECK_SECS,
            },
        )
        logger.info("Registered mount healthcheck (every %ds)", self._HTTPFS_HEALTHCHECK_SECS)

    def _httpfs_remove_healthcheck(self, logger: logging.Logger) -> None:
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return
        PeriodicTask.objects.filter(name=self._HTTPFS_HEALTHCHECK_TASK).delete()

    # --- actions -----------------------------------------------------------------

    def httpfs_enable(self, settings: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
        """[MOUNT] Enable — declare intent, clear the crash-loop breaker, register
        supervision, and (re)start. This is also the manual retry: it resets
        fail_count so a tripped breaker gets another chance."""
        self._httpfs_write_state(enabled=True, fail_count=0, last_error=None)
        self._httpfs_register_healthcheck(settings, logger)
        rc = self._httpfs_reconcile(settings, logger)
        port = self._httpfs_port(settings)
        if rc["healthy"]:
            return {
                "status": "ok",
                "message": ("Mount server enabled on port %d. Open  /vod2mlib/rclone_conf "
                            "(or click [MOUNT] rclone config) for the rclone remote, then "
                            "point Plex at the mount." % port),
                "pid": rc.get("pid"), "port": port,
            }
        raise RuntimeError(rc.get("reason") or
                           "Mount server failed to start; see mountsrv/server.log")

    def httpfs_disable(self, logger: logging.Logger) -> Dict[str, Any]:
        """[MOUNT] Disable — clear intent, remove supervision, stop the child."""
        self._httpfs_write_state(enabled=False)
        self._httpfs_remove_healthcheck(logger)
        state = self._httpfs_read_pidstate()
        if state and self._httpfs_is_ours(state):
            logger.info("Stopping mount server (PID %s)", state.get("pid"))
            self._httpfs_stop_process(logger, state)
            return {"status": "ok", "message": "Mount server stopped."}
        self._httpfs_remove_pid_file()
        return {"status": "ok", "message": "Mount server not running."}

    def httpfs_status(self, settings: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
        st = self._httpfs_state()
        port = self._httpfs_port(settings)
        if st.get("fail_count", 0) >= self._HTTPFS_MAX_FAILURES:
            return {"status": "error",
                    "message": ("Mount server FAILED after %d attempts. Fix config/logs, then "
                                "click [MOUNT] Enable to retry. Last error: %s"
                                % (self._HTTPFS_MAX_FAILURES, st.get("last_error") or "unknown"))}
        if not self._httpfs_is_ours() or not self._httpfs_healthy(port):
            missing = self._httpfs_missing_dependencies(self._HTTPFS_PYTHON_EXE)
            if missing:
                raise RuntimeError(self._httpfs_dep_red_alert(self._HTTPFS_PYTHON_EXE, missing))
            if st.get("enabled"):
                return {"status": "ok",
                        "message": "Mount server is DOWN — supervision will restart it shortly. "
                                   "Click [MOUNT] Enable to start it now."}
            return {"status": "ok", "message": "Mount server is STOPPED. Click [MOUNT] Enable to start."}
        try:
            stats = self._httpfs_child(settings, "/stats")
            hy = self._httpfs_child(settings, "/hydrate/status")
        except Exception as e:
            return {"status": "error", "message": "Mount server running but not responding: %s" % e}
        lib = stats.get("library", {})
        mv, sv = lib.get("movies", {}), lib.get("series", {})
        lines = [
            "✅ Mount server running.",
            "Movies visible: %s / %s  (Series: %s / %s)" % (
                mv.get("sized", "?"), mv.get("total", "?"),
                sv.get("sized", "?"), sv.get("total", "?")),
        ]
        if hy.get("enabled"):
            nxt = hy.get("next_run") or "manual only"
            run = "running now" if hy.get("running") else "next run %s" % nxt
            lines.append("Hydration: %s parallel fetches, %s." % (hy.get("concurrency"), run))
        else:
            lines.append("Hydration: disabled (concurrency=0).")
        gap = (mv.get("total", 0) or 0) - (mv.get("sized", 0) or 0)
        if gap > 0:
            lines.append("Next step: %d movies still need sizes — click [MOUNT] Hydrate Now." % gap)
        else:
            lines.append("Next step: point Plex at the rclone mount /Movies and /Series.")
        return {"status": "ok", "message": "\n".join(lines)}

    def httpfs_rclone_config(self, settings: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
        if not self._httpfs_is_running():
            return {"status": "error",
                    "message": "Mount server not running — click [MOUNT] Enable first."}
        port = self._httpfs_port(settings)
        auth_note = (" Use any active Dispatcharr API key in the headers line."
                     if settings.get("httpfs_enable_auth", False) else "")
        return {"status": "ok",
                "message": ("Open  http://<this-host>:%d/rclone_conf  for a paste-ready rclone "
                            "[vod2mlib] remote (the URL is filled in from your browser request)."
                            "%s" % (port, auth_note))}

    def httpfs_hydrate_now(self, settings: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
        if not self._httpfs_is_running():
            raise RuntimeError("Mount server not running — click [MOUNT] Enable first.")
        try:
            d = self._httpfs_child(settings, "/hydrate/run", method="POST")
        except Exception as e:
            raise RuntimeError("Could not reach the mount server: %s" % e)
        if not d.get("triggered"):
            return {"status": "ok",
                    "message": "Hydration is disabled — set Hydration Concurrency above 0 and re-enable."}
        stt = d.get("status", {})
        return {"status": "ok",
                "message": ("Hydration started (%s parallel fetches). Titles appear as sizes land."
                            % stt.get("concurrency"))}

    def httpfs_stop_on_unload(self, logger: logging.Logger):
        """Call from Plugin.stop() (disable/reload). Kill the child but DON'T clear
        the desired-enabled flag, so a reload/restart brings it back via reconcile/
        beat. The user turns the mount off deliberately with [MOUNT] Disable."""
        state = self._httpfs_read_pidstate()
        if state and self._httpfs_is_ours(state):
            logger.info("Plugin stop() — terminating mount server (PID %s)", state.get("pid"))
            self._httpfs_stop_process(logger, state)
