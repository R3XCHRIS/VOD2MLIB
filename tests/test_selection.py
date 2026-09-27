"""Tests for Selection mode.

No Django needed: the FakeCatalogue stands in for Dispatcharr's ORM.
"""
import json
import logging
import os
import socket
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from plugin import Plugin
from selection.apply import apply_pending
from selection.catalogue import make_fake_catalogue
from selection.server import SelectionService, _Server, make_handler
from selection.store import SelectionStore

LOG = logging.getLogger("test.selection")

SPEC = {
    "accounts": {1: "Provider A", 2: "Provider B"},
    "movies": [
        {"name": "EN - The Matrix (1999)", "year": 1999,
         "copies": [(1, 101, "EN - Sci-Fi"), (2, 201, "4K Movies")]},
        {"name": "Aladdin", "year": 1992, "copies": [(1, 102, "EN - Family")]},
        {"name": "Adult Thing", "year": 2020, "copies": [(2, 202, "FOR ADULTS")]},
    ],
    # Series copies are (account_id, external_series_id, category); episodes
    # per copy are (season, episode, title, stream_id).
    "series": [
        {"name": "EN - Breaking Bad (2008)", "year": 2008,
         "copies": [(1, "5001", "EN - Drama"), (2, "7001", "4K Series")],
         "episodes": {
             (1, "5001"): [(1, 1, "Pilot", 9001), (1, 2, "Cat's in the Bag", 9002),
                           (2, 1, "Seven Thirty-Seven", 9003)],
             # A German copy: same episodes, its own titles.
             (2, "7001"): [(1, 1, "Der Einstieg", 9101), (1, 2, "Die Katze ist im Sack", 9102),
                           (2, 1, "Sieben Dreiunddreißig", 9103)],
         }},
        {"name": "P+ - Empty Show (2024)", "year": 2024,
         "copies": [(2, "7002", "PARAMOUNT+")], "episodes": {}},
    ],
}
BREAKING_BAD = "00000000-0000-4000-9000-000000000001"
EMPTY_SHOW = "00000000-0000-4000-9000-000000000002"
MATRIX = "00000000-0000-4000-8000-000000000001"
ALADDIN = "00000000-0000-4000-8000-000000000002"
ADULT = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def env(tmp_path):
    plugin = Plugin()
    settings = {f["id"]: f["default"] for f in plugin.fields if "default" in f}
    settings.update(
        dispatcharr_url="http://10.0.0.5:9191",
        root_folder=str(tmp_path / "Movies"),
        series_root_folder=str(tmp_path / "Series"),
        selection_mode=True,
        selection_password="pw",
        category_exclude="FOR ADULTS",
    )
    store = SelectionStore(str(tmp_path / "db" / "selection.db"))
    catalogue = make_fake_catalogue(plugin, SPEC)
    return plugin, catalogue, store, settings, tmp_path


def _apply(env):
    plugin, catalogue, store, settings, _ = env
    return apply_pending(plugin, catalogue, store, settings, LOG)


def _matrix_folder(tmp_path):
    return tmp_path / "Movies" / "The Matrix (1999)"


# ---------- store ----------

class TestStore:
    def test_new_selection_is_pending_until_applied(self, env):
        _, _, store, _, _ = env
        store.set_desired("movie", MATRIX, True, 1, "101", title="Matrix")
        assert [r["content_uuid"] for r in store.pending()] == [MATRIX]
        store.mark_applied("movie", MATRIX)
        assert store.pending() == []

    def test_changing_copy_makes_it_pending_again(self, env):
        _, _, store, _, _ = env
        store.set_desired("movie", MATRIX, True, 1, "101")
        store.mark_applied("movie", MATRIX)
        store.set_desired("movie", MATRIX, True, 2, "201")
        assert len(store.pending()) == 1

    def test_select_then_unselect_before_apply_is_not_pending(self, env):
        _, _, store, _, _ = env
        store.set_desired("movie", MATRIX, True, 1, "101")
        store.set_desired("movie", MATRIX, False)
        assert store.pending() == []

    def test_counts(self, env):
        _, _, store, _, _ = env
        store.set_desired("movie", MATRIX, True, 1, "101")
        store.mark_applied("movie", MATRIX)
        store.set_desired("movie", ALADDIN, True, 1, "102")
        assert store.counts() == {"applied_selected": 1, "pending": 1,
                                  "movies_on_disk": 1, "series_on_disk": 0}


# ---------- apply ----------

class TestApply:
    def test_add_writes_strm_and_nfo_with_chosen_copy(self, env):
        _, _, store, _, tmp_path = env
        store.set_desired("movie", MATRIX, True, 2, "201", title="Matrix")
        result = _apply(env)
        assert result["added"] == 1 and result["errors"] == 0
        strm = _matrix_folder(tmp_path) / "The Matrix (1999).strm"
        assert strm.read_text() == f"http://10.0.0.5:9191/proxy/vod/movie/{MATRIX}?stream_id=201"
        assert (_matrix_folder(tmp_path) / "The Matrix (1999).nfo").exists()
        assert store.pending() == []

    def test_remove_deletes_files_and_empty_folder(self, env):
        _, _, store, _, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, "101")
        _apply(env)
        store.set_desired("movie", MATRIX, False)
        result = _apply(env)
        assert result["removed"] == 1
        assert not _matrix_folder(tmp_path).exists()
        assert (tmp_path / "Movies").is_dir()  # root survives

    def test_remove_keeps_user_files_and_their_folder(self, env):
        _, _, store, _, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, "101")
        _apply(env)
        (_matrix_folder(tmp_path) / "subs.en.srt").write_text("1")
        store.set_desired("movie", MATRIX, False)
        _apply(env)
        assert (_matrix_folder(tmp_path) / "subs.en.srt").exists()
        assert not (_matrix_folder(tmp_path) / "The Matrix (1999).strm").exists()

    def test_existing_nfo_is_not_overwritten_or_claimed(self, env):
        _, _, store, _, tmp_path = env
        _matrix_folder(tmp_path).mkdir(parents=True)
        nfo = _matrix_folder(tmp_path) / "The Matrix (1999).nfo"
        nfo.write_text("<movie>my edits</movie>")
        store.set_desired("movie", MATRIX, True, 1, "101")
        _apply(env)
        store.set_desired("movie", MATRIX, False)
        _apply(env)
        assert nfo.read_text() == "<movie>my edits</movie>"

    def test_copy_change_rewrites_url(self, env):
        _, _, store, _, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, "101")
        _apply(env)
        store.set_desired("movie", MATRIX, True, 2, "201")
        result = _apply(env)
        assert result["changed"] == 1
        strm = _matrix_folder(tmp_path) / "The Matrix (1999).strm"
        assert strm.read_text().endswith("stream_id=201")

    def test_vanished_copy_stays_pending_with_error(self, env):
        _, _, store, _, _ = env
        store.set_desired("movie", MATRIX, True, 1, "999")
        result = _apply(env)
        assert result["errors"] == 1
        [row] = store.pending()
        assert "no longer available" in row["last_error"]

    def test_placeholder_url_refused(self, env):
        plugin, catalogue, store, settings, _ = env
        settings["dispatcharr_url"] = ""
        store.set_desired("movie", MATRIX, True, 1, "101")
        result = apply_pending(plugin, catalogue, store, settings, LOG)
        assert result["status"] == "error"
        assert len(store.pending()) == 1


# ---------- catalogue ----------

class TestFakeCatalogue:
    def test_category_exclude_hides_titles(self, env):
        _, catalogue, _, settings, _ = env
        names = [i["name"] for i in catalogue.list_titles("movie", settings)["items"]]
        assert "Adult Thing" not in names and len(names) == 2

    def test_selected_title_visible_under_selected_even_if_excluded(self, env):
        _, catalogue, _, settings, _ = env
        items = catalogue.list_titles("movie", settings, include_uuids=[ADULT])["items"]
        assert [i["uuid"] for i in items] == [ADULT]

    def test_copies_listed_per_movie_best_first(self, env):
        _, catalogue, _, settings, _ = env
        matrix = [i for i in catalogue.list_titles("movie", settings, q="matrix")["items"]][0]
        # The "4K Movies" copy ranks above the one with no quality info.
        assert [(c["account_id"], c["stream_id"]) for c in matrix["copies"]] == [(2, "201"), (1, "101")]


# ---------- plugin guard ----------

def test_bulk_generate_paused_in_selection_mode():
    plugin = Plugin()
    result = plugin.run("generate_movies", {}, {"logger": LOG, "settings": {"selection_mode": True}})
    assert result["status"] == "error" and "Selection mode is ON" in result["message"]


# ---------- HTTP ----------

