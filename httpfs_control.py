"""HTTP-filesystem (rclone/Plex mount) control plane for VOD2MLIB.

This is the opt-in second output mode. The classic .strm/.nfo generator (plugin.py)
is untouched and still the default; everything here only runs when the user clicks
one of the [MOUNT] actions.

It manages a standalone ASGI child server (the ``mountsrv/`` subpackage, vendored from
VODFS — MIT, Copyright (c) 2026 OneHotTake) that exposes the live Dispatcharr VOD
library as a read-only HTTP filesystem. rclone mounts it; Plex reads real,
Plex-correctly-named files that 302-redirect into Dispatcharr's native VOD proxy.
Unlike .strm, this plays in Plex.

The child needs uvicorn/fastapi/jinja2 (not in Dispatcharr's base image); they are
installed into Dispatcharr's interpreter on first Enable. The classic .strm mode has
no such dependency, so nothing is installed unless the user opts into the mount.
"""

import os
import json
import signal
import socket
import logging
import subprocess
from typing import Dict, Any


class HttpfsControlMixin:
    """Mixed into the VOD2MLIB ``Plugin`` class. Adds the [MOUNT] actions."""

    # Vendored child server deps (absent from Dispatcharr's base image).
    _HTTPFS_DEPENDENCIES = ("uvicorn", "fastapi", "jinja2")
    # Dispatcharr's bundled interpreter (sys.executable in-container is uwsgi).
    _HTTPFS_PYTHON_EXE = "/dispatcharrpy/bin/python"
    _HTTPFS_DATA_DIR = "/data/plugins/vod2mlib"
    _HTTPFS_DEFAULT_PORT = 8889

    # --- PID / process bookkeeping ----------------------------------------------

    @property
    def _httpfs_pid_file(self) -> str:
        os.makedirs(self._HTTPFS_DATA_DIR, exist_ok=True)
        return os.path.join(self._HTTPFS_DATA_DIR, "httpfs_server.pid")

    def _httpfs_read_pid(self):
        try:
            with open(self._httpfs_pid_file, "r") as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError, IOError):
            return None

    def _httpfs_save_pid(self, pid: int):
        with open(self._httpfs_pid_file, "w") as f:
            f.write(str(pid))

    def _httpfs_remove_pid_file(self):
        try:
            os.remove(self._httpfs_pid_file)
        except FileNotFoundError:
            pass

    def _httpfs_is_running(self, pid=None) -> bool:
        if pid is None:
            pid = self._httpfs_read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return pid is not None and isinstance(pid, int) and self._httpfs_proc_alive(pid)

    @staticmethod
    def _httpfs_proc_alive(pid: int) -> bool:
        return os.path.exists("/proc/%d" % pid)

    def _httpfs_stop_process(self, pid: int, logger: logging.Logger):
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to httpfs server %d", pid)
        except ProcessLookupError:
            logger.warning("httpfs server %d not found", pid)
        except Exception as e:
            logger.exception("Failed to stop httpfs server %d: %s", pid, e)

    # --- child HTTP API ----------------------------------------------------------

    def _httpfs_port(self, settings: Dict[str, Any]) -> int:
        try:
            return int(settings.get("httpfs_port") or self._HTTPFS_DEFAULT_PORT)
        except (TypeError, ValueError):
            return self._HTTPFS_DEFAULT_PORT

    def _httpfs_child(self, settings, path, method="GET", timeout=15):
        """Call the child server's own HTTP API (hydrator + stats)."""
        import urllib.request
        port = self._httpfs_port(settings)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")

    # --- dependency bootstrap ----------------------------------------------------

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

    # --- actions -----------------------------------------------------------------

    def httpfs_enable(self, settings: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
        pid = self._httpfs_read_pid()
        if pid and self._httpfs_is_running(pid):
            return {"status": "ok", "message": "Mount server already running on PID %d" % pid}

        # One configuration path: build the mount config from the same settings dict,
        # via the shared schema. Everything the child needs comes from cfg.to_env().
        from vodlib.config import MountConfig
        cfg = MountConfig.from_settings(settings)
        port = cfg.port

        url_error = self._httpfs_validate_base_url(cfg.dispatcharr_url)
        if url_error:
            logger.error("Refusing to start mount: %s", url_error)
            raise RuntimeError(url_error)

        python_exe = self._HTTPFS_PYTHON_EXE
        dep_error = self._httpfs_ensure_dependencies(python_exe, logger)
        if dep_error:
            raise RuntimeError(dep_error)

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        runner_path = os.path.join(plugin_dir, "mountsrv", "standalone_runner.py")
        if not os.path.exists(runner_path):
            raise RuntimeError("httpfs runner not found at %s" % runner_path)

        env = os.environ.copy()
        env.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
        app_dir = "/app"
        env["PYTHONPATH"] = app_dir if "PYTHONPATH" not in env else "%s:%s" % (app_dir, env["PYTHONPATH"])
        # The whole parent->child config contract, from the one schema.
        env.update(cfg.to_env())

        cmd = [python_exe, runner_path, "--port", str(port)]
        log_file = os.path.join(plugin_dir, "mountsrv", "server.log")
        try:
            with open(log_file, "a") as log:
                process = subprocess.Popen(cmd, env=env, stdout=log, stderr=log,
                                           start_new_session=True)
            self._httpfs_save_pid(process.pid)
            logger.info("Mount server started PID %d on 0.0.0.0:%d", process.pid, port)
            return {"status": "ok",
                    "message": ("Mount server enabled on port %d. Open  /vod2mlib/rclone_conf "
                                "(or click [MOUNT] rclone config) for the rclone remote, then "
                                "point Plex at the mount." % port),
                    "pid": process.pid, "port": port}
        except Exception as e:
            logger.exception("Failed to start mount server: %s", e)
            raise RuntimeError("Failed to start mount server: %s" % e)

    def httpfs_disable(self, logger: logging.Logger) -> Dict[str, Any]:
        pid = self._httpfs_read_pid()
        if not pid:
            return {"status": "ok", "message": "Mount server not running"}
        if not self._httpfs_is_running(pid):
            self._httpfs_remove_pid_file()
            return {"status": "ok", "message": "Mount server was not running (stale PID %d)" % pid}
        logger.info("Stopping mount server (PID %d)", pid)
        self._httpfs_stop_process(pid, logger)
        self._httpfs_remove_pid_file()
        return {"status": "ok", "message": "Mount server stopped (PID %d)" % pid}

    def httpfs_status(self, settings: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
        if not self._httpfs_is_running(self._httpfs_read_pid()):
            missing = self._httpfs_missing_dependencies(self._HTTPFS_PYTHON_EXE)
            if missing:
                raise RuntimeError(self._httpfs_dep_red_alert(self._HTTPFS_PYTHON_EXE, missing))
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
        if not self._httpfs_is_running(self._httpfs_read_pid()):
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
        if not self._httpfs_is_running(self._httpfs_read_pid()):
            raise RuntimeError("Mount server not running — click [MOUNT] Enable first.")
        try:
            d = self._httpfs_child(settings, "/hydrate/run", method="POST")
        except Exception as e:
            raise RuntimeError("Could not reach the mount server: %s" % e)
        if not d.get("triggered"):
            return {"status": "ok",
                    "message": "Hydration is disabled — set Hydration Concurrency above 0 and re-enable."}
        st = d.get("status", {})
        return {"status": "ok",
                "message": ("Hydration started (%s parallel fetches). Titles appear as sizes land."
                            % st.get("concurrency"))}

    def httpfs_stop_on_unload(self, logger: logging.Logger):
        """Call from Plugin.stop() so disabling/reloading the plugin kills the child."""
        pid = self._httpfs_read_pid()
        if pid and self._httpfs_is_running(pid):
            logger.info("Plugin stop() - terminating mount server (PID %d)", pid)
            self._httpfs_stop_process(pid, logger)
            self._httpfs_remove_pid_file()
