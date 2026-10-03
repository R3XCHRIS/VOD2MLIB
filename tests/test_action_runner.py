import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import action_runner as runner
from inventory import action_lock
from plugin import Plugin


@pytest.mark.parametrize("cancelled", [True, False])
def test_monitor_stops_blocked_worker_and_releases_lock(tmp_path, cancelled):
    ready = tmp_path / "ready"
    code = (
        "from inventory import action_lock;from pathlib import Path;import time;"
        + f"\nwith action_lock({str(tmp_path)!r}):\n Path({str(ready)!r}).touch()\n time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code], start_new_session=os.name == "posix"
    )
    try:
        limit = time.time() + 10
        while not ready.exists() and time.time() < limit:
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(RuntimeError):
            with action_lock(tmp_path):
                pass
        cancel = tmp_path / "cancel"
        if cancelled:
            cancel.touch()
        started = time.time()
        assert runner.monitor(process, time.time() + 0.2, cancel) == (
            "cancelled" if cancelled else "timed_out"
        )
        assert time.time() - started < 5
        with action_lock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            runner.terminate(process)


@pytest.mark.skipif(os.name != "posix", reason="Linux process groups")
def test_deadline_stops_descendants(tmp_path):
    pidfile = tmp_path / "pid"
    code = (
        "import subprocess,sys,time;from pathlib import Path;"
        + f"p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        while not pidfile.exists():
            time.sleep(0.02)
        runner.monitor(parent, time.time(), tmp_path / "cancel")
        child = int(pidfile.read_text())
        limit = time.time() + 3
        while time.time() < limit:
            path = Path(f"/proc/{child}/stat")
            if (
                not path.exists()
                or path.read_text().rsplit(")", 1)[1].split()[0] == "Z"
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("descendant survived cancellation")
    finally:
        if parent.poll() is None:
            runner.terminate(parent)


@pytest.mark.parametrize("action", ["selective_cleanup", "rebuild_inventory"])
def test_async_dispatch_and_stop(monkeypatch, tmp_path, action):
    monkeypatch.setenv("VOD2MLIB_STATE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        runner, "start", lambda *args: calls.append(args) or {"status": "ok"}
    )
    assert Plugin().run(action, {}, {"settings": {}})["status"] == "ok"
    assert calls[0][0] == action
    job = {
        "id": "test",
        "pid": os.getpid(),
        "birth": runner.birth(os.getpid()),
        "state": "running",
        "message": "Running",
    }
    runner.write_json(tmp_path / "job.json", job)
    assert runner.stop()["status"] == "ok"
    assert (tmp_path / "test.cancel").exists()
    assert runner.start("rescan_all", {}, {})["status"] == "ok"  # mocked dispatcher


def test_overlap_and_stale_pid(tmp_path):
    job = {
        "id": "test",
        "pid": os.getpid(),
        "birth": runner.birth(os.getpid()),
        "state": "running",
    }
    runner.write_json(tmp_path / "job.json", job)
    assert runner.start("rescan_all", {}, {}, tmp_path)["status"] == "error"
    job["birth"] = "different-process"
    runner.write_json(tmp_path / "job.json", job)
    assert runner.status(tmp_path)["job"]["state"] == "interrupted"
    assert runner.stop(tmp_path)["message"] == "No background action is running"


@pytest.mark.skipif(os.name != "posix", reason="Linux unreaped processes")
def test_exited_unreaped_process_is_not_running():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        limit = time.time() + 5
        while time.time() < limit:
            fields = (
                Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
            )
            if fields[0] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("process did not exit")
        assert runner.birth(process.pid) is None
    finally:
        process.wait(timeout=5)


def test_status_reports_phase_and_remaining_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "time", lambda: 1000)
    runner.write_json(
        tmp_path / "job.json",
        {
            "id": "test",
            "pid": os.getpid(),
            "birth": runner.birth(os.getpid()),
            "state": "running",
            "started": 700,
            "deadline": 1900,
        },
    )
    runner.write_json(
        tmp_path / "test.progress.json",
        {"phase": "Emby snapshot: 500 of 2,000 items fetched"},
    )
    message = runner.status(tmp_path)["message"]
    assert "500 of 2,000" in message
    assert "elapsed 5 min" in message and "deadline in 15 min" in message


def test_supervisor_metadata_failure_cannot_orphan_worker(tmp_path, monkeypatch):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        start_new_session=os.name == "posix",
    )
    runner.write_json(
        tmp_path / "job.json",
        {"id": "test", "action": "rescan_all", "deadline": time.time() + 60},
    )
    request = tmp_path / "test.request.json"
    request.write_text('{"settings": {"token": "private"}}')
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    def full_disk(*args):
        raise OSError("Disk full")

    monkeypatch.setattr(runner, "write_json", full_disk)
    try:
        with pytest.raises(OSError, match="Disk full"):
            runner.supervise(tmp_path, "test")
        assert process.poll() is not None
        assert not request.exists()
    finally:
        if process.poll() is None:
            runner.terminate(process)


def test_legacy_lock_is_respected(tmp_path):
    with action_lock(tmp_path):
        result = runner.start("rescan_all", {}, {}, tmp_path)
        assert result["status"] == "error"
        assert not (tmp_path / "job.json").exists()


def test_private_request_and_status_do_not_expose_settings(tmp_path, monkeypatch):
    class Process:
        pid = os.getpid()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    result = runner.start(
        "rescan_all", {}, {"media_server_token": "secret-value"}, tmp_path
    )
    assert "secret-value" not in json.dumps(result)
    request = next(tmp_path.glob("*.request.json"))
    assert (
        json.loads(request.read_text())["settings"]["media_server_token"]
        == "secret-value"
    )
    if os.name == "posix":
        assert request.stat().st_mode & 0o077 == 0


def test_completed_status_includes_timing_summary(tmp_path):
    runner.write_json(tmp_path / "job.json", {
        "id": "test", "state": "completed", "message": "Cleanup complete",
        "result": {"reconciliation": {"timings": {
            "total": {"wall_seconds": 31.25},
            "catalogue": {"wall_seconds": 19.01},
        }}},
    })
    first, second = runner.status(tmp_path), runner.status(tmp_path)
    assert first["message"] == second["message"]
    assert "total 31.2s" in first["message"]
    assert "catalogue 19.0s" in first["message"]
    assert first["message"].count("timing:") == 1
