"""Isolated, cancellable action processes with an independently enforced deadline."""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:
    from .inventory import action_lock, state_directory
except ImportError:
    from inventory import action_lock, state_directory

ACTIVE = {"starting", "running", "stopping"}


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as output:
        json.dump(value, output)
    os.replace(temporary, path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except FileNotFoundError:
        return {}


def birth(pid):
    if os.name == "posix":
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return None if fields[0] == "Z" else fields[19]
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if (
            not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            or exit_code.value != 259
        ):
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def status(directory=None):
    directory = Path(directory or state_directory())
    job = read_json(directory / "job.json")
    if not job:
        return {"status": "ok", "message": "No background action has run"}
    if job["state"] in ACTIVE and birth(job["pid"]) != job["birth"]:
        job.update(
            state="interrupted", message="Action process exited; retry is available"
        )
    if job["state"] in ACTIVE:
        elapsed = max(0, int((time.time() - job.get("started", time.time())) / 60))
        remaining = max(0, int((job.get("deadline", time.time()) - time.time()) / 60))
        live = read_json(directory / f"{job['id']}.progress.json")
        job['progress'] = live
        progress = live.get(
            "phase", job.get("message", job["state"])
        )
        job["message"] = (
            f"{progress}; elapsed {elapsed} min, deadline in {remaining} min"
        )
    timings = job.get("result", {}).get("reconciliation", {}).get("timings", {})
    if timings:
        names = {
            "total": "total", "emby_snapshot": "Emby", "catalogue": "catalogue",
            "legacy_adoption": "discovery", "m3u_tracked_show_check": "M3U check",
            "cleanup": "cleanup", "generate_movies": "movies", "generate_series": "series",
        }
        summary = ", ".join(
            f"{label} {timings[name]['wall_seconds']:.1f}s"
            for name, label in names.items() if name in timings
        )
        job["message"] = job.get("message", job["state"]) + "; timing: " + summary
    return {"status": "ok", "message": job.get("message", job["state"]), "job": job}


def start(action, params, settings, directory=None):
    directory = Path(directory or state_directory())
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with action_lock(directory / "control"):
            current = status(directory).get("job", {})
            if current.get("state") in ACTIVE:
                return {
                    "status": "error",
                    "message": "Another action is running; use Action status or Stop running action",
                }
            # Also respect actions launched by older plugin versions.
            with action_lock(directory):
                pass
            job_id = uuid.uuid4().hex
            request = directory / f"{job_id}.request.json"
            minutes = int(settings.get("action_timeout_minutes", 30))
            if not 1 <= minutes <= 120:
                raise ValueError("Action timeout must be between 1 and 120 minutes")
            write_json(
                request, {"action": action, "params": params, "settings": settings}
            )
            with open(os.devnull, "wb") as output:
                process = subprocess.Popen(
                    [
                        shutil.which("python3")
                        or shutil.which("python")
                        or sys.executable,
                        str(Path(__file__).resolve()),
                        "supervise",
                        str(directory),
                        job_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=os.name == "posix",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            job = {
                "id": job_id,
                "action": action,
                "pid": process.pid,
                "birth": birth(process.pid),
                "state": "starting",
                "started": time.time(),
                "deadline": time.time() + minutes * 60,
                "message": "Action started; use Action status to view progress and results",
            }
            write_json(directory / "job.json", job)
            return {"status": "ok", "message": job["message"], "job": job}
    except Exception as error:
        return {"status": "error", "message": str(error)}


def stop(directory=None):
    directory = Path(directory or state_directory())
    with action_lock(directory / "control"):
        job = status(directory).get("job", {})
        if job.get("state") not in ACTIVE:
            return {"status": "ok", "message": "No background action is running"}
        (directory / f"{job['id']}.cancel").touch(mode=0o600)
    return {
        "status": "ok",
        "message": "Stop requested; the action and its workers will exit within a few seconds. Completed file changes remain.",
    }


def terminate(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
    # A descendant may survive the leader's graceful exit.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def monitor(process, deadline, cancel):
    while process.poll() is None:
        if cancel.exists() or time.time() >= deadline:
            reason = "cancelled" if cancel.exists() else "timed_out"
            terminate(process)
            return reason
        time.sleep(0.2)
    return "completed" if process.returncode == 0 else "failed"


def supervise(directory, job_id):
    request = directory / f"{job_id}.request.json"
    result_path = directory / f"{job_id}.result.json"
    cancel = directory / f"{job_id}.cancel"
    # Launch holds control until job metadata is committed.
    for attempt in range(100):
        try:
            with action_lock(directory / "control"):
                job = read_json(directory / "job.json")
            break
        except RuntimeError:
            time.sleep(0.1)
    else:
        return
    if job.get("id") != job_id:
        return
    process = None
    try:
        with open(os.devnull, "wb") as output:
            process = subprocess.Popen(
                [
                    shutil.which("python3") or shutil.which("python") or sys.executable,
                    str(Path(__file__).resolve()),
                    "work",
                    str(directory),
                    job_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=os.name == "posix",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        job.update(
            state="running",
            message=f"Running {job['action']}; deadline in {round((job['deadline'] - time.time()) / 60)} minutes",
        )
        write_json(directory / "job.json", job)
        outcome = monitor(process, job["deadline"], cancel)
        result = read_json(result_path)
        if outcome == "completed" and result.get("status") == "error":
            outcome = "failed"
        job.update(
            state=outcome,
            finished=time.time(),
            result=result,
            message=result.get(
                "message", f"Action {outcome}; completed file changes remain"
            ),
        )
    except Exception as error:
        if process is not None and process.poll() is None:
            terminate(process)
        job.update(
            state="failed", message=f"Action supervisor failed ({type(error).__name__})"
        )
    finally:
        try:
            write_json(directory / "job.json", job)
        finally:
            for path in (
                request,
                result_path,
                cancel,
                directory / f"{job_id}.progress.json",
            ):
                path.unlink(missing_ok=True)


def work(directory, job_id):
    request = read_json(directory / f"{job_id}.request.json")
    os.environ["VOD2MLIB_STATE_DIR"] = str(directory)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
    sys.path.insert(0, "/app")
    import django

    # App startup hooks belong to the existing Dispatcharr services, not this
    # short-lived action worker (some hooks spawn background services).
    from django.apps import AppConfig

    original_create = AppConfig.create

    def quiet_create(cls, entry):
        config = original_create(entry)
        config.ready = lambda: None
        return config

    AppConfig.create = classmethod(quiet_create)
    django.setup()
    from plugin import Plugin

    plugin = Plugin()
    def progress(message):
        rec = getattr(plugin, '_reconciliation', None)
        telemetry = rec.telemetry() if rec else {}
        write_json(directory / f"{job_id}.progress.json", {
            'phase': message, 'updated_at': time.time(), **telemetry,
        })
    plugin._progress = progress
    result = plugin._run_locked_action(
        request["action"],
        request["params"],
        {
            "logger": logging.getLogger("vod2mlib.action"),
            "settings": request["settings"],
        },
    )
    write_json(directory / f"{job_id}.result.json", result)


def run_and_wait(action, params, settings):
    result = start(action, params, settings)
    if result["status"] == "error":
        return result
    job_id = result["job"]["id"]
    while True:
        current = status()
        job = current.get("job", {})
        if job.get("id") != job_id:
            return {
                "status": "error",
                "message": "Scheduled action result was superseded",
            }
        if job.get("state") not in ACTIVE:
            return job.get("result") or {
                "status": "error",
                "message": current["message"],
            }
        time.sleep(0.5)


if __name__ == "__main__":
    mode, folder, identifier = sys.argv[1:]
    (supervise if mode == "supervise" else work)(Path(folder), identifier)
