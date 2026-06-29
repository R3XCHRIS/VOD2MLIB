"""Behavior lock for the mount supervisor (httpfs_control.HttpfsControlMixin).

These tests pin the agreed matrices:
  - B: is_ours() identity gate (I1-I7)
  - C: stop escalation (S1-S4)
  - D: spawn readiness + typed errors (E1-E5)
  - A: reconcile decision cube D x O x P (R1-R8)
  - #28: crash-loop breaker (T28.1-T28.6)

Everything is mocked at clean seams (/proc, os.kill, subprocess, HTTP /healthz,
MountConfig) so no real child process, Django, or celery is required.
"""

import os
import sys
import types
import logging

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpfs_control as hc  # noqa: E402

LOG = logging.getLogger("test.mount")


class Harness(hc.HttpfsControlMixin):
    pass


@pytest.fixture
def fs(tmp_path, monkeypatch):
    h = Harness()
    h._HTTPFS_DATA_DIR = str(tmp_path)          # instance attr shadows class attr
    monkeypatch.setattr(hc.time, "sleep", lambda *_a, **_k: None)  # no real waits
    return h


def _write_pidstate(fs, **kw):
    st = {"pid": 4321, "starttime": 111, "cmd": "python standalone_runner.py", "port": 8889}
    st.update(kw)
    fs._httpfs_atomic_write_json(fs._httpfs_pid_file, st)
    return st


# --------------------------------------------------------------------------- B
# is_ours() identity gate

def test_I1_no_pidfile_not_ours(fs):
    assert fs._httpfs_is_ours() is False


def test_I2_starttime_match_is_ours(fs, monkeypatch):
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: True)
    monkeypatch.setattr(fs, "_httpfs_proc_starttime", lambda pid: 111)
    assert fs._httpfs_is_ours({"pid": 4321, "starttime": 111}) is True


def test_I3_recycled_pid_starttime_mismatch_not_ours(fs, monkeypatch):
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: True)
    monkeypatch.setattr(fs, "_httpfs_proc_starttime", lambda pid: 999)  # different proc
    assert fs._httpfs_is_ours({"pid": 4321, "starttime": 111}) is False


def test_I4_dead_pid_not_ours(fs, monkeypatch):
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: False)
    assert fs._httpfs_is_ours({"pid": 4321, "starttime": 111}) is False


def test_I5_legacy_cmdline_match_is_ours(fs, monkeypatch):
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: True)
    monkeypatch.setattr(fs, "_httpfs_proc_cmdline",
                        lambda pid: "python /x/mountsrv/standalone_runner.py --port 8889")
    assert fs._httpfs_is_ours({"pid": 4321, "starttime": None}) is True


def test_I6_legacy_cmdline_mismatch_not_ours(fs, monkeypatch):
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: True)
    monkeypatch.setattr(fs, "_httpfs_proc_cmdline", lambda pid: "nginx: worker process")
    assert fs._httpfs_is_ours({"pid": 4321, "starttime": None}) is False


def test_I8_zombie_is_not_alive(fs, monkeypatch):
    # kill(0) succeeds for a defunct process, but state 'Z' means it's dead.
    monkeypatch.setattr(hc.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(hc.HttpfsControlMixin, "_httpfs_proc_state",
                        staticmethod(lambda pid: "Z"))
    assert fs._httpfs_proc_alive(1234) is False
    monkeypatch.setattr(hc.HttpfsControlMixin, "_httpfs_proc_state",
                        staticmethod(lambda pid: "S"))
    assert fs._httpfs_proc_alive(1234) is True


def test_I7_corrupt_pidfile_not_ours(fs):
    with open(fs._httpfs_pid_file, "w") as f:
        f.write("{not json")
    assert fs._httpfs_read_pidstate() is None
    assert fs._httpfs_is_ours() is False


# --------------------------------------------------------------------------- C
# stop escalation

def _kill_recorder(monkeypatch, raise_on=None):
    kills = []

    def fake_kill(pid, sig):
        kills.append(sig)
        if raise_on is not None and sig == raise_on:
            raise ProcessLookupError()
    monkeypatch.setattr(hc.os, "kill", fake_kill)
    return kills


def test_S1_exits_on_sigterm_no_sigkill(fs, monkeypatch):
    _write_pidstate(fs)
    monkeypatch.setattr(fs, "_httpfs_is_ours", lambda state=None: True)
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: False)  # gone after TERM
    kills = _kill_recorder(monkeypatch)
    fs._httpfs_stop_process(LOG)
    assert kills == [hc.signal.SIGTERM]
    assert fs._httpfs_read_pidstate() is None


def test_S2_ignores_sigterm_escalates_to_sigkill(fs, monkeypatch):
    _write_pidstate(fs)
    fs._HTTPFS_STOP_GRACE = 0.0
    monkeypatch.setattr(fs, "_httpfs_is_ours", lambda state=None: True)
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: True)  # never dies
    kills = _kill_recorder(monkeypatch)
    fs._httpfs_stop_process(LOG)
    assert kills == [hc.signal.SIGTERM, hc.signal.SIGKILL]
    assert fs._httpfs_read_pidstate() is None