@pytest.fixture
def http(env):
    plugin, catalogue, store, settings, _ = env
    service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
    server = _Server(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, service
    server.shutdown()
    server.server_close()


def _req(base, method, path, body=None, cookie=None, csrf=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if csrf:
        req.add_header("X-VOD2MLIB", "1")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read() or b"{}"), res.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


def _login(base):
    status, _, headers = _req(base, "POST", "/api/login", {"password": "pw"})
    assert status == 200
    return headers["Set-Cookie"].split(";")[0]


class TestHttp:
    def test_api_requires_login(self, http):
        base, _ = http
        assert _req(base, "GET", "/api/movies")[0] == 401

    def test_wrong_password(self, http):
        base, _ = http
        assert _req(base, "POST", "/api/login", {"password": "nope"})[0] == 401

    def test_state_change_requires_csrf_header(self, http):
        base, _ = http
        cookie = _login(base)
        status, _, _ = _req(base, "PUT", f"/api/selection/movie/{ALADDIN}",
                            {"selected": True, "account_id": 1, "stream_id": "102"},
                            cookie=cookie, csrf=False)
        assert status == 403

    def test_select_apply_roundtrip(self, http, env):
        base, service = http
        tmp_path = env[4]
        cookie = _login(base)
        status, _, _ = _req(base, "PUT", f"/api/selection/movie/{ALADDIN}",
                            {"selected": True, "account_id": 1, "stream_id": "102", "title": "Aladdin"},
                            cookie=cookie)
        assert status == 200
        _, pending, _ = _req(base, "GET", "/api/pending", cookie=cookie)
        assert [(i["title"], i["action"]) for i in pending["items"]] == [("Aladdin", "add")]
        _, listing, _ = _req(base, "GET", "/api/movies?state=selected", cookie=cookie)
        assert [i["uuid"] for i in listing["items"]] == [ALADDIN]
        assert listing["items"][0]["pending"] is True

        _req(base, "POST", "/api/apply", cookie=cookie)
        for _ in range(100):
            _, job, _ = _req(base, "GET", "/api/job", cookie=cookie)
            if not job["running"]:
                break
            threading.Event().wait(0.05)
        assert job["result"]["added"] == 1
        assert (tmp_path / "Movies" / "Aladdin (1992)" / "Aladdin (1992).strm").exists()

    def test_index_served(self, http):
        base, _ = http
        with urllib.request.urlopen(base + "/") as res:
            assert b"VOD Selection" in res.read()


# ---------- hosting and Page status ----------

from selection import runtime, server as page_server


class TestPageHost:
    def test_only_daphne_hosts_the_page(self):
        assert runtime.is_page_host(
            ["/dispatcharrpy/bin/daphne", "-b", "0.0.0.0", "-p", "8001", "dispatcharr.asgi:application"])
        # uWSGI workers run streaming under gevent; the dvr worker never loads plugins.
        assert not runtime.is_page_host(["/dispatcharrpy/bin/uwsgi", "--ini", "/app/docker/uwsgi.ini"])
        assert not runtime.is_page_host(
            ["/dispatcharrpy/bin/celery", "-A", "dispatcharr", "worker", "-Q", "dvr"])
        assert not runtime.is_page_host(["python", "-m", "pytest"])


class TestOwnerRecord:
    def test_roundtrip(self, env):
        store = env[2]
        assert store.owner() is None
        store.set_owner(pid=123, port=9192)
        owner = store.owner()
        assert owner["pid"] == 123 and owner["port"] == 9192 and owner["started_at"] > 0

    def test_only_the_owner_clears_it(self, env):
        # A process that lost the port must not wipe the new owner's record.
        store = env[2]
        store.set_owner(pid=123, port=9192)
        store.clear_owner(pid=999)
        assert store.owner()["pid"] == 123
        store.clear_owner(pid=123)
        assert store.owner() is None

    def test_binding_records_and_stopping_clears(self, env):
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        try:
            assert page_server.start_server(service, 0, host="127.0.0.1")
            assert store.owner()["pid"] == os.getpid()
        finally:
            page_server.stop_server()
        assert store.owner() is None


@pytest.fixture
def served(env):
    plugin, catalogue, store, settings, _ = env
    service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
    page_server.start_server(service, 0, host="127.0.0.1")
    settings["selection_port"] = store.owner()["port"]
    yield plugin, settings, service
    page_server.stop_server()


class TestPageStatus:
    def test_running_reports_owner_and_host_port_hint(self, served):
        plugin, settings, service = served
        r = runtime.status(plugin, settings, service=service)
        assert r["status"] == "ok" and r["running"]
        assert r["pid"] == os.getpid()
        assert "container port" in r["message"] and "publish" in r["message"]
        assert "writable" in r["message"]

    def test_not_running(self, env):
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        settings["selection_port"] = free_port
        r = runtime.status(plugin, settings, service=service)
        assert r["status"] == "error" and not r["running"]
        assert "not running" in r["message"]

    def test_unwritable_root_is_reported(self, served, tmp_path):
        plugin, settings, service = served
        blocker = tmp_path / "a-file"
        blocker.write_text("x")
        settings["root_folder"] = str(blocker)
        r = runtime.status(plugin, settings, service=service)
        assert r["status"] == "error"
        assert str(blocker) in r["message"]


class TestApplyRootErrors:
    def test_uncreatable_root_is_a_clear_error_not_a_crash(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        blocker = tmp_path / "a-file"
        blocker.write_text("x")
        settings["root_folder"] = str(blocker / "Movies")
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        r = _apply(env)
        assert r["status"] == "error"
        assert str(blocker / "Movies") in r["message"]
        assert "crash" not in r["message"].lower()


# ---------- series ----------

def _bb_season1(tmp_path):
    return tmp_path / "Series" / "Breaking Bad (2008)" / "Season 01"


class TestSeriesApply:
    def test_add_writes_tvshow_nfo_and_every_episode(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        r = _apply(env)
        assert r["added"] == 1 and r["errors"] == 0
        show = tmp_path / "Series" / "Breaking Bad (2008)"
        assert (show / "tvshow.nfo").is_file()
        strm = _bb_season1(tmp_path) / "Breaking Bad - S01E01 - Pilot.strm"
        assert strm.is_file() and "9001" in strm.read_text()
        assert (_bb_season1(tmp_path) / "Breaking Bad - S01E01 - Pilot.nfo").is_file()
        assert (show / "Season 02" / "Breaking Bad - S02E01 - Seven Thirty-Seven.strm").is_file()
        assert store.pending() == []

    def test_remove_deletes_everything_and_prunes_folders(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        store.set_desired("series", BREAKING_BAD, False)
        r = _apply(env)
        assert r["removed"] == 1
        assert not (tmp_path / "Series" / "Breaking Bad (2008)").exists()
        assert (tmp_path / "Series").is_dir()

    def test_copy_change_rewrites_episode_urls(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        store.set_desired("series", BREAKING_BAD, True, 2, "7001", "Breaking Bad")
        r = _apply(env)
        assert r["changed"] == 1
        # Named from the chosen copy's own episode titles, not the shared ones.
        assert not (_bb_season1(tmp_path) / "Breaking Bad - S01E01 - Pilot.strm").exists()
        strm = _bb_season1(tmp_path) / "Breaking Bad - S01E01 - Der Einstieg.strm"
        assert "9101" in strm.read_text()
        assert "<title>Der Einstieg</title>" in strm.with_suffix(".nfo").read_text(encoding="utf-8")

    def test_series_without_episodes_stays_pending_with_error(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", EMPTY_SHOW, True, 2, "7002", "Empty Show")
        r = _apply(env)
        assert r["errors"] == 1
        row = store.pending()[0]
        assert "no episodes" in row["last_error"].lower()
        assert not (tmp_path / "Series" / "Empty Show (2024)").exists()

    def test_movies_and_series_apply_together(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        r = _apply(env)
        assert r["added"] == 2
        assert _matrix_folder(tmp_path).is_dir() and _bb_season1(tmp_path).is_dir()

    def test_unwritable_series_root_only_blocks_when_series_are_pending(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        blocker = tmp_path / "a-file"
        blocker.write_text("x")
        settings["series_root_folder"] = str(blocker / "Series")
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        assert _apply(env)["added"] == 1
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        r = _apply(env)
        assert r["status"] == "error" and "Series root folder" in r["message"]


class TestSeriesHttp:
    def test_list_select_and_pending(self, http):
        base, _ = http
        cookie = _login(base)
        status, data, _ = _req(base, "GET", "/api/series", cookie=cookie)
        assert status == 200
        names = [i["name"] for i in data["items"]]
        assert names == ["EN - Breaking Bad (2008)", "P+ - Empty Show (2024)"]
        bb = data["items"][0]
        assert [c["stream_id"] for c in bb["copies"]] == ["7001", "5001"]  # "4K Series" first
        status, _, _ = _req(base, "PUT", f"/api/selection/series/{BREAKING_BAD}",
                         {"selected": True, "account_id": 2, "stream_id": "7001",
                          "title": bb["name"]}, cookie=cookie)
        assert status == 200
        _, data, _ = _req(base, "GET", "/api/series?state=selected", cookie=cookie)
        assert [i["uuid"] for i in data["items"]] == [BREAKING_BAD]
        assert data["items"][0]["chosen"] == {"account_id": 2, "stream_id": "7001"}
        _, pending, _ = _req(base, "GET", "/api/pending", cookie=cookie)
        assert [(i["kind"], i["action"]) for i in pending["items"]] == [("series", "add")]


# ---------- seasons ----------

class TestSeasonStore:
    def test_v1_database_is_migrated_and_keeps_selections(self, tmp_path):
        import sqlite3
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '1');
            CREATE TABLE selection (
                kind TEXT NOT NULL, content_uuid TEXT NOT NULL, title TEXT,
                desired_selected INTEGER NOT NULL DEFAULT 0, desired_account_id INTEGER,
                desired_stream_id TEXT, applied_selected INTEGER NOT NULL DEFAULT 0,
                applied_account_id INTEGER, applied_stream_id TEXT, last_error TEXT,
                updated_at REAL, PRIMARY KEY (kind, content_uuid));
            INSERT INTO selection (kind, content_uuid, title, desired_selected, desired_account_id,
                desired_stream_id, applied_selected, applied_account_id, applied_stream_id)
                VALUES ('series', 'u1', 'Yellowstone', 1, 1, '5001', 1, 1, '5001');
            CREATE TABLE applied_file (path TEXT PRIMARY KEY, kind TEXT NOT NULL,
                content_uuid TEXT NOT NULL);
            INSERT INTO applied_file VALUES ('/s/tvshow.nfo', 'series', 'u1');
        """)
        conn.commit()
        conn.close()
        store = SelectionStore(str(path))
        row = store.get_many("series", ["u1"])["u1"]
        assert row["applied_selected"] == 1
        assert store.excluded_seasons(row) == []
        assert store.pending() == []
        assert store.files_for("series", "u1") == ["/s/tvshow.nfo"]
        assert store.adopted_paths("series", "u1") == set()
        from selection.store import SCHEMA_VERSION
        assert store.schema_version() == SCHEMA_VERSION
        SelectionStore(str(path))  # opening again is a no-op

    def test_excluding_a_season_is_pending_until_applied(self, env):
        plugin, catalogue, store, settings, _ = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        store.mark_applied("series", BREAKING_BAD)
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", excluded_seasons=[2])
        assert [r["content_uuid"] for r in store.pending()] == [BREAKING_BAD]
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", excluded_seasons=[])
        assert store.pending() == []

    def test_copy_change_keeps_exclusions_and_unselect_clears_them(self, env):
        plugin, catalogue, store, settings, _ = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", excluded_seasons=[2])
        store.set_desired("series", BREAKING_BAD, True, 2, "7001")
        row = store.get_many("series", [BREAKING_BAD])[BREAKING_BAD]
        assert store.excluded_seasons(row, "desired") == [2]
        store.set_desired("series", BREAKING_BAD, False)
        row = store.get_many("series", [BREAKING_BAD])[BREAKING_BAD]
        assert store.excluded_seasons(row, "desired") == []


def _bb_show(tmp_path):
    return tmp_path / "Series" / "Breaking Bad (2008)"


def _add_bb_episode(catalogue, season, number, title, stream_id):
    from types import SimpleNamespace
    rel = catalogue.relations["series"][0]
    catalogue.episodes[rel.id].append(SimpleNamespace(
        episode=SimpleNamespace(
            uuid=f"00000000-0000-4000-a000-new{season:04d}{number:05d}", name=title,
            season_number=season, episode_number=number, description="", air_date=None,
            rating="", duration_secs=None, tmdb_id="", imdb_id="", custom_properties={}),
        stream_id=str(stream_id), custom_properties={"info": {"title": title}}))


class TestSeasonApply:
    def test_add_with_an_excluded_season_writes_only_the_rest(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad", excluded_seasons=[2])
        assert _apply(env)["added"] == 1
        assert _bb_season1(tmp_path).is_dir()
        assert not (_bb_show(tmp_path) / "Season 02").exists()

    def test_excluding_later_removes_only_that_season_and_leaves_the_rest_alone(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        pilot = _bb_season1(tmp_path) / "Breaking Bad - S01E01 - Pilot.strm"
        os.utime(pilot, (1_000_000, 1_000_000))
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", excluded_seasons=[2])
        r = _apply(env)
        assert r["changed"] == 1 and r["errors"] == 0
        assert not (_bb_show(tmp_path) / "Season 02").exists()
        assert pilot.stat().st_mtime == 1_000_000  # untouched, not rewritten
        assert (_bb_show(tmp_path) / "tvshow.nfo").is_file()
        assert all("Season 02" not in p for p in store.files_for("series", BREAKING_BAD))
        assert store.pending() == []

    def test_re_including_a_season_writes_it_again(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad", excluded_seasons=[2])
        _apply(env)
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", excluded_seasons=[])
        _apply(env)
        assert (_bb_show(tmp_path) / "Season 02" / "Breaking Bad - S02E01 - Seven Thirty-Seven.strm").is_file()

    def test_a_new_season_is_included_automatically(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad", excluded_seasons=[2])
        _apply(env)
        _add_bb_episode(catalogue, 3, 1, "No Mas", 9004)
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", excluded_seasons=[1, 2])
        _apply(env)
        assert (_bb_show(tmp_path) / "Season 03" / "Breaking Bad - S03E01 - No Mas.strm").is_file()
        assert not _bb_season1(tmp_path).exists()

    def test_excluding_every_season_is_an_error_and_keeps_the_files(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", excluded_seasons=[1, 2])
        r = _apply(env)
        assert r["errors"] == 1 and "every season" in r["failures"][0]
        assert _bb_season1(tmp_path).is_dir()

    def test_movie_copy_change_to_same_path_keeps_files_claimed(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        _apply(env)
        store.set_desired("movie", MATRIX, True, 2, 201, "The Matrix")
        assert _apply(env)["changed"] == 1
        strm = next(_matrix_folder(tmp_path).glob("*.strm"))
        assert "201" in strm.read_text()
        assert sorted(os.path.basename(p) for p in store.files_for("movie", MATRIX)) == sorted(
            p.name for p in _matrix_folder(tmp_path).iterdir())


class TestSeasonHttp:
    def test_seasons_listed_for_a_copy_and_exclusion_saved(self, http):
        base, _ = http
        cookie = _login(base)
        status, data, _ = _req(
            base, "GET", f"/api/series/{BREAKING_BAD}/seasons?account_id=1&stream_id=5001",
            cookie=cookie)
        assert status == 200
        assert data["seasons"] == [{"season": 1, "episodes": 2}, {"season": 2, "episodes": 1}]
        assert data["excluded"] == []
        status, _, _ = _req(base, "PUT", f"/api/selection/series/{BREAKING_BAD}",
                            {"selected": True, "account_id": 1, "stream_id": "5001",
                             "title": "Breaking Bad", "excluded_seasons": [2]}, cookie=cookie)
        assert status == 200
        _, data, _ = _req(base, "GET", "/api/series?state=selected", cookie=cookie)
        assert data["items"][0]["excluded_seasons"] == [2]
        _, data, _ = _req(
            base, "GET", f"/api/series/{BREAKING_BAD}/seasons?account_id=1&stream_id=5001",
            cookie=cookie)
        assert data["excluded"] == [2]

    def test_unknown_copy_is_a_404(self, http):
        base, _ = http
        cookie = _login(base)
        status, _, _ = _req(
            base, "GET", f"/api/series/{BREAKING_BAD}/seasons?account_id=1&stream_id=nope",
            cookie=cookie)
        assert status == 404


# ---------- copy info + ranking ----------

from selection.copyinfo import copy_info, language_of, quality_of_video, rank


class TestCopyInfo:
    @pytest.mark.parametrize("text,lang", [
        ("EN - The Matrix (1999)", "EN"),
        ("DE - Star Trek: Discovery (2017) (US)", "DE"),
        ("|FR| Amélie", "FR"),
        ("NL| Title", "NL"),
        ("▪NL▪ Title", "NL"),
        ("▪MULTIG▪ Title", "MULTI"),
        ("EN-TOP - 02. The Godfather (1972)", "EN"),
        ("GER - Title", "DE"),
        ("EN Title", "EN"),
        ("EN - IMDB TOP 250", "EN"),
        # not languages
        ("P+ - Star Trek: Discovery (2017)", None),
        ("PARAMOUNT+", None),
        ("4K Movies", None),
        ("AC-130 (2020)", None),
        ("IT Chapter Two", None),
        ("UP (2009)", None),
        ("TV - Something", None),
        ("", None),
    ])
    def test_language_of(self, text, lang):
        assert language_of(text) == lang

    @pytest.mark.parametrize("w,h,q", [
        (3840, 2160, "2160"), (3840, 1600, "2160"), (1920, 1080, "1080"),
        (1920, 800, "1080"), (1920, 960, "1080"), (1280, 720, "720"),
        (1280, 536, "720"), (720, 576, "SD"), (None, None, None), ("x", None, None),
    ])
    def test_quality_from_frame_size(self, w, h, q):
        assert quality_of_video(w, h) == q

    def test_video_data_beats_name_tokens(self):
        info = copy_info("4K - Title", "", {"video": {"width": 1920, "height": 800, "codec_name": "h264"},
                                             "bitrate": 8209})
        assert info == {"quality": "1080", "height": 800, "video_codec": "H264",
                        "bitrate_kbps": 8209, "langs": [], "subtitle_langs": [], "source": "provider"}

    def test_name_then_category_for_language_and_quality(self):
        assert copy_info("Title", "EN - 4K Movies", None)["langs"] == ["EN"]
        assert copy_info("DE - Title", "EN - Movies", None)["langs"] == ["DE"]
        assert copy_info("Title", "EN - 4K Movies", None)["quality"] == "2160"
        assert copy_info("Title 1080p", "", {})["quality"] == "1080"
        assert copy_info("Title", "", {"video": None, "bitrate": None})["quality"] is None

    def test_rank_quality_then_bitrate_then_stable(self):
        def c(aid, sid, quality=None, kbps=None):
            return {"account_id": aid, "stream_id": sid, "info": {"quality": quality, "bitrate_kbps": kbps}}
        copies = [c(1, "a"), c(1, "b", "720"), c(2, "c", "1080", 4774), c(2, "d", "1080", 8209), c(1, "e", "2160")]
        assert [x["stream_id"] for x in rank(copies)] == ["e", "d", "c", "b", "a"]


STAR_TREK_SPEC = {
    "accounts": {2: "Strong"},
    "series": [
        {"name": "P+ - Star Trek: Discovery (2017)", "year": 2017,
         "copies": [(2, "19854", "PARAMOUNT+", {"basic_data": {"name": "P+ - Star Trek: Discovery (2017)"}}),
                    (2, "29846", "PARAMOUNT+", {"basic_data": {"name": "DE - Star Trek: Discovery (2017) (US)"}})],
         "episodes": {
             (2, "19854"): [(1, 1, "Context Is for Kings", 1,
                             {"video": {"width": 1920, "height": 1080, "codec_name": "h264"}, "bitrate": 8209})],
             (2, "29846"): [(1, 1, "Kontext ist für Könige", 2,
                             {"video": {"width": 1920, "height": 960, "codec_name": "h264"}, "bitrate": 4774})],
         }},
        {"name": "Never Fetched", "year": 2020, "copies": [(2, "1", "EN - Drama")]},
    ],
}


class TestCopyInfoListing:
    def test_series_copies_carry_info_from_episodes_and_own_name(self):
        catalogue = make_fake_catalogue(Plugin(), STAR_TREK_SPEC)
        items = catalogue.list_titles("series", {})["items"]
        st = next(i for i in items if "Star Trek" in i["name"])
        en, de = st["copies"]  # same quality, higher bitrate first
        assert (en["stream_id"], en["info"]["quality"], en["info"]["bitrate_kbps"], en["info"]["langs"]) == \
            ("19854", "1080", 8209, [])
        assert (de["stream_id"], de["info"]["quality"], de["info"]["langs"]) == ("29846", "1080", ["DE"])
        never = next(i for i in items if i["name"] == "Never Fetched")["copies"][0]["info"]
        assert never["quality"] is None and never["langs"] == ["EN"]

    def test_info_reaches_the_page_api(self, http):
        base, _ = http
        cookie = _login(base)
        _, data, _ = _req(base, "GET", "/api/movies?q=matrix", cookie=cookie)
        assert data["items"][0]["copies"][0]["info"]["quality"] == "2160"
        assert data["items"][0]["copies"][1]["info"]["langs"] == ["EN"]


# ---------- deep probe ----------

import subprocess
from types import SimpleNamespace
from selection import probe

# Trimmed from a real probe of Star Trek: Discovery S01E01 (English copy).
FFPROBE_EN = {"streams": [
    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "disposition": {"default": 1}},
    {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "spa"}, "disposition": {"default": 0}},
    {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "eng"}, "disposition": {"default": 1}},
    {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "cze"}, "disposition": {"default": 0}},
    {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "und"}},
    {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "bul"}, "disposition": {"default": 1}},
    {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "nob"}},
    {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
]}


class TestProbeParse:
    def test_real_output(self):
        assert probe.parse(FFPROBE_EN) == {
            "quality": "1080", "height": 1080, "video_codec": "H264",
            "langs": ["EN", "ES", "CS"], "audio_tracks": 4,
            "subtitle_langs": ["BG", "EN", "NO"],
        }

    def test_lang_codes(self):
        assert [probe.lang_code(t) for t in ("eng", "ger", "deu", "fr", "und", "", None, "xyz")] ==             ["EN", "DE", "DE", "FR", None, None, None, "XYZ"]


def _proc(rc, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class TestProbeRun:
    def test_busy_is_retried_then_succeeds(self):
        replies = [_proc(1, err="Server returned 5XX Server Error reply"), _proc(0, json.dumps(FFPROBE_EN))]
        sleeps = []
        info = probe.run("http://x", runner=lambda *a, **k: replies.pop(0), sleep=sleeps.append)
        assert info["langs"][0] == "EN" and sleeps == [probe.BUSY_DELAY]

    def test_always_busy_gives_a_clear_error(self):
        with pytest.raises(probe.ProbeError, match="no free connection"):
            probe.run("http://x", runner=lambda *a, **k: _proc(1, err="Server returned 5XX"), sleep=lambda s: None)

    def test_other_failures_are_not_retried(self):
        calls = []
        def runner(*a, **k):
            calls.append(1)
            return _proc(1, err="line one" + chr(10) + "http://x: Server returned 404 Not Found")
        with pytest.raises(probe.ProbeError, match="404"):
            probe.run("http://x", runner=runner, sleep=lambda s: None)
        assert len(calls) == 1

    def test_failure_without_error_text(self):
        for err in ("", chr(10), "  " + chr(10)):
            with pytest.raises(probe.ProbeError, match="ffprobe failed"):
                probe.run("http://x", runner=lambda *a, **k: _proc(1, err=err), sleep=lambda s: None)

    def test_timeout_and_missing_ffprobe(self):
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired("ffprobe", 60)
        def missing(*a, **k):
            raise FileNotFoundError()
        with pytest.raises(probe.ProbeError, match="no answer"):
            probe.run("http://x", runner=timeout)
        with pytest.raises(probe.ProbeError, match="not installed"):
            probe.run("http://x", runner=missing)


class TestProbeTitle:
    def test_probes_every_copy_through_the_proxy_and_stores_results(self, http, env):
        base, service = http
        urls = []
        def fake_run(url):
            urls.append(url)
            if "stream_id=9101" in url:
                raise probe.ProbeError("the provider has no free connection (all in use); try again later")
            return probe.parse(FFPROBE_EN)
        service.probe_run = fake_run
        cookie = _login(base)
        status, data, _ = _req(base, "POST", f"/api/probe/series/{BREAKING_BAD}", cookie=cookie)
        assert status == 200 and data["ok"]  # a background job; the page polls /api/probe-job
        job = _wait_probe_job(service)
        assert (job["result"]["probed"], job["result"]["failed"]) == (1, 1)
        # One sample episode per copy, through Dispatcharr's own proxy.
        assert sorted(urls) == [f"http://127.0.0.1:9191/proxy/vod/episode/00000000-0000-4000-a000-000100010001?stream_id={sid}"
                                for sid in (9001, 9101)]
        _, listing, _ = _req(base, "GET", "/api/series?q=breaking", cookie=cookie)
        copies = listing["items"][0]["copies"]
        probed = next(c for c in copies if c["stream_id"] == "5001")["info"]
        assert (probed["source"], probed["langs"], probed["subtitle_langs"], probed["quality"]) ==             ("deep", ["EN", "ES", "CS"], ["BG", "EN", "NO"], "1080")
        failed = next(c for c in copies if c["stream_id"] == "7001")["info"]
        assert failed["source"] == "provider" and "no free connection" in failed["probe_error"]
        # The probed 1080p copy now ranks above the "4K Series" one? No: 2160 from the
        # category still wins; probing never hides a better copy.
        assert [c["stream_id"] for c in copies] == ["7001", "5001"]

    def test_a_probe_can_reorder_copies(self, http):
        base, service = http
        service.probe_run = lambda url: dict(probe.parse(FFPROBE_EN), quality="2160")
        cookie = _login(base)
        _req(base, "POST", f"/api/probe/movie/{MATRIX}", cookie=cookie)
        _wait_probe_job(service)
        _, listing, _ = _req(base, "GET", "/api/movies?q=matrix", cookie=cookie)
        assert all(c["info"]["source"] == "deep" for c in listing["items"][0]["copies"])

    def test_failed_reprobe_keeps_the_last_good_result(self, env):
        plugin, catalogue, store, settings, _ = env
        store.save_probe("movie", 1, "101", result={"quality": "1080", "langs": ["EN"]})
        store.save_probe("movie", 1, "101", error="busy")
        row = store.probes("movie", [(1, "101")])[(1, "101")]
        assert row["result"]["langs"] == ["EN"] and row["error"] == "busy"

    def test_unknown_title_is_a_404(self, http):
        base, _ = http
        cookie = _login(base)
        unknown = "00000000-0000-4000-8000-00000000abcd"
        assert _req(base, "POST", f"/api/probe/movie/{unknown}", cookie=cookie)[0] == 404
        assert _req(base, "POST", "/api/probe/movie/nope", cookie=cookie)[0] == 400  # not a title id


def _wait_probe_job(service, timeout=10):
    import time as _t
    end = _t.time() + timeout
    while _t.time() < end:
        job = service.probe_job()
        if not job.get("running"):
            return job
        _t.sleep(0.05)
    raise AssertionError("probe job did not finish")


class TestProbeTitleJob:
    """The per-title Probe button: a background job (a title can have a dozen
    copies at ~5 s each), skipping copies probed recently, chosen copy first."""

    def _service(self, env, run):
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        service.probe_run = run
        return service, store

    def test_chosen_copy_is_probed_first(self, env):
        urls = []
        service, store = self._service(env, lambda url: urls.append(url) or probe.parse(FFPROBE_EN))
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")  # 201 (4K) ranks higher
        assert service.start_probe_title("movie", MATRIX)["ok"]
        assert _wait_probe_job(service)["result"]["probed"] == 2
        assert ["stream_id=101" in urls[0], "stream_id=201" in urls[1]] == [True, True]

    def test_recently_probed_copies_are_skipped(self, env):
        urls = []
        service, store = self._service(env, lambda url: urls.append(url) or probe.parse(FFPROBE_EN))
        store.save_probe("movie", 2, "201", result={"langs": ["EN"]})
        service.start_probe_title("movie", MATRIX)
        assert _wait_probe_job(service)["result"]["probed"] == 1
        assert len(urls) == 1 and "stream_id=101" in urls[0]

    def test_every_copy_fresh_says_so(self, env):
        service, store = self._service(env, lambda url: probe.parse(FFPROBE_EN))
        store.save_probe("movie", 1, "101", result={"langs": ["EN"]})
        store.save_probe("movie", 2, "201", result={"langs": ["EN"]})
        service.start_probe_title("movie", MATRIX)
        assert "probed in the last 7 days" in _wait_probe_job(service)["result"]["message"]

    def test_one_probe_job_at_a_time(self, env):
        import threading as _th
        gate = _th.Event()
        service, store = self._service(env, lambda url: gate.wait(5) and probe.parse(FFPROBE_EN))
        assert service.start_probe_title("movie", MATRIX)["ok"]
        assert not service.start_probe_all()["ok"]
        assert not service.start_probe_title("movie", ALADDIN)["ok"]
        gate.set()
        _wait_probe_job(service)

    def test_unknown_title(self, env):
        service, _ = self._service(env, lambda url: probe.parse(FFPROBE_EN))
        with pytest.raises(LookupError):
            service.start_probe_title("movie", "nope")


class TestProbeAllSelected:
    def _service(self, env, run):
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        service.probe_run = run
        return service, store

    def test_probes_every_copy_of_selected_titles_only(self, env):
        urls = []
        service, store = self._service(env, lambda url: urls.append(url) or probe.parse(FFPROBE_EN))
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        store.set_desired("movie", ALADDIN, False)
        assert service.start_probe_all()["ok"]
        job = _wait_probe_job(service)
        # Matrix has 2 copies, Breaking Bad 2: all four, nothing else.
        assert (job["done"], job["total"], job["result"]["probed"]) == (4, 4, 4)
        assert sum("/movie/" in u for u in urls) == 2 and sum("/episode/" in u for u in urls) == 2

    def test_recently_probed_copies_are_skipped(self, env):
        service, store = self._service(env, lambda url: probe.parse(FFPROBE_EN))
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        store.save_probe("movie", 1, "101", result={"langs": ["EN"]})
        store.save_probe("movie", 2, "201", result={"langs": ["EN"]}, error="busy")  # last try failed
        assert [(k, r.stream_id) for k, _, r in service.copies_to_probe()] == [("movie", "201")]
        import time as _t
        later = _t.time() + 8 * 24 * 3600
        assert len(service.copies_to_probe(now=later)) == 2

    def test_nothing_to_do_says_so(self, env):
        service, store = self._service(env, lambda url: probe.parse(FFPROBE_EN))
        service.start_probe_all()
        assert "Nothing to probe" in _wait_probe_job(service)["result"]["message"]

    def test_stops_when_the_provider_stays_busy(self, env):
        def busy(url):
            raise probe.ProbeError("the provider has no free connection (all in use); try again later", busy=True)
        service, store = self._service(env, busy)
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        service.start_probe_all()
        job = _wait_probe_job(service)
        assert job["done"] == 3 and job["total"] == 4
        assert job["result"]["message"].startswith("Stopped: the provider has had no free connection")

    def test_stop_button(self, env):
        started = threading.Event()
        release = threading.Event()
        def slow(url):
            started.set()
            release.wait(5)
            return probe.parse(FFPROBE_EN)
        service, store = self._service(env, slow)
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        service.start_probe_all()
        started.wait(5)
        assert not service.start_probe_all()["ok"]  # one job at a time
        service.stop_probe_all()
        release.set()
        job = _wait_probe_job(service)
        assert job["done"] == 1 and job["result"]["message"].startswith("Stopped")

    def test_http_routes(self, http, env):
        base, service = http
        service.probe_run = lambda url: probe.parse(FFPROBE_EN)
        env[2].set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        cookie = _login(base)
        status, data, _ = _req(base, "POST", "/api/probe-all", cookie=cookie)
        assert status == 200 and data["ok"]
        _wait_probe_job(service)
        _, job, _ = _req(base, "GET", "/api/probe-job", cookie=cookie)
        assert job["result"]["probed"] == 2


# ---------- preferences + ranking ----------

from selection.copyinfo import DEFAULT_PREFS, rank_key


def _c(sid, langs=(), subs=(), quality="1080", kbps=None, deep=True):
    return {"account_id": 1, "stream_id": sid,
            "info": {"quality": quality, "bitrate_kbps": kbps, "langs": list(langs),
                     "subtitle_langs": list(subs), "source": "deep" if deep else "provider"}}


def _order(copies, **prefs):
    return [c["stream_id"] for c in rank(copies, dict(DEFAULT_PREFS, **prefs))]


class TestPreferenceRanking:
    def test_audio_match_beats_unknown_beats_mismatch_even_over_quality(self):
        copies = [_c("de4k", ["DE"], quality="2160"), _c("unknown", deep=False), _c("en", ["DE", "EN"], quality="720")]
        assert _order(copies, audio=["EN"]) == ["en", "unknown", "de4k"]
        assert _order(copies) == ["de4k", "unknown", "en"]  # no preferences: quality first

    def test_subtitles_known_only_after_a_probe(self):
        copies = [_c("probed_none", ["EN"], []), _c("unprobed", ["EN"], deep=False), _c("nl", ["EN"], ["NL"])]
        assert _order(copies, subtitles=["NL"]) == ["nl", "unprobed", "probed_none"]

    def test_subtitles_are_a_preference_not_a_requirement(self):
        # On a real catalogue, every movie without subtitle tracks was marked ✗.
        from selection.copyinfo import language_match, MATCH
        no_subs = _c("uhd_no_subs", ["EN"], [], quality="2160")
        with_subs = _c("fhd_nl", ["EN"], ["NL"], quality="1080")
        prefs = dict(DEFAULT_PREFS, audio=["EN"], subtitles=["NL"])
        assert language_match(no_subs["info"], prefs) == MATCH
        assert _order([with_subs, no_subs], audio=["EN"], subtitles=["NL"]) == ["uhd_no_subs", "fhd_nl"]

    def test_quality_order_preference(self):
        copies = [_c("uhd", ["EN"], quality="2160"), _c("fhd", ["EN"], quality="1080")]
        assert _order(copies, quality_order=["1080", "2160", "720", "SD"]) == ["fhd", "uhd"]

    def test_default_audio_track_breaks_ties_before_bitrate(self):
        copies = [_c("cs_default", ["CS", "EN"], kbps=9000), _c("en_default", ["EN", "ES"], kbps=8000)]
        assert _order(copies, audio=["EN"]) == ["en_default", "cs_default"]
        assert _order(copies) == ["cs_default", "en_default"]  # no audio preference: bitrate

    def test_probed_beats_unprobed_when_otherwise_equal(self):
        copies = [_c("tagged", ["EN"], deep=False), _c("probed", ["EN"])]
        assert _order(copies) == ["probed", "tagged"]


class TestPreferencesStore:
    def test_defaults_and_roundtrip(self, env):
        store = env[2]
        assert store.prefs() == DEFAULT_PREFS
        store.set_prefs({"audio": ["EN"], "subtitles": [], "quality_order": ["1080", "2160", "720", "SD"]})
        assert store.prefs()["audio"] == ["EN"] and store.prefs()["quality_order"][0] == "1080"


class TestPreferencesService:
    def _service(self, env):
        plugin, catalogue, store, settings, _ = env
        return SelectionService(plugin, catalogue, store, lambda: settings, LOG), store

    def test_better_copy_hint_for_selected_titles_only(self, env):
        service, store = self._service(env)
        # Breaking Bad: 7001 is in "4K Series" (ranked first); select the other one.
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        items = {i["uuid"]: i for i in service.list_titles("series")["items"]}
        assert items[BREAKING_BAD]["better_copy"] == {"account_id": 2, "stream_id": "7001"}
        assert items[EMPTY_SHOW]["better_copy"] is None  # not selected
        store.set_desired("series", BREAKING_BAD, True, 2, "7001")
        assert service.list_titles("series")["items"][0]["better_copy"] is None

    def test_equal_copies_give_no_hint(self, env):
        service, store = self._service(env)
        store.save_probe("series", 1, "5001", result={"quality": "2160", "langs": ["EN"], "subtitle_langs": []})
        store.save_probe("series", 2, "7001", result={"quality": "2160", "langs": ["EN"], "subtitle_langs": []})
        store.set_desired("series", BREAKING_BAD, True, 2, "7001", "Breaking Bad")
        assert service.list_titles("series")["items"][0]["better_copy"] is None
        store.set_desired("series", BREAKING_BAD, True, 1, "5001")
        assert service.list_titles("series")["items"][0]["better_copy"] is None

    def test_preferences_rerank_the_listing(self, env):
        service, store = self._service(env)
        store.save_probe("movie", 1, "101", result={"quality": "1080", "langs": ["EN"], "subtitle_langs": []})
        store.save_probe("movie", 2, "201", result={"quality": "2160", "langs": ["DE"], "subtitle_langs": []})
        matrix = lambda: service.list_titles("movie", q="matrix")["items"][0]["copies"]
        assert [c["stream_id"] for c in matrix()] == ["201", "101"]
        service.set_prefs({"audio": ["EN"]})
        assert [c["stream_id"] for c in matrix()] == ["101", "201"]

    def test_prefs_api_lists_languages_seen_and_validates(self, http, env):
        base, _ = http
        env[2].save_probe("movie", 1, "101", result={"quality": "1080", "langs": ["KO", "EN"], "subtitle_langs": ["NL"]})
        cookie = _login(base)
        _, data, _ = _req(base, "GET", "/api/prefs", cookie=cookie)
        assert data["prefs"] == DEFAULT_PREFS
        assert "KO" in data["seen"]["audio"] and "EN" in data["seen"]["audio"] and "NL" in data["seen"]["subtitles"]
        status, data, _ = _req(base, "PUT", "/api/prefs", {"audio": ["en"], "quality_order": ["1080"]}, cookie=cookie)
        assert status == 200 and data["prefs"]["audio"] == ["EN"]
        assert data["prefs"]["quality_order"] == ["1080", "2160", "720", "SD"]  # completed
        status, _, _ = _req(base, "PUT", "/api/prefs", {"audio": "EN"}, cookie=cookie)
        assert status == 400


# ---------- scheduled upkeep ----------

from selection.upkeep import run_upkeep


def _upkeep(env):
    plugin, catalogue, store, settings, _ = env
    return run_upkeep(plugin, catalogue, store, settings, LOG)


def _drop_copy(catalogue, kind, copy_id):
    field = "stream_id" if kind == "movie" else "external_series_id"
    catalogue.relations[kind] = [r for r in catalogue.relations[kind] if getattr(r, field) != str(copy_id)]


class TestUpkeep:
    def test_refreshes_urls_of_applied_titles(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        _apply(env)
        settings["dispatcharr_url"] = "http://10.0.0.9:9191"
        r = _upkeep(env)
        assert r["status"] == "ok" and r["refreshed"] == 1
        strm = next(_matrix_folder(tmp_path).glob("*.strm"))
        assert strm.read_text().startswith("http://10.0.0.9:9191/")

    def test_new_episodes_written_unless_season_excluded(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad", excluded_seasons=[2])
        _apply(env)
        _add_bb_episode(catalogue, 1, 3, "Bag in the River", 9010)
        _add_bb_episode(catalogue, 2, 2, "Grilled", 9011)
        r = _upkeep(env)
        assert r["new_files"] == 2  # S01E03 .strm + .nfo
        assert (_bb_season1(tmp_path) / "Breaking Bad - S01E03 - Bag in the River.strm").is_file()
        assert not (_bb_show(tmp_path) / "Season 02").exists()

    def test_series_upkeep_never_deletes_episodes_the_provider_stopped_listing(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        rel = catalogue.relations["series"][0]
        catalogue.episodes[rel.id] = catalogue.episodes[rel.id][:1]
        _upkeep(env)
        assert (_bb_show(tmp_path) / "Season 02" / "Breaking Bad - S02E01 - Seven Thirty-Seven.strm").is_file()

    def test_a_renamed_episode_replaces_its_old_file(self, env):
        # The provider renames an episode (or an adopted library was named from
        # another copy's titles): the same episode must not end up twice.
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        rel = catalogue.relations["series"][0]
        pilot = catalogue.episodes[rel.id][0]
        pilot.episode.name = "Pilot (Extended)"
        info = (pilot.custom_properties or {}).get("info")
        if isinstance(info, dict):
            info["title"] = "Pilot (Extended)"
        catalogue.episodes[rel.id] = catalogue.episodes[rel.id][:1]  # and lists fewer
        _upkeep(env)
        season1 = _bb_season1(tmp_path)
        assert (season1 / "Breaking Bad - S01E01 - Pilot (Extended).strm").is_file()
        assert not (season1 / "Breaking Bad - S01E01 - Pilot.strm").exists()
        assert not (season1 / "Breaking Bad - S01E01 - Pilot.nfo").exists()
        # Episodes the provider stopped listing are still kept.
        assert (season1 / "Breaking Bad - S01E02 - Cat's in the Bag.strm").is_file()
        assert (_bb_show(tmp_path) / "Season 02" / "Breaking Bad - S02E01 - Seven Thirty-Seven.strm").is_file()

    def test_a_renamed_show_folder_replaces_the_old_one(self, env):
        # Seen on a real instance: a series applied before a naming change got the
        # new folder from upkeep while the old one stayed (shown twice).
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        rel = catalogue.relations["series"][0]
        catalogue.episodes[rel.id] = catalogue.episodes[rel.id][:1]  # provider lists fewer, too
        rel.series.name = "EN - Breaking Bad Remastered (2008)"
        _upkeep(env)
        assert not _bb_show(tmp_path).exists()
        new = tmp_path / "Series" / "Breaking Bad Remastered (2008)"
        assert (new / "Season 01" / "Breaking Bad Remastered - S01E01 - Pilot.strm").is_file()
        assert all("Remastered" in p for p in store.files_for("series", BREAKING_BAD))

    def test_vanished_copy_falls_back_to_the_best_remaining_one(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        _drop_copy(catalogue, "series", "5001")
        r = _upkeep(env)
        assert r["fallback"] == 1
        assert not (_bb_season1(tmp_path) / "Breaking Bad - S01E01 - Pilot.strm").exists()
        assert "9101" in (_bb_season1(tmp_path) / "Breaking Bad - S01E01 - Der Einstieg.strm").read_text()
        row = store.get_many("series", [BREAKING_BAD])[BREAKING_BAD]
        assert (row["applied_stream_id"], row["desired_stream_id"], row["flag"]) == ("7001", "7001", "fallback")
        assert "5001" in row["flag_detail"] and store.pending() == []

    def test_fallback_keeps_an_unapplied_change_of_the_user(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        _apply(env)
        store.set_desired("movie", MATRIX, False)  # pending removal, not applied yet
        _drop_copy(catalogue, "movie", "101")
        _upkeep(env)
        row = store.get_many("movie", [MATRIX])[MATRIX]
        assert (row["applied_stream_id"], row["desired_selected"]) == ("201", 0)
        assert [p["content_uuid"] for p in store.pending()] == [MATRIX]

    def test_no_copy_left_deletes_files_keeps_selection_and_restores_later(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
        _apply(env)
        saved = list(catalogue.relations["movie"])
        _drop_copy(catalogue, "movie", "102")
        r = _upkeep(env)
        assert r["no_copy"] == 1
        assert not (tmp_path / "Movies" / "Aladdin (1992)").exists()
        row = store.get_many("movie", [ALADDIN])[ALADDIN]
        assert (row["flag"], row["desired_selected"], row["applied_selected"]) == ("no_copy", 1, 1)
        assert store.pending() == []
        assert _upkeep(env)["no_copy"] == 0  # not counted again
        catalogue.relations["movie"] = saved
        r = _upkeep(env)
        assert r["restored"] == 1
        assert (tmp_path / "Movies" / "Aladdin (1992)").is_dir()
        assert store.get_many("movie", [ALADDIN])[ALADDIN]["flag"] == "fallback"

    def test_a_failed_episode_fetch_changes_nothing(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        catalogue.episodes[catalogue.relations["series"][0].id] = []
        r = _upkeep(env)
        assert r["errors"] == 1 and r["status"] == "partial"
        assert (_bb_season1(tmp_path) / "Breaking Bad - S01E01 - Pilot.strm").is_file()

    def test_upkeep_and_apply_never_write_at_the_same_time(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        assert store.acquire_disk_lease("upkeep-other")
        r = _apply(env)
        assert r["status"] == "error" and "upkeep" in r["message"].lower()
        store.release_disk_lease("upkeep-other")
        _apply(env)
        store.acquire_disk_lease("apply-other")
        assert _upkeep(env)["status"] == "skipped"
        store.release_disk_lease("apply-other")
        assert store.last_upkeep()["status"] == "skipped"

    def test_a_stale_lease_expires(self, env):
        store = env[2]
        assert store.acquire_disk_lease("crashed")
        import time as _t
        assert not store.acquire_disk_lease("next")
        assert store.acquire_disk_lease("next", now=_t.time() + 7 * 3600)


class TestScheduledHook:
    def test_scheduled_rescan_runs_upkeep_in_selection_mode(self, monkeypatch):
        import plugin as plugin_module
        calls = []
        monkeypatch.setattr(plugin_module._selection_runtime, "run_scheduled_upkeep",
                            lambda p, s, l: calls.append(s) or {"status": "ok", "message": "Upkeep: done"})
        settings = {"selection_mode": True}
        r = Plugin().run("rescan_all", {"scheduled": True}, {"logger": LOG, "settings": settings})
        assert r["message"] == "Upkeep: done" and calls == [settings]
        r = Plugin().run("rescan_all", {}, {"logger": LOG, "settings": settings})  # a click
        assert "paused" in r["message"] and len(calls) == 1


class TestUpkeepHttp:
    def test_refresh_now_flags_and_flagged_filter(self, http, env):
        base, service = http
        plugin, catalogue, store, settings, _ = env
        store.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
        _apply(env)
        _drop_copy(catalogue, "movie", "102")
        cookie = _login(base)
        status, data, _ = _req(base, "POST", "/api/upkeep", cookie=cookie)
        assert status == 200 and data["ok"]
        import time as _t
        for _ in range(100):
            if not service.upkeep_job()["running"]:
                break
            _t.sleep(0.05)
        assert service.upkeep_job()["result"]["no_copy"] == 1
        _, pending, _ = _req(base, "GET", "/api/pending", cookie=cookie)
        assert "lost every copy" in pending["last_upkeep"]["message"]
        # The page can still list it once any copy is visible again.
        catalogue.relations["movie"].append(SimpleNamespace(
            id=99, movie=next(t for t in catalogue.titles["movie"] if str(t.uuid) == ALADDIN),
            m3u_account_id=2, m3u_account=catalogue.relations["movie"][0].m3u_account, stream_id="999",
            category=None, container_extension="mkv", custom_properties={}))
        _, data, _ = _req(base, "GET", "/api/movies?state=flagged", cookie=cookie)
        assert [i["uuid"] for i in data["items"]] == [ALADDIN] and data["items"][0]["flag"] == "no_copy"
        _req(base, "POST", f"/api/clear-flag/movie/{ALADDIN}", cookie=cookie)
        _, data, _ = _req(base, "GET", "/api/movies?state=flagged", cookie=cookie)
        assert data["items"] == []


# ---------- adoption of an existing library ----------

from selection import adopt as adoption


class TestParseStrm:
    @pytest.mark.parametrize("text,expected", [
        ("http://10.0.0.5:9191/proxy/vod/movie/0A1B2C3D-0000-4000-8000-000000000001?stream_id=101\n",
         ("movie", "0a1b2c3d-0000-4000-8000-000000000001", "101")),
        ("http://h/proxy/vod/episode/00000000-0000-4000-a000-000100010001",
         ("episode", "00000000-0000-4000-a000-000100010001", None)),
        ("http://h/proxy/vod/movie/00000000-0000-4000-8000-000000000001?m3u_account_id=2&stream_id=201",
         ("movie", "00000000-0000-4000-8000-000000000001", "201")),
        ("http://example.com/video.mp4", None),
        ("", None),
    ])
    def test_parse(self, text, expected):
        assert adoption.parse_strm(text) == expected


def _classic_library(env):
    """Files as another run left them: applied through a separate store, plus
    a duplicate folder, content Dispatcharr no longer has, a non-Dispatcharr
    .strm and a file of the user's own."""
    plugin, catalogue, store, settings, tmp_path = env
    other = SelectionStore(str(tmp_path / "other" / "selection.db"))
    other.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
    other.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
    other.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
    assert apply_pending(plugin, catalogue, other, settings, LOG)["added"] == 3
    movies = tmp_path / "Movies"
    dup = movies / "EN - The Matrix (1999)"
    dup.mkdir()
    (dup / "EN - The Matrix (1999).strm").write_text(
        f"http://10.0.0.5:9191/proxy/vod/movie/{MATRIX}?stream_id=201")
    gone = movies / "Gone (2001)"
    gone.mkdir()
    (gone / "Gone (2001).strm").write_text(
        "http://10.0.0.5:9191/proxy/vod/movie/00000000-0000-4000-8000-000000000999?stream_id=1")
    home = movies / "Home Video"
    home.mkdir()
    (home / "Home Video.strm").write_text("http://example.com/home.mp4")
    (_matrix_folder(tmp_path) / "poster.jpg").write_text("user art")
    return tmp_path


class TestAdoption:
    def test_scan_reads_the_files_and_changes_nothing(self, env):
        plugin, catalogue, store, settings, _ = env
        _classic_library(env)
        plan = adoption.scan(catalogue, store, settings)
        assert plan["counts"] == {"strm_files": 8, "movies": 2, "series": 1, "duplicates": 1,
                                  "already_known": 0, "unknown_content": 1, "not_dispatcharr": 1}
        assert store.pending() == [] and store.adopted_at() is None

    def test_adopt_records_titles_and_duplicates_become_pending(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        _classic_library(env)
        result = adoption.adopt(catalogue, store, adoption.scan(catalogue, store, settings), LOG)
        assert (result["adopted"], result["duplicates"]) == (3, 1)
        assert store.adopted_at() is not None
        rows = {**store.get_many("movie", [MATRIX, ALADDIN]), **store.get_many("series", [BREAKING_BAD])}
        assert all(r["desired_selected"] == r["applied_selected"] == 1 for r in rows.values())
        # The Matrix is on disk as two copies: the better one (4K) is kept.
        assert (rows[MATRIX]["applied_stream_id"], rows[MATRIX]["flag"]) == ("201", "duplicates")
        assert (rows[ALADDIN]["applied_stream_id"], rows[ALADDIN]["flag"]) == ("102", None)
        assert rows[BREAKING_BAD]["applied_stream_id"] == "5001"
        assert [p["content_uuid"] for p in store.pending()] == [MATRIX]
        files = store.files_for("series", BREAKING_BAD)
        assert any(f.endswith("tvshow.nfo") for f in files) and sum(f.endswith(".strm") for f in files) == 3
        assert not any(f.endswith("poster.jpg") for f in store.files_for("movie", MATRIX))

    def test_apply_after_adoption_removes_only_the_extra_files(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        _classic_library(env)
        adoption.adopt(catalogue, store, adoption.scan(catalogue, store, settings), LOG)
        r = _apply(env)
        assert r["changed"] == 1 and r["errors"] == 0
        assert not (tmp_path / "Movies" / "EN - The Matrix (1999)").exists()
        strm = next(_matrix_folder(tmp_path).glob("*.strm"))
        assert "stream_id=201" in strm.read_text()
        assert (_matrix_folder(tmp_path) / "poster.jpg").is_file()  # never ours
        assert (tmp_path / "Movies" / "Gone (2001)").is_dir() and (tmp_path / "Movies" / "Home Video").is_dir()
        assert store.pending() == []
        assert store.get_many("movie", [MATRIX])[MATRIX]["flag"] is None

    def test_known_titles_are_left_alone(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        _classic_library(env)
        store.set_desired("movie", ALADDIN, False)  # the user already decided about it
        plan = adoption.scan(catalogue, store, settings)
        assert plan["counts"]["already_known"] == 1
        adoption.adopt(catalogue, store, plan, LOG)
        assert store.get_many("movie", [ALADDIN])[ALADDIN]["desired_selected"] == 0
        assert store.files_for("movie", ALADDIN) == []

    def test_urls_without_stream_id_take_the_best_copy(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        folder = tmp_path / "Movies" / "The Matrix (1999)"
        folder.mkdir(parents=True)
        (folder / "The Matrix (1999).strm").write_text(f"http://10.0.0.5:9191/proxy/vod/movie/{MATRIX}")
        adoption.adopt(catalogue, store, adoption.scan(catalogue, store, settings), LOG)
        row = store.get_many("movie", [MATRIX])[MATRIX]
        assert (row["applied_stream_id"], row["flag"]) == ("201", None)

    def test_upkeep_leaves_duplicates_for_apply(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        _classic_library(env)
        adoption.adopt(catalogue, store, adoption.scan(catalogue, store, settings), LOG)
        _upkeep(env)
        assert (tmp_path / "Movies" / "EN - The Matrix (1999)").is_dir()


class TestAdoptionHttp:
    def test_scan_adopt_and_skip(self, http, env):
        base, service = http
        _classic_library(env)
        cookie = _login(base)
        _, status, _ = _req(base, "GET", "/api/adopt", cookie=cookie)
        assert status["adopted_at"] is None
        s, data, _ = _req(base, "POST", "/api/adopt/apply", cookie=cookie)
        assert s == 400  # scan first
        _req(base, "POST", "/api/adopt/scan", cookie=cookie)
        import time as _t
        for _ in range(100):
            _, status, _ = _req(base, "GET", "/api/adopt", cookie=cookie)
            if not status["job"].get("running"):
                break
            _t.sleep(0.05)
        assert status["job"]["summary"]["counts"]["movies"] == 2
        assert "plan" not in status["job"]
        s, data, _ = _req(base, "POST", "/api/adopt/apply", cookie=cookie)
        assert s == 200 and data["adopted"] == 3
        _, pending, _ = _req(base, "GET", "/api/pending", cookie=cookie)
        assert [(i["action"], i["uuid"]) for i in pending["items"]] == [("tidy", MATRIX)]
        _, status, _ = _req(base, "GET", "/api/adopt", cookie=cookie)
        assert status["adopted_at"] is not None

    def test_skip(self, http, env):
        base, _ = http
        cookie = _login(base)
        _req(base, "POST", "/api/adopt/skip", cookie=cookie)
        _, status, _ = _req(base, "GET", "/api/adopt", cookie=cookie)
        assert status["adopted_at"] is not None and env[2].pending() == []


# ---------- per-title language override ----------

from selection.copyinfo import MATCH, MISMATCH, UNKNOWN, language_match, title_prefs

GLOBAL = dict(DEFAULT_PREFS, audio=["EN"], subtitles=["EN"])


class TestTitlePrefs:
    def test_no_override_keeps_the_global_preferences(self):
        assert title_prefs(GLOBAL, None) == GLOBAL

    def test_override_asks_for_that_audio_and_requires_the_global_subtitles(self):
        p = title_prefs(GLOBAL, "fr")
        assert p["audio"] == ["FR"] and p["subtitles"] == ["EN"] and p["subtitles_required"]
        assert GLOBAL["audio"] == ["EN"] and "subtitles_required" not in GLOBAL  # not mutated

    def test_match_needs_original_audio_and_subtitles(self):
        p = title_prefs(GLOBAL, "FR")
        assert language_match(_c("fr_en", ["FR"], ["EN"])["info"], p) == MATCH
        # English audio doesn't count against a copy that has the original.
        assert language_match(_c("en_fr", ["EN", "FR"], ["EN", "DE"])["info"], p) == MATCH
        # No subtitle tracks at all: they may be burned in (seen with real French films), so unknown.
        assert language_match(_c("fr_nosubs", ["FR"], [])["info"], p) == UNKNOWN
        assert language_match(_c("fr_desubs", ["FR"], ["DE"])["info"], p) == MISMATCH
        assert language_match(_c("en_only", ["EN"], ["EN"])["info"], p) == MISMATCH
        # Tagged FR but not probed: subtitles unknown.
        assert language_match(_c("fr_tag", ["FR"], deep=False)["info"], p) == UNKNOWN
        assert language_match(_c("en_tag", ["EN"], deep=False)["info"], p) == MISMATCH

    def test_without_global_subtitles_only_audio_is_required(self):
        p = title_prefs(dict(DEFAULT_PREFS, audio=["EN"]), "DE")
        assert language_match(_c("de_nosubs", ["DE"], [])["info"], p) == MATCH

    def test_original_with_subtitles_beats_english_4k(self):
        copies = [_c("en4k", ["EN"], ["EN"], quality="2160"), _c("fr", ["FR", "EN"], ["EN"])]
        assert [c["stream_id"] for c in rank(copies, title_prefs(GLOBAL, "FR"))] == ["fr", "en4k"]
        assert [c["stream_id"] for c in rank(copies, GLOBAL)] == ["en4k", "fr"]


class TestOverrideStore:
    def test_roundtrip_and_never_pending(self, env):
        store = env[2]
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        store.mark_applied("movie", MATRIX)
        store.set_audio_override("movie", MATRIX, "FR")
        assert store.get_many("movie", [MATRIX])[MATRIX]["audio_override"] == "FR"
        assert store.pending() == []
        store.set_desired("movie", MATRIX, True, 2, 201)  # a copy change keeps it
        assert store.get_many("movie", [MATRIX])[MATRIX]["audio_override"] == "FR"
        store.set_audio_override("movie", MATRIX, None)
        assert store.get_many("movie", [MATRIX])[MATRIX]["audio_override"] is None

    def test_override_on_an_unknown_title_creates_an_unselected_row(self, env):
        store = env[2]
        store.set_audio_override("movie", ALADDIN, "DE", title="Aladdin")
        row = store.get_many("movie", [ALADDIN])[ALADDIN]
        assert row["audio_override"] == "DE" and not row["desired_selected"] and store.pending() == []


class TestOverrideService:
    def _service(self, env):
        plugin, catalogue, store, settings, _ = env
        store.set_prefs(GLOBAL)
        # Matrix: 101 is 1080p with FR (default) + EN audio, 201 is 4K English only.
        store.save_probe("movie", 1, "101", result={"quality": "1080", "langs": ["FR", "EN"], "subtitle_langs": ["EN"]})
        store.save_probe("movie", 2, "201", result={"quality": "2160", "langs": ["EN"], "subtitle_langs": ["EN"]})
        return SelectionService(plugin, catalogue, store, lambda: settings, LOG), store

    def _matrix(self, service):
        return service.list_titles("movie", q="matrix")["items"][0]

    def test_override_reranks_marks_and_hints(self, env):
        service, store = self._service(env)
        store.set_desired("movie", MATRIX, True, 2, "201", "The Matrix")
        store.mark_applied("movie", MATRIX)
        item = self._matrix(service)
        assert item["audio_override"] is None and item["better_copy"] is None
        assert [c["stream_id"] for c in item["copies"]] == ["201", "101"]
        service.set_override("movie", MATRIX, {"audio": "fr"})
        item = self._matrix(service)
        assert item["audio_override"] == "FR"
        assert [(c["stream_id"], c["info"]["match"]) for c in item["copies"]] == [("101", "match"), ("201", "mismatch")]
        # The chosen copy stays; the page offers the switch.
        assert item["chosen"]["stream_id"] == "201"
        assert item["better_copy"] == {"account_id": 1, "stream_id": "101"}
        assert store.pending() == []

    def test_default_audio_warning_follows_the_override(self, env):
        service, store = self._service(env)
        service.set_override("movie", MATRIX, {"audio": "EN"})
        by_id = {c["stream_id"]: c for c in self._matrix(service)["copies"]}
        assert by_id["101"]["info"]["default_audio_ok"] is False  # default FR, wants EN

    def test_override_api_validates(self, http, env):
        base, _ = http
        cookie = _login(base)
        status, data, _ = _req(base, "PUT", f"/api/override/movie/{MATRIX}", {"audio": "de"}, cookie=cookie)
        assert status == 200 and data["audio_override"] == "DE"
        status, data, _ = _req(base, "PUT", f"/api/override/movie/{MATRIX}", {"audio": None}, cookie=cookie)
        assert status == 200 and data["audio_override"] is None
        for bad in ({"audio": "french!"}, {"audio": 3}, {}):
            status, _, _ = _req(base, "PUT", f"/api/override/movie/{MATRIX}", bad, cookie=cookie)
            assert status == 400
        status, _, _ = _req(base, "PUT", f"/api/override/nope/{MATRIX}", {"audio": "DE"}, cookie=cookie)
        assert status == 400


class TestOverrideUpkeep:
    def test_fallback_ranks_by_the_title_override(self, tmp_path):
        plugin = Plugin()
        settings = {f["id"]: f["default"] for f in plugin.fields if "default" in f}
        settings.update(dispatcharr_url="http://10.0.0.5:9191", root_folder=str(tmp_path / "Movies"),
                        series_root_folder=str(tmp_path / "Series"), selection_mode=True)
        spec = dict(SPEC, movies=[{"name": "Amelie (2001)", "year": 2001,
                                   "copies": [(1, 101, "EN - Films"), (1, 102, "4K Movies"), (2, 201, "FR - Films")]}])
        catalogue = make_fake_catalogue(plugin, spec)
        store = SelectionStore(str(tmp_path / "db" / "selection.db"))
        store.set_prefs(GLOBAL)
        store.save_probe("movie", 1, "102", result={"quality": "2160", "langs": ["EN"], "subtitle_langs": ["EN"]})
        store.save_probe("movie", 2, "201", result={"quality": "1080", "langs": ["FR"], "subtitle_langs": ["EN"]})
        store.set_desired("movie", MATRIX, True, 1, 101, "Amelie")  # first fake movie has MATRIX's uuid
        store.set_audio_override("movie", MATRIX, "FR")
        env = (plugin, catalogue, store, settings, tmp_path)
        _apply(env)
        _drop_copy(catalogue, "movie", 101)
        assert _upkeep(env)["fallback"] == 1
        row = store.get_many("movie", [MATRIX])[MATRIX]
        assert (row["applied_account_id"], row["applied_stream_id"]) == (2, "201")


# ---------- page sessions survive restarts ----------


class TestPersistentSessions:
    def _service(self, env, settings=None):
        plugin, catalogue, store, base_settings, _ = env
        s = settings if settings is not None else base_settings
        return SelectionService(plugin, catalogue, store, lambda: s, LOG)

    def test_session_survives_a_restart(self, env):
        token = self._service(env).login("pw")
        assert token and self._service(env).is_authenticated(token)  # a new service = a restart

    def test_database_holds_no_usable_token(self, env):
        token = self._service(env).login("pw")
        with open(env[2].db_path, "rb") as f:
            assert token.encode() not in f.read()

    def test_logout_and_expiry(self, env, monkeypatch):
        service = self._service(env)
        token = service.login("pw")
        service.logout(token)
        assert not self._service(env).is_authenticated(token)
        token = service.login("pw")
        monkeypatch.setattr(page_server.time, "time", lambda: 10 ** 11)
        assert not service.is_authenticated(token)

    def test_changing_the_password_logs_everyone_out(self, env):
        settings = dict(env[3])
        service = self._service(env, settings)
        token = service.login("pw")
        settings["selection_password"] = "new"
        assert not service.is_authenticated(token)
        settings["selection_password"] = "pw"  # changed back: the old session is gone for good
        assert not service.is_authenticated(token)

    def test_no_password_means_no_session(self, env):
        settings = dict(env[3])
        service = self._service(env, settings)
        token = service.login("pw")
        settings["selection_password"] = ""
        assert not service.is_authenticated(token)


# ---------- new since last visit ----------


class TestNewSinceLastVisit:
    def _service(self, env):
        plugin, catalogue, store, settings, _ = env
        return SelectionService(plugin, catalogue, store, lambda: settings, LOG), catalogue, store

    @staticmethod
    def _added(catalogue, kind, uuid, when):
        next(t for t in catalogue.titles[kind] if t.uuid == uuid).created_at = when

    def test_first_visit_starts_the_clock_so_nothing_is_new(self, env):
        service, _, store = self._service(env)
        result = service.list_titles("movie")
        assert result["new_total"] == 0 and not any(i["new"] for i in result["items"])
        assert store.seen_at("movie") is not None

    def test_titles_added_after_the_mark_are_new(self, env):
        service, catalogue, store = self._service(env)
        store.set_seen("movie", 1000)
        self._added(catalogue, "movie", MATRIX, 1500)
        items = {i["uuid"]: i for i in service.list_titles("movie")["items"]}
        assert items[MATRIX]["new"] and not items[ALADDIN]["new"]
        new = service.list_titles("movie", state="new")
        assert [i["uuid"] for i in new["items"]] == [MATRIX] and new["new_total"] == 1

    def test_mark_seen_clears_new_for_that_kind_only(self, env):
        service, catalogue, store = self._service(env)
        store.set_seen("movie", 1000)
        store.set_seen("series", 1000)
        self._added(catalogue, "movie", MATRIX, 1500)
        self._added(catalogue, "series", BREAKING_BAD, 1500)
        service.mark_seen("movie")
        assert service.list_titles("movie")["new_total"] == 0
        assert service.list_titles("series")["new_total"] == 1

    def test_seen_api(self, http, env):
        base, service = http
        env[2].set_seen("movie", 1000)
        self._added(env[1], "movie", MATRIX, 1500)
        cookie = _login(base)
        _, data, _ = _req(base, "GET", "/api/movies?state=new", cookie=cookie)
        assert data["new_total"] == 1 and data["items"][0]["new"]
        status, _, _ = _req(base, "POST", "/api/seen/movie", cookie=cookie)
        assert status == 200
        _, data, _ = _req(base, "GET", "/api/movies?state=new", cookie=cookie)
        assert data["new_total"] == 0 and data["items"] == []
        status, _, _ = _req(base, "POST", "/api/seen/nope", cookie=cookie)
        assert status == 400


# ---------- relink after Dispatcharr re-creates content; mass-loss guard ----------
# Seen on a real instance: the provider once returned 0 movies, Dispatcharr deleted
# all 250 and re-imported them on the next refresh with new uuids.

from selection import upkeep as upkeep_mod

NEW_MATRIX = "11111111-0000-4000-8000-000000000001"


def _recreate(catalogue, kind, uuid, new_uuid):
    """What Dispatcharr does: same content and copies, a new uuid."""
    next(t for t in catalogue.titles[kind] if t.uuid == uuid).uuid = new_uuid


class TestRelink:
    def test_upkeep_relinks_by_copy_and_rewrites_the_url(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        _apply(env)
        paths = sorted(store.files_for("movie", MATRIX))
        _recreate(catalogue, "movie", MATRIX, NEW_MATRIX)
        r = _upkeep(env)
        assert r["relinked"] == 1 and r["no_copy"] == 0
        assert store.get_many("movie", [MATRIX]) == {}
        row = store.get_many("movie", [NEW_MATRIX])[NEW_MATRIX]
        assert row["applied_selected"] and row["applied_stream_id"] == "101" and row["flag"] is None
        assert sorted(store.files_for("movie", NEW_MATRIX)) == paths  # same paths, no media-server churn
        strm = next(p for p in paths if p.endswith(".strm"))
        assert NEW_MATRIX in open(strm).read()
        assert store.pending() == []

    def test_series_relinks_by_external_series_id(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("series", BREAKING_BAD, True, 1, "5001", "Breaking Bad")
        _apply(env)
        new = "11111111-0000-4000-9000-000000000001"
        _recreate(catalogue, "series", BREAKING_BAD, new)
        assert _upkeep(env)["relinked"] == 1
        assert store.get_many("series", [new])[new]["applied_stream_id"] == "5001"
        # .strm files hold episode uuids (re-fetched by upkeep); the files stay recorded.
        assert (_bb_season1(tmp_path) / "Breaking Bad - S01E01 - Pilot.strm").is_file()
        assert store.files_for("series", new) and store.files_for("series", BREAKING_BAD) == []

    def test_page_listing_relinks_too(self, env):
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        _recreate(catalogue, "movie", MATRIX, NEW_MATRIX)
        items = {i["uuid"]: i for i in service.list_titles("movie")["items"]}
        assert items[NEW_MATRIX]["selected"]

    def test_page_listing_leaves_the_disk_lease_alone_when_nothing_vanished(self, env):
        # Every tab or filter click lists titles; holding the lease then would
        # make an Apply started at that moment fail.
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        taken = []
        real = store.acquire_disk_lease
        store.acquire_disk_lease = lambda holder, *a, **k: taken.append(holder) or real(holder, *a, **k)
        service.list_titles("movie")
        assert taken == []

    def test_page_listing_checks_for_vanished_titles_at_most_once_a_minute(self, env, monkeypatch):
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        checks = []
        real = catalogue.title_ids
        catalogue.title_ids = lambda *a, **k: checks.append(1) or real(*a, **k)
        service.list_titles("movie")
        service.list_titles("movie", state="selected")
        assert len(checks) == 1

    def test_tmdb_fallback_when_the_copy_is_gone_too(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        matrix = next(t for t in catalogue.titles["movie"] if t.uuid == MATRIX)
        matrix.tmdb_id = "603"
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        _apply(env)
        _upkeep(env)  # records the tmdb id while the title exists
        _recreate(catalogue, "movie", MATRIX, NEW_MATRIX)
        _drop_copy(catalogue, "movie", 101)  # the provider renumbered too
        r = _upkeep(env)
        assert r["relinked"] == 1 and r["fallback"] == 1
        row = store.get_many("movie", [NEW_MATRIX])[NEW_MATRIX]
        assert (row["applied_stream_id"], row["flag"]) == ("201", "fallback")

    def test_ambiguous_tmdb_is_not_relinked(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        for t in catalogue.titles["movie"]:
            t.tmdb_id = "603"  # every title claims the same TMDB id
        store.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
        _apply(env)
        _upkeep(env)
        _recreate(catalogue, "movie", ALADDIN, "11111111-0000-4000-8000-000000000002")
        _drop_copy(catalogue, "movie", 102)
        assert _upkeep(env)["relinked"] == 0
        assert store.get_many("movie", [ALADDIN])[ALADDIN]["flag"] == "no_copy"

    def test_merges_into_an_existing_unselected_row(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        _apply(env)
        _recreate(catalogue, "movie", MATRIX, NEW_MATRIX)
        store.set_audio_override("movie", NEW_MATRIX, "DE")  # a row for the new uuid already exists
        assert _upkeep(env)["relinked"] == 1
        row = store.get_many("movie", [NEW_MATRIX])[NEW_MATRIX]
        assert row["desired_selected"] and row["applied_selected"] and row["audio_override"] == "DE"
        assert store.get_many("movie", [MATRIX]) == {} and store.files_for("movie", NEW_MATRIX)


class TestMassLossGuard:
    def test_many_titles_losing_every_copy_deletes_nothing(self, env, monkeypatch):
        plugin, catalogue, store, settings, tmp_path = env
        monkeypatch.setattr(upkeep_mod, "GUARD_MIN_TITLES", 2)
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        store.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
        _apply(env)
        files = store.files_for("movie", MATRIX) + store.files_for("movie", ALADDIN)
        catalogue.relations["movie"] = []  # the provider returned no movies
        r = _upkeep(env)
        assert r["held"] == 2 and r["no_copy"] == 0 and "nothing deleted" in r["message"]
        assert all(os.path.exists(p) for p in files)
        assert all(row["flag"] is None for row in store.get_many("movie", [MATRIX, ALADDIN]).values())

    def test_a_few_lost_titles_still_follow_the_no_copy_rule(self, env):
        plugin, catalogue, store, settings, tmp_path = env
        store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
        store.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
        _apply(env)
        _drop_copy(catalogue, "movie", 102)
        r = _upkeep(env)  # 1 of 2 lost: below the 5-title minimum
        assert r["no_copy"] == 1 and r["held"] == 0


# ---------- DB connections of page threads go back to Dispatcharr's pool ----------
# Seen on a real instance: Dispatcharr pools 8 connections per process
# (django_db_geventpool); a page thread that ends holding one leaks it, and
# once the pool is empty every query fails with gevent's LoopExit.


class TestSettingsConnection:
    def test_settings_lookup_returns_its_connection(self, monkeypatch):
        import types
        events = []
        django_db = types.ModuleType("django.db")
        django_db.close_old_connections = lambda: events.append("close")

        class _Query:
            def first(self):
                events.append("query")
                return types.SimpleNamespace(settings={"selection_password": "pw"})

        models = types.ModuleType("apps.plugins.models")
        models.PluginConfig = types.SimpleNamespace(objects=types.SimpleNamespace(filter=lambda **kw: _Query()))
        for name, module in (("django", types.ModuleType("django")), ("django.db", django_db),
                             ("apps", types.ModuleType("apps")), ("apps.plugins", types.ModuleType("apps.plugins")),
                             ("apps.plugins.models", models)):
            monkeypatch.setitem(sys.modules, name, module)
        settings, _ = runtime._current_settings(Plugin())
        assert settings["selection_password"] == "pw"
        assert events[-1] == "close"  # closed after the query, not only before


# ---------- provider (M3U account) filter ----------

class TestProviderFilter:
    def _service(self, env):
        plugin, catalogue, store, settings, _ = env
        return SelectionService(plugin, catalogue, store, lambda: settings, LOG)

    def test_filter_lists_titles_with_a_copy_there_and_keeps_every_copy(self, env):
        result = self._service(env).list_titles("movie", account=2)
        assert [i["uuid"] for i in result["items"]] == [MATRIX]  # Adult is category-excluded
        assert {c["account_id"] for c in result["items"][0]["copies"]} == {1, 2}

    def test_listing_reports_accounts_with_title_counts(self, env):
        service = self._service(env)
        assert service.list_titles("movie")["accounts"] == [
            {"id": 1, "name": "Provider A", "titles": 2},
            {"id": 2, "name": "Provider B", "titles": 1},
        ]
        assert service.list_titles("series")["accounts"] == [
            {"id": 1, "name": "Provider A", "titles": 1},
            {"id": 2, "name": "Provider B", "titles": 2},
        ]

    def test_inactive_account_is_not_offered(self, env):
        next(r for r in env[1].relations["movie"] if r.m3u_account_id == 2).m3u_account.is_active = False
        assert [a["id"] for a in self._service(env).list_titles("movie")["accounts"]] == [1]

    def test_filter_applies_under_the_selected_tab(self, env):
        service = self._service(env)
        service.set_selection("movie", MATRIX, {"selected": True, "account_id": 1, "stream_id": "101"})
        service.set_selection("movie", ALADDIN, {"selected": True, "account_id": 1, "stream_id": "102"})
        # Has a copy on account 2, even though the chosen copy is on account 1.
        result = service.list_titles("movie", state="selected", account=2)
        assert [i["uuid"] for i in result["items"]] == [MATRIX]

    def test_account_query_param(self, http, env):
        base, _ = http
        cookie = _login(base)
        _, data, _ = _req(base, "GET", "/api/series?account=1", cookie=cookie)
        assert [i["uuid"] for i in data["items"]] == [BREAKING_BAD]
        status, _, _ = _req(base, "GET", "/api/series?account=abc", cookie=cookie)
        assert status == 400


# ---------- category and year (decade) filters ----------

FILTER_SPEC = {
    "accounts": {1: "Provider A", 2: "Provider B"},
    "movies": [
        {"name": "The Matrix", "year": 1999, "copies": [(1, 101, "Sci-Fi"), (2, 201, "4K Movies")]},
        {"name": "Aladdin", "year": 1992, "copies": [(1, 102, "Family")]},
        {"name": "Casablanca", "year": 1942, "copies": [(1, 103, "Classics")]},
        {"name": "Dune", "year": 2021, "copies": [(2, 203, "Sci-Fi")]},
        {"name": "Mystery Reel", "year": None, "copies": [(1, 104, "Classics")]},
        {"name": "Junk Year", "year": 1, "copies": [(1, 105, "")]},
    ],
    "series": [],
}


class TestCategoryYearFilters:
    @pytest.fixture
    def service(self, env):
        plugin, _, store, settings, _ = env
        catalogue = make_fake_catalogue(plugin, FILTER_SPEC)
        return SelectionService(plugin, catalogue, store, lambda: settings, LOG)

    @staticmethod
    def _names(result):
        return [i["name"] for i in result["items"]]

    def test_category_filter_matches_any_copy_and_keeps_every_copy(self, service):
        result = service.list_titles("movie", category="Sci-Fi")
        assert self._names(result) == ["Dune", "The Matrix"]
        matrix = next(i for i in result["items"] if i["name"] == "The Matrix")
        assert {c["category"] for c in matrix["copies"]} == {"Sci-Fi", "4K Movies"}

    def test_category_and_provider_need_the_same_copy(self, service):
        # The Matrix is Sci-Fi on account 1 only; its account 2 copy is "4K Movies".
        assert self._names(service.list_titles("movie", category="Sci-Fi", account=2)) == ["Dune"]

    def test_decade_filter(self, service):
        assert self._names(service.list_titles("movie", year="1990s")) == ["Aladdin", "The Matrix"]
        assert self._names(service.list_titles("movie", year="before-1950")) == ["Casablanca"]
        # No year, and junk years before 1900, count as "no year".
        assert self._names(service.list_titles("movie", year="none")) == ["Junk Year", "Mystery Reel"]

    def test_listing_reports_categories_by_name_with_counts(self, service):
        assert service.list_titles("movie")["categories"] == [
            {"name": "4K Movies", "titles": 1},
            {"name": "Classics", "titles": 2},
            {"name": "Family", "titles": 1},
            {"name": "Sci-Fi", "titles": 2},
        ]

    def test_listing_reports_decades_newest_first(self, service):
        assert service.list_titles("movie")["decades"] == [
            {"key": "2020s", "titles": 1},
            {"key": "1990s", "titles": 2},
            {"key": "before-1950", "titles": 1},
            {"key": "none", "titles": 2},
        ]

    def test_facets_follow_the_provider_filter(self, service):
        result = service.list_titles("movie", account=2)
        assert result["categories"] == [{"name": "4K Movies", "titles": 1}, {"name": "Sci-Fi", "titles": 1}]
        assert result["decades"] == [{"key": "2020s", "titles": 1}, {"key": "1990s", "titles": 1}]

    def test_filters_apply_under_the_selected_tab(self, service):
        service.set_selection("movie", MATRIX, {"selected": True, "account_id": 1, "stream_id": "101"})
        assert self._names(service.list_titles("movie", state="selected", year="1990s")) == ["The Matrix"]
        assert self._names(service.list_titles("movie", state="selected", category="Family")) == []

    def test_bad_year_is_rejected(self, service):
        with pytest.raises(ValueError):
            service.list_titles("movie", year="1990")

    def test_query_params(self, http, env):
        base, _ = http
        cookie = _login(base)
        _, data, _ = _req(base, "GET", "/api/movies?category=EN%20-%20Family", cookie=cookie)
        assert [i["uuid"] for i in data["items"]] == [ALADDIN]
        _, data, _ = _req(base, "GET", "/api/movies?year=1990s", cookie=cookie)
        assert {i["uuid"] for i in data["items"]} == {MATRIX, ALADDIN}
        status, _, _ = _req(base, "GET", "/api/movies?year=soon", cookie=cookie)
        assert status == 400


# ---------- listing aggregates are cached briefly ----------

class TestListingAggregateCache:
    # Provider / category / decade counts and the New total don't depend on
    # the page being viewed; on a 74k-movie catalogue they were over half of
    # every listing's time.
    def _counting(self, env):
        plugin, catalogue, store, settings, _ = env
        calls = []
        for name in ("accounts", "facets"):
            real = getattr(catalogue, name)
            setattr(catalogue, name, (lambda real, name: lambda *a, **k: calls.append(name) or real(*a, **k))(real, name))
        return SelectionService(plugin, catalogue, store, lambda: settings, LOG), calls

    def test_repeated_listings_reuse_the_aggregates(self, env):
        service, calls = self._counting(env)
        service.list_titles("movie")
        service.list_titles("movie", state="selected", page=1, q="mat")
        assert calls == ["accounts", "facets"]

    def test_cache_is_per_kind_and_provider(self, env):
        service, calls = self._counting(env)
        service.list_titles("movie")
        service.list_titles("series")
        service.list_titles("movie", account=2)
        assert calls.count("facets") == 3 and calls.count("accounts") == 2

    def test_mark_all_seen_updates_the_new_total_at_once(self, env):
        plugin, catalogue, store, settings, _ = env
        service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
        store.set_seen("movie", -1)  # fake titles have no created_at (0)
        assert service.list_titles("movie")["new_total"] > 0
        service.mark_seen("movie")
        assert service.list_titles("movie")["new_total"] == 0

# ---------- schedule snapshot keeps no secrets ----------

def test_schedule_snapshot_leaves_out_selection_settings():
    # The password would be readable in Django admin, and a stored
    # selection_mode=True would keep pausing the classic cron after the
    # user turns selection mode off (the task reads selection settings live).
    snap = Plugin()._schedule_snapshot({"selection_password": "pw", "selection_mode": True,
                                        "selection_port": 9192, "schedule_cron": "0 3 * * *",
                                        "batch_size": "250"})
    assert snap == {"batch_size": "250"}


def test_cron_runs_classic_rescan_after_selection_mode_is_turned_off(monkeypatch):
    import plugin as plugin_mod
    monkeypatch.setattr(plugin_mod._selection_runtime, "live_settings",
                        lambda plugin: {"selection_mode": False})
    snapshot = Plugin()._schedule_snapshot({"selection_mode": True, "batch_size": "250"})
    settings, params = plugin_mod._scheduled_run_settings(Plugin(), snapshot, LOG)
    assert (settings, params) == ({"batch_size": "250"}, {})
    monkeypatch.setattr(plugin_mod._selection_runtime, "live_settings",
                        lambda plugin: {"selection_mode": True, "batch_size": "10"})
    settings, params = plugin_mod._scheduled_run_settings(Plugin(), snapshot, LOG)
    assert (settings, params) == ({"selection_mode": True, "batch_size": "10"}, {"scheduled": True})


def test_removing_a_title_clears_its_upkeep_flag(env):
    # Seen on a real instance: a restored title (flag 'fallback') was unselected and
    # applied, but stayed under the Flagged filter.
    _, _, store, _, _ = env
    store.set_desired("movie", MATRIX, True, account_id=1, stream_id="101", title="The Matrix")
    store.mark_applied("movie", MATRIX)
    store.set_flag("movie", MATRIX, "fallback", "switched")
    store.set_desired("movie", MATRIX, False)
    assert _apply(env)["removed"] == 1
    assert store.flagged_uuids("movie") == []


def test_applying_another_copy_clears_the_upkeep_flag(env):
    # 'no_copy' / 'fallback' describe the copy upkeep left behind; once the
    # user applies a copy of their own, the badge would be wrong.
    _, _, store, _, _ = env
    store.set_desired("movie", MATRIX, True, account_id=1, stream_id="101", title="The Matrix")
    _apply(env)
    store.set_flag("movie", MATRIX, "no_copy", "lost")
    store.set_desired("movie", MATRIX, True, account_id=2, stream_id="201", title="The Matrix")
    _apply(env)
    assert store.flagged_uuids("movie") == []


def test_applying_a_season_change_keeps_the_upkeep_flag(env):
    _, _, store, _, _ = env
    store.set_desired("series", BREAKING_BAD, True, account_id=1, stream_id="5001", title="Breaking Bad")
    _apply(env)
    store.set_flag("series", BREAKING_BAD, "fallback", "switched")
    store.set_desired("series", BREAKING_BAD, True, account_id=1, stream_id="5001", title="Breaking Bad",
                      excluded_seasons=[2])
    _apply(env)
    assert store.flagged_uuids("series") == [BREAKING_BAD]


def test_a_change_made_during_apply_stays_pending(env):
    # Apply reads the pending rows once. A page edit made while it runs must
    # not be recorded as applied: the files on disk follow the old row.
    plugin, catalogue, store, _, tmp_path = env
    store.set_desired("movie", MATRIX, True, account_id=1, stream_id="101", title="The Matrix")
    real_resolve = catalogue.resolve_copy

    def resolve_then_unselect(*args, **kwargs):
        result = real_resolve(*args, **kwargs)
        store.set_desired("movie", MATRIX, False)  # the user unticks it meanwhile
        return result

    catalogue.resolve_copy = resolve_then_unselect
    assert _apply(env)["added"] == 1
    catalogue.resolve_copy = real_resolve
    row = store.get_many("movie", [MATRIX])[MATRIX]
    assert row["applied_selected"] == 1 and row["desired_selected"] == 0
    assert [r["content_uuid"] for r in store.pending("movie")] == [MATRIX]
    assert _apply(env)["removed"] == 1
    assert not _matrix_folder(tmp_path).exists()


# ---------- review follow-ups (PR #17) ----------

def test_scheduled_run_is_skipped_when_live_settings_cannot_be_read(monkeypatch):
    # The snapshot holds no selection settings, so falling back to it would run
    # a classic full rescan while selection mode may still be on.
    import plugin as plugin_mod

    def broken(plugin):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(plugin_mod._selection_runtime, "live_settings", broken)
    snapshot = Plugin()._schedule_snapshot({"selection_mode": True, "batch_size": "250"})
    assert plugin_mod._scheduled_run_settings(Plugin(), snapshot, LOG) == (None, None)


def test_adopted_nfo_is_never_rewritten_but_goes_with_its_title(env):
    # Classic mode never overwrites an existing .nfo, so an adopted one may
    # hold the user's edits.
    plugin, catalogue, store, settings, tmp_path = env
    _classic_library(env)
    nfo = next(_matrix_folder(tmp_path).glob("*.nfo"))
    nfo.write_text("<movie><title>My edit</title></movie>")
    adoption.adopt(catalogue, store, adoption.scan(catalogue, store, settings), LOG)
    assert _apply(env)["changed"] == 1  # rewrites the Matrix with its 4K copy
    assert nfo.read_text() == "<movie><title>My edit</title></movie>"
    assert str(nfo) in store.files_for("movie", MATRIX)
    store.set_desired("movie", MATRIX, False)
    assert _apply(env)["removed"] == 1
    assert not nfo.exists()  # removed with its title, as classic Clean up would


def test_nfo_written_by_apply_is_still_updated(env):
    plugin, catalogue, store, settings, tmp_path = env
    store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
    _apply(env)
    nfo = next(_matrix_folder(tmp_path).glob("*.nfo"))
    nfo.write_text("stale")
    store.set_desired("movie", MATRIX, True, 2, 201, "The Matrix")
    _apply(env)
    assert nfo.read_text() != "stale"


def test_two_titles_with_the_same_file_name_do_not_share_it(tmp_path):
    plugin = Plugin()
    settings = {f["id"]: f["default"] for f in plugin.fields if "default" in f}
    settings.update(dispatcharr_url="http://10.0.0.5:9191", root_folder=str(tmp_path / "Movies"),
                    series_root_folder=str(tmp_path / "Series"))
    spec = {"accounts": {1: "Provider A"},
            "movies": [{"name": "Twin", "year": 2000, "copies": [(1, 301, "Films")]},
                       {"name": "Twin", "year": 2000, "copies": [(1, 302, "Films")]}],
            "series": []}
    first, second = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
    catalogue = make_fake_catalogue(plugin, spec)
    store = SelectionStore(str(tmp_path / "db" / "selection.db"))
    store.set_desired("movie", first, True, 1, 301, "Twin")
    apply_pending(plugin, catalogue, store, settings, LOG)
    files = store.files_for("movie", first)
    store.set_desired("movie", second, True, 1, 302, "Twin")
    r = apply_pending(plugin, catalogue, store, settings, LOG)
    assert r["errors"] == 1 and "already belongs to Twin" in r["failures"][0]
    assert store.files_for("movie", first) == files and store.files_for("movie", second) == []
    strm = next(p for p in files if p.endswith(".strm"))
    assert "stream_id=301" in open(strm).read()  # not overwritten by the second title
    store.set_desired("movie", second, False)
    apply_pending(plugin, catalogue, store, settings, LOG)
    assert all(os.path.exists(p) for p in files)


def test_guard_share_counts_only_titles_it_watches(env, monkeypatch):
    # Titles already without a copy aren't watched, so they mustn't dilute the
    # share: 2 of 2 watched titles lost is an outage, whatever else is flagged.
    plugin, catalogue, store, settings, tmp_path = env
    monkeypatch.setattr(upkeep_mod, "GUARD_MIN_TITLES", 2)
    store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
    store.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
    _apply(env)
    for n in range(10):
        uuid = f"00000000-0000-4000-8000-0000000009{n:02d}"
        store.set_desired("movie", uuid, True, 1, 900 + n, f"Gone {n}")
        store.mark_applied("movie", uuid)
        store.set_flag("movie", uuid, "no_copy", "lost")
    files = store.files_for("movie", MATRIX) + store.files_for("movie", ALADDIN)
    catalogue.relations["movie"] = []
    r = _upkeep(env)
    assert r["held"] == 2 and all(os.path.exists(p) for p in files)


def test_no_copy_titles_are_listed_and_not_counted_on_disk(http, env):
    base, service = http
    plugin, catalogue, store, settings, _ = env
    store.set_desired("movie", ALADDIN, True, 1, 102, "Aladdin")
    _apply(env)
    _drop_copy(catalogue, "movie", "102")
    _upkeep(env)
    cookie = _login(base)
    for state in ("flagged", "selected"):
        _, data, _ = _req(base, "GET", f"/api/movies?state={state}", cookie=cookie)
        assert [i["uuid"] for i in data["items"]] == [ALADDIN]
        item = data["items"][0]
        assert (item["copies"], item["on_disk"], item["flag"]) == ([], False, "no_copy")
    assert store.counts()["movies_on_disk"] == 0
    # It can still be unselected, which then removes nothing and clears the flag.
    _req(base, "PUT", f"/api/selection/movie/{ALADDIN}", {"selected": False, "title": "Aladdin"}, cookie=cookie)
    assert _apply(env)["removed"] == 1
    _, data, _ = _req(base, "GET", "/api/movies?state=flagged", cookie=cookie)
    assert data["items"] == []


def test_scheduled_run_when_the_selection_package_fails_to_import(monkeypatch):
    # The snapshot can't say whether selection mode is on, so the saved setting
    # decides: off runs classic as before, on (or unreadable) skips the run.
    import plugin as plugin_mod
    monkeypatch.setattr(plugin_mod, "_selection_runtime", None)
    snapshot = {"batch_size": "250"}
    monkeypatch.setattr(plugin_mod, "_saved_selection_mode", lambda p: False)
    assert plugin_mod._scheduled_run_settings(Plugin(), snapshot, LOG) == (snapshot, {})
    monkeypatch.setattr(plugin_mod, "_saved_selection_mode", lambda p: True)
    assert plugin_mod._scheduled_run_settings(Plugin(), snapshot, LOG) == (None, None)

    def broken(p):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(plugin_mod, "_saved_selection_mode", broken)
    assert plugin_mod._scheduled_run_settings(Plugin(), snapshot, LOG) == (None, None)


def test_negative_content_length_is_rejected(http):
    base, _ = http
    host, port = base.rsplit("/", 1)[1].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nX-VOD2MLIB: 1\r\n"
                  b"Content-Length: -1\r\n\r\n")  # and the connection stays open
        assert s.recv(200).split(b"\r\n")[0].endswith(b"400 Bad Request")


def test_unmanaged_strm_at_the_target_path_is_left_alone(env):
    plugin, catalogue, store, settings, tmp_path = env
    folder = _matrix_folder(tmp_path)
    folder.mkdir(parents=True)
    strm = folder / "The Matrix (1999).strm"
    strm.write_text("http://example.com/my-own-rip.mkv")
    store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
    r = _apply(env)
    assert r["errors"] == 1 and "Scan library" in r["failures"][0]
    assert strm.read_text() == "http://example.com/my-own-rip.mkv"
    assert store.files_for("movie", MATRIX) == []


def test_classic_strm_for_the_same_title_is_taken_over(env):
    # What Scan library would adopt: a Dispatcharr link to this very title.
    plugin, catalogue, store, settings, tmp_path = env
    folder = _matrix_folder(tmp_path)
    folder.mkdir(parents=True)
    strm = folder / "The Matrix (1999).strm"
    strm.write_text(f"http://10.0.0.5:9191/proxy/vod/movie/{MATRIX}?stream_id=201")
    store.set_desired("movie", MATRIX, True, 1, 101, "The Matrix")
    assert _apply(env)["added"] == 1
    assert "stream_id=101" in strm.read_text() and str(strm) in store.files_for("movie", MATRIX)


def test_a_stalled_connection_is_closed(env):
    plugin, catalogue, store, settings, _ = env
    service = SelectionService(plugin, catalogue, store, lambda: settings, LOG)
    handler = make_handler(service)
    handler.timeout = 0.5
    server = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(server.server_address, timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\n")  # headers never finished
            assert s.recv(100) == b""  # the server gave up and closed it
    finally:
        server.shutdown()
        server.server_close()


def test_parallel_wrong_passwords_are_answered_one_per_second(http):
    import time as _t
    base, _ = http
    results = []

    def guess():
        results.append(_req(base, "POST", "/api/login", {"password": "wrong"})[0])
    threads = [threading.Thread(target=guess) for _ in range(3)]
    started = _t.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [401, 401, 401] and _t.monotonic() - started >= 2.9