def test_S3_not_ours_refuses_to_signal(fs, monkeypatch):
    _write_pidstate(fs)
    monkeypatch.setattr(fs, "_httpfs_is_ours", lambda state=None: False)
    kills = _kill_recorder(monkeypatch)
    fs._httpfs_stop_process(LOG)
    assert kills == []                       # never signalled a stranger
    assert fs._httpfs_read_pidstate() is None  # but dropped our stale pointer


def test_S4_already_dead_on_sigterm_is_graceful(fs, monkeypatch):
    _write_pidstate(fs)
    monkeypatch.setattr(fs, "_httpfs_is_ours", lambda state=None: True)
    monkeypatch.setattr(fs, "_httpfs_proc_alive", lambda pid: True)
    _kill_recorder(monkeypatch, raise_on=hc.signal.SIGTERM)
    fs._httpfs_stop_process(LOG)             # must not raise
    assert fs._httpfs_read_pidstate() is None


# --------------------------------------------------------------------------- D
# spawn readiness + typed errors

def _prep_spawn(fs, monkeypatch, *, deps=None, url=None, healthy_seq=None, poll=None):
    fake_cfg = types.SimpleNamespace(port=8889, dispatcharr_url="http://h:9191",
                                     to_env=lambda: {})
    import vodlib.config as vc
    monkeypatch.setattr(vc.MountConfig, "from_settings",
                        lambda *a, **k: fake_cfg, raising=True)
    monkeypatch.setattr(fs, "_httpfs_validate_base_url", lambda u: url)
    monkeypatch.setattr(fs, "_httpfs_ensure_dependencies", lambda exe, lg: deps)
    monkeypatch.setattr(fs, "_httpfs_log_path",
                        lambda: os.path.join(fs._HTTPFS_DATA_DIR, "server.log"))

    seq = list(healthy_seq or [])

    def fake_healthy(port, timeout=2.0):
        return seq.pop(0) if seq else False
    monkeypatch.setattr(fs, "_httpfs_healthy", fake_healthy)

    class FakeProc:
        pid = 4321

        def __init__(self):
            self.returncode = poll

        def poll(self):
            return poll
    monkeypatch.setattr(hc.subprocess, "Popen", lambda *a, **k: FakeProc())


def test_E1_spawn_becomes_healthy_returns_pid(fs, monkeypatch):
    # pre-check sees port free (False), readiness sees healthy (True)
    _prep_spawn(fs, monkeypatch, healthy_seq=[False, True], poll=None)
    assert fs._httpfs_spawn({}, LOG) == 4321
    assert fs._httpfs_read_pidstate()["pid"] == 4321


def test_E2_child_exits_during_startup_readiness_error(fs, monkeypatch):
    _prep_spawn(fs, monkeypatch, healthy_seq=[False], poll=1)  # exited rc=1
    with pytest.raises(hc._ReadinessError):
        fs._httpfs_spawn({}, LOG)


def test_E2_never_healthy_times_out_readiness_error(fs, monkeypatch):
    fs._HTTPFS_READY_TIMEOUT = 0.0
    _prep_spawn(fs, monkeypatch, healthy_seq=[False], poll=None)
    monkeypatch.setattr(fs, "_httpfs_stop_process", lambda *a, **k: None)
    with pytest.raises(hc._ReadinessError):
        fs._httpfs_spawn({}, LOG)


def test_E3_port_in_use_conflict(fs, monkeypatch):
    _prep_spawn(fs, monkeypatch, healthy_seq=[True], poll=None)  # pre-check: busy
    with pytest.raises(hc._PortInUse):
        fs._httpfs_spawn({}, LOG)


def test_E4_missing_deps_dep_error(fs, monkeypatch):
    _prep_spawn(fs, monkeypatch, deps="deps missing", healthy_seq=[False])
    with pytest.raises(hc._DepError):
        fs._httpfs_spawn({}, LOG)


def test_E5_bad_url_url_error(fs, monkeypatch):
    _prep_spawn(fs, monkeypatch, url="localhost not allowed", healthy_seq=[False])
    with pytest.raises(hc._UrlError):
        fs._httpfs_spawn({}, LOG)


# --------------------------------------------------------------------------- A
# reconcile decision cube  (D x O x P)

def _mock_reconcile_seams(fs, monkeypatch, ours, healthy, spawn=None):
    monkeypatch.setattr(fs, "_httpfs_is_ours", lambda state=None: ours)
    monkeypatch.setattr(fs, "_httpfs_healthy", lambda port, timeout=2.0: healthy)
    stop_calls, spawn_calls = [], []

    def fake_stop(logger, state=None):
        stop_calls.append(True)
        fs._httpfs_remove_pid_file()
    monkeypatch.setattr(fs, "_httpfs_stop_process", fake_stop)

    def fake_spawn(settings, logger):
        spawn_calls.append(True)
        if spawn is not None:
            raise spawn
        _write_pidstate(fs)
        return 4321
    monkeypatch.setattr(fs, "_httpfs_spawn", fake_spawn)
    return stop_calls, spawn_calls


CUBE = [
    # enabled, ours, healthy, action,      spawn?, stop?
    (True,  True,  True,  "NOOP",     False, False),  # R1
    (True,  True,  False, "RESTART",  True,  True),   # R2
    (True,  False, False, "START",    True,  False),  # R3
    (True,  False, True,  "CONFLICT", False, False),  # R4
    (False, True,  True,  "STOP",     False, True),   # R5
    (False, True,  False, "STOP",     False, True),   # R6
    (False, False, True,  "CLEAN",    False, False),  # R7
    (False, False, False, "CLEAN",    False, False),  # R8
]


@pytest.mark.parametrize("enabled,ours,healthy,action,spawned,stopped", CUBE)
def test_reconcile_cube(fs, monkeypatch, enabled, ours, healthy, action, spawned, stopped):
    fs._httpfs_write_state(enabled=enabled, fail_count=0)
    stop_calls, spawn_calls = _mock_reconcile_seams(fs, monkeypatch, ours, healthy)
    rc = fs._httpfs_reconcile({}, LOG)
    assert rc["action"] == action
    assert bool(spawn_calls) == spawned
    assert bool(stop_calls) == stopped


# --------------------------------------------------------------------------- #28
# crash-loop breaker

def test_T28_1_consecutive_readiness_failures_increment(fs, monkeypatch):
    fs._httpfs_write_state(enabled=True, fail_count=0)
    _, spawn_calls = _mock_reconcile_seams(
        fs, monkeypatch, ours=False, healthy=False,
        spawn=hc._ReadinessError("boom"))
    for _ in range(3):
        fs._httpfs_reconcile({}, LOG)
    assert len(spawn_calls) == 3                      # still trying (3 < N)
    assert fs._httpfs_state()["fail_count"] == 3


def test_T28_2_success_resets_counter(fs, monkeypatch):
    fs._httpfs_write_state(enabled=True, fail_count=3)
    _mock_reconcile_seams(fs, monkeypatch, ours=False, healthy=False)  # spawn succeeds
    rc = fs._httpfs_reconcile({}, LOG)
    assert rc["action"] == "START"
    assert fs._httpfs_state()["fail_count"] == 0


def test_T28_3_breaker_open_stops_trying(fs, monkeypatch):
    fs._httpfs_write_state(enabled=True, fail_count=fs._HTTPFS_MAX_FAILURES,
                           last_error="prior boom")
    _, spawn_calls = _mock_reconcile_seams(fs, monkeypatch, ours=False, healthy=False)
    rc = fs._httpfs_reconcile({}, LOG)
    assert rc["action"] == "FAILED"
    assert spawn_calls == []                          # no Popen attempt
    assert rc["reason"] == "prior boom"


def test_T28_4_enable_resets_breaker_and_retries(fs, monkeypatch):
    fs._httpfs_write_state(enabled=True, fail_count=fs._HTTPFS_MAX_FAILURES,
                           last_error="prior boom")
    _, spawn_calls = _mock_reconcile_seams(fs, monkeypatch, ours=False, healthy=False)
    monkeypatch.setattr(fs, "_httpfs_register_healthcheck", lambda s, l: None)
    res = fs.httpfs_enable({}, LOG)
    assert res["status"] == "ok"                      # forced past the breaker
    assert len(spawn_calls) == 1
    assert fs._httpfs_state()["fail_count"] == 0


@pytest.mark.parametrize("exc", [hc._DepError("d"), hc._UrlError("u"), hc._PortInUse("p")])
def test_T28_5_config_errors_do_not_count(fs, monkeypatch, exc):
    fs._httpfs_write_state(enabled=True, fail_count=0)
    _mock_reconcile_seams(fs, monkeypatch, ours=False, healthy=False, spawn=exc)
    rc = fs._httpfs_reconcile({}, LOG)
    assert rc["action"] == "CONFLICT"
    assert fs._httpfs_state()["fail_count"] == 0      # breaker untouched


def test_T28_5b_R4_foreign_holder_does_not_count(fs, monkeypatch):
    fs._httpfs_write_state(enabled=True, fail_count=0)
    _, spawn_calls = _mock_reconcile_seams(fs, monkeypatch, ours=False, healthy=True)
    rc = fs._httpfs_reconcile({}, LOG)
    assert rc["action"] == "CONFLICT"
    assert spawn_calls == []
    assert fs._httpfs_state()["fail_count"] == 0


def test_T28_6_tripped_breaker_survives_passive_reconcile(fs, monkeypatch):
    fs._httpfs_write_state(enabled=True, fail_count=fs._HTTPFS_MAX_FAILURES,
                           last_error="boom")
    _, spawn_calls = _mock_reconcile_seams(fs, monkeypatch, ours=False, healthy=False)
    rc = fs._httpfs_reconcile({}, LOG)                # a passive tick / interaction
    assert rc["action"] == "FAILED"
    assert spawn_calls == []                          # did not retry
    assert fs._httpfs_state()["fail_count"] == fs._HTTPFS_MAX_FAILURES  # not reset
