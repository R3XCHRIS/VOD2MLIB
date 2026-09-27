"""The selection page server (stdlib only).

Dispatcharr instantiates the plugin in every web and Celery process, so each
process runs a small watchdog that tries to bind the port while selection
mode is ON. Only one bind can succeed, which makes the port itself the
single-owner lock; the rest keep retrying quietly, which also re-homes the
server if the owning process is recycled.
"""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import uuid as uuid_mod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import probe
from .catalogue import KINDS, _copy_id, copy_key, valid_decade
from .relink import relink_missing, titles_missing
from .upkeep import _ranked_copies
from .copyinfo import clean_prefs, default_audio_ok, language_match, merge_probe, rank, rank_key, title_prefs

# Offered in the preferences dialog even before any probe has seen them.
COMMON_LANGS = ["EN", "DE", "FR", "ES", "IT", "NL"]

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_COOKIE = "vod2mlib_session"
SESSION_TTL = 7 * 24 * 3600
CSRF_HEADER = "X-VOD2MLIB"  # custom header: forces a CORS preflight cross-site
MAX_BODY = 1 << 20
REQUEST_TIMEOUT = 30  # seconds a connection may sit idle or half-sent
LOGIN_WAIT = 5  # seconds a login waits behind other attempts before a 429
PAGE_SIZE_MAX = 200
PROBE_FRESH = 7 * 24 * 3600  # "probe all selected" skips copies probed this recently
PROBE_BUSY_STOP = 3  # ...and stops after this many "no free connection" copies in a row
RELINK_CHECK_INTERVAL = 60  # seconds between page listings' checks for re-created titles
AGGREGATE_TTL = 60  # seconds a listing's provider/category/decade/New counts are reused


class SelectionService:
    """Everything the HTTP handler needs, independent of HTTP."""

    def __init__(self, plugin, catalogue, store, settings_provider, logger):
        self.plugin = plugin
        self.catalogue = catalogue
        self.store = store
        self.settings_provider = settings_provider
        self.logger = logger
        self._relink_checked_at = float("-inf")
        self._aggregates = {}  # key -> (monotonic time, value); see _cached
        self._lock = threading.Lock()
        self._job = {"running": False}
        self._probe_job = {"running": False}
        self._upkeep_job = {"running": False}
        self._adopt_job = {"running": False}
        self.probe_run = probe.run  # (url) -> info; replaced in tests

    # ---------- auth ----------

    # Sessions live in selection.db so a Dispatcharr restart (every plugin
    # update) doesn't log the page out. Only a hash of the token is stored,
    # tied to the password it was issued under: changing the password ends
    # every session.

    def _password(self):
        return (self.settings_provider().get("selection_password") or "").strip()

    def login(self, password):
        expected = self._password()
        if not expected or not hmac.compare_digest(str(password or ""), expected):
            return None
        token = secrets.token_urlsafe(32)
        self.store.add_session(_sha256(token), _sha256(expected), time.time() + SESSION_TTL)
        return token

    def logout(self, token):
        if token:
            self.store.delete_session(_sha256(token))

    def is_authenticated(self, token):
        if not token:
            return False
        session = self.store.session(_sha256(token))
        if not session:
            return False
        password = self._password()
        if (session["expires_at"] > time.time() and password
                and hmac.compare_digest(session["password_tag"], _sha256(password))):
            return True
        self.store.delete_session(session["token_hash"])
        return False

    # ---------- catalogue + selection ----------

    def _relink(self):
        """Find selected titles Dispatcharr re-created under a new uuid, so
        the page shows them selected. Checked at most once a minute, and the
        disk lease is taken only when a title actually vanished, so a
        listing never blocks Apply. Skipped while Apply or upkeep writes."""
        now = time.monotonic()
        if now - self._relink_checked_at < RELINK_CHECK_INTERVAL:
            return
        self._relink_checked_at = now
        try:
            if not titles_missing(self.catalogue, self.store):
                return
        except Exception as e:  # the listing matters more
            self.logger.warning("Relink check failed: %s", e)
            return
        holder = f"relink-{secrets.token_hex(4)}"
        if not self.store.acquire_disk_lease(holder):
            return
        try:
            relink_missing(self.catalogue, self.store, self.logger)
        except Exception as e:  # the listing matters more
            self.logger.warning("Relink failed: %s", e)
        finally:
            self.store.release_disk_lease(holder)

    def _cached(self, key, compute):
        """Catalogue-wide counts (providers, categories, decades, New) change
        only when Dispatcharr refreshes, but cost more than the page itself
        on a large catalogue; keep each for AGGREGATE_TTL seconds."""
        now = time.monotonic()
        hit = self._aggregates.get(key)
        if hit and now - hit[0] < AGGREGATE_TTL:
            return hit[1]
        value = compute()
        self._aggregates[key] = (now, value)
        return value

    def list_titles(self, kind, q="", state="all", page=1, page_size=50, account=None, category="", year=""):
        settings = self.settings_provider()
        account = None if account in (None, "") else int(account)
        if year and not valid_decade(year):
            raise ValueError(f"unknown year filter {year!r} (use e.g. 2010s, before-1950, none)")
        self._relink()
        page = max(1, int(page))
        page_size = max(1, min(PAGE_SIZE_MAX, int(page_size)))
        include = exclude = None
        if state == "selected":
            include = self.store.uuids_where(kind, True)
        elif state == "ignored":
            exclude = self.store.uuids_where(kind, True)
        elif state == "pending":
            include = [r["content_uuid"] for r in self.store.pending(kind)]
        elif state == "flagged":
            include = self.store.flagged_uuids(kind)
        seen = self.store.seen_at(kind)
        if seen is None:  # first visit: start the clock, nothing is "new" yet
            self.store.set_seen(kind)
            seen = self.store.seen_at(kind)
        result = self.catalogue.list_titles(
            kind, settings, q=q, include_uuids=include, exclude_uuids=exclude,
            offset=(page - 1) * page_size, limit=page_size,
            added_after=seen if state == "new" else None, account_id=account,
            category=category or None, decade=year or None,
        )
        filters = (settings.get("category_filter"), settings.get("category_exclude"))
        result["new_total"] = self._cached(
            ("new_total", kind, seen, filters),
            lambda: self.catalogue.list_titles(kind, settings, added_after=seen, limit=0)["total"])
        result["accounts"] = self._cached(("accounts", kind, filters),
                                          lambda: self.catalogue.accounts(kind, settings))
        result.update(self._cached(("facets", kind, account, filters),
                                   lambda: self.catalogue.facets(kind, settings, account_id=account)))
        rows = self.store.get_many(kind, [i["uuid"] for i in result["items"]])
        pending = self.store.pending_uuids(kind, [i["uuid"] for i in result["items"]])
        probes = self.store.probes(kind, [(c["account_id"], c["stream_id"])
                                          for i in result["items"] for c in i["copies"]])
        global_prefs = self.store.prefs()
        for item in result["items"]:
            row = rows.get(item["uuid"]) or {}
            prefs = title_prefs(global_prefs, row.get("audio_override"))
            for c in item["copies"]:
                c["info"] = merge_probe(c["info"], probes.get((c["account_id"], str(c["stream_id"]))))
                c["info"]["match"] = ("match", "unknown", "mismatch")[language_match(c["info"], prefs)]
                c["info"]["default_audio_ok"] = default_audio_ok(c["info"], prefs)
            item["copies"] = rank(item["copies"], prefs)
            item["audio_override"] = row.get("audio_override")
            item["selected"] = bool(row.get("desired_selected"))
            # 'no_copy' stays applied so upkeep can restore it, but its files are gone.
            item["on_disk"] = bool(row.get("applied_selected")) and row.get("flag") != "no_copy"
            item["chosen"] = (
                {"account_id": row["desired_account_id"], "stream_id": row["desired_stream_id"]}
                if row.get("desired_selected") else None
            )
            item["better_copy"] = _better_copy(item, row, prefs)
            item["flag"] = row.get("flag")
            item["new"] = (item.get("added_at") or 0) > seen
            item["flag_detail"] = row.get("flag_detail")
            item["pending"] = item["uuid"] in pending
            item["error"] = row.get("last_error")
            if kind == "series":
                item["excluded_seasons"] = self.store.excluded_seasons(row, "desired") if row else []
        result.update(page=page, page_size=page_size)
        return result

    def set_selection(self, kind, content_uuid, body):
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        selected = bool(body.get("selected"))
        account_id = body.get("account_id")
        stream_id = body.get("stream_id")
        if selected and (account_id is None or stream_id is None):
            raise ValueError("selecting a title needs account_id and stream_id (the copy)")
        excluded = body.get("excluded_seasons")
        if excluded is not None:
            if kind != "series" or not isinstance(excluded, list):
                raise ValueError("excluded_seasons must be a list of season numbers, for a series")
            excluded = [int(s) for s in excluded]
        self.store.set_desired(
            kind, content_uuid, selected,
            account_id=int(account_id) if selected else None,
            stream_id=str(stream_id) if selected else None,
            title=body.get("title"),
            excluded_seasons=excluded,
        )
        return {"ok": True}

    def set_override(self, kind, content_uuid, body):
        """Per-title audio language: {"audio": "FR"} or {"audio": null}."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if "audio" not in body:
            raise ValueError("audio is required (a language code, or null to follow the preferences)")
        language = body["audio"]
        if language is not None:
            if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,3}", language.strip()):
                raise ValueError("audio must be a 2- or 3-letter language code")
            language = language.strip().upper()
        self.store.set_audio_override(kind, content_uuid, language, title=body.get("title"))
        return {"ok": True, "audio_override": language}

    def get_prefs(self):
        prefs = self.store.prefs()
        audio, subs = self.store.probed_languages()
        return {"prefs": prefs, "seen": {
            "audio": sorted(audio | set(COMMON_LANGS) | set(prefs["audio"])),
            "subtitles": sorted(subs | set(COMMON_LANGS) | set(prefs["subtitles"])),
        }}

    def set_prefs(self, body):
        prefs = clean_prefs(body)
        self.store.set_prefs(prefs)
        return {"ok": True, "prefs": prefs}

    def series_seasons(self, content_uuid, account_id, stream_id):
        """Seasons of one series copy, with episode counts, and the desired
        exclusions. Fetches the copy's episodes from the provider."""
        from .apply import season_counts
        series, relation = self.catalogue.resolve_copy("series", content_uuid, int(account_id), stream_id)
        if series is None:
            raise LookupError("that copy is no longer available from the provider")
        episodes = self.catalogue.series_episodes(relation)
        row = self.store.get_many("series", [content_uuid]).get(content_uuid) or {}
        return {"seasons": season_counts(episodes),
                "excluded": self.store.excluded_seasons(row, "desired") if row else []}

    def start_probe_title(self, kind, content_uuid):
        """The per-title Probe button: probe the title's copies in the
        background (a title can have a dozen copies at a few seconds each),
        the chosen copy first, then the rest best-ranked first, skipping
        copies probed successfully in the last PROBE_FRESH seconds."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        copies = self.catalogue.title_copies(kind, content_uuid)
        if not copies:
            raise LookupError("no copies of that title are available")
        row = self.store.get_many(kind, [content_uuid]).get(content_uuid) or {}
        chosen = ((row.get("desired_account_id"), str(row.get("desired_stream_id")))
                  if row.get("desired_selected") else None)
        prefs = title_prefs(self.store.prefs(), row.get("audio_override"))

        def todo():
            ranked = _ranked_copies(self.catalogue, self.store, kind, content_uuid, prefs)
            ranked.sort(key=lambda c: copy_key(kind, c[1]) != chosen)  # stable: chosen first
            return self._not_fresh([(kind, title, rel) for title, rel in ranked])

        return self._start_probe_job(
            todo, "Nothing to probe: every copy of this title was probed in the last 7 days")

    def _probe_copy(self, kind, title, rel):
        """Probe one copy and store the outcome. Never raises ProbeError."""
        copy_id = _copy_id(kind, rel)
        entry = {"account_id": rel.m3u_account_id, "stream_id": copy_id, "ok": False, "error": None}
        base = probe.internal_base_url()
        try:
            if kind == "movie":
                url = self.plugin._build_proxy_url(base, "movie", title.uuid, rel.stream_id)
            else:
                episodes = self.catalogue.series_episodes(rel)
                if not episodes:
                    raise probe.ProbeError("the provider lists no episodes for this copy")
                episode, stream_id, _title = episodes[0]
                url = self.plugin._build_proxy_url(base, "episode", episode.uuid, stream_id)
            result = self.probe_run(url)
            self.store.save_probe(kind, rel.m3u_account_id, copy_id, result=result)
            entry["ok"] = True
        except Exception as e:  # e.g. the episode fetch failed: record it, keep going
            if not isinstance(e, probe.ProbeError):
                self.logger.exception("Probe failed for %s copy %s", kind, copy_id)
            entry["error"] = str(e) or type(e).__name__
            entry["busy"] = getattr(e, "busy", False)
            self.store.save_probe(kind, rel.m3u_account_id, copy_id, error=entry["error"])
        return entry

    # ---------- "probe all selected" job ----------

    def copies_to_probe(self, now=None):
        """[(kind, title, relation)] for every copy of every selected title,
        except copies probed successfully in the last PROBE_FRESH seconds."""
        todo = []
        for kind in KINDS:
            todo += [(kind, title, rel) for uuid in self.store.uuids_where(kind, True)
                     for title, rel in self.catalogue.title_copies(kind, uuid)]
        return self._not_fresh(todo, now)

    def _not_fresh(self, copies, now=None):
        """`copies` ([(kind, title, relation)]) without those probed
        successfully in the last PROBE_FRESH seconds; order kept."""
        now = now or time.time()
        done = {}
        for kind in {k for k, _, _ in copies}:
            done[kind] = self.store.probes(kind, [(rel.m3u_account_id, _copy_id(kind, rel))
                                                  for k, _, rel in copies if k == kind])
        out = []
        for kind, title, rel in copies:
            row = done[kind].get((rel.m3u_account_id, _copy_id(kind, rel)))
            fresh = row and row["result"] and not row["error"] and now - row["probed_at"] < PROBE_FRESH
            if not fresh:
                out.append((kind, title, rel))
        return out

    def start_probe_all(self):
        return self._start_probe_job(
            self.copies_to_probe,
            "Nothing to probe: every copy of the selected titles was probed recently")

    def _start_probe_job(self, todo_fn, nothing_message):
        """Run todo_fn() -> [(kind, title, relation)] and probe each copy, one
        at a time, in a background thread. One probe job at a time."""
        with self._lock:
            if self._probe_job.get("running"):
                return {"ok": False, "error": "Probing is already running", "job": dict(self._probe_job)}
            self._probe_job = {"running": True, "done": 0, "total": 0, "stop": False,
                               "started_at": time.time(), "result": None}

        def work():
            probed = failed = 0
            busy_in_a_row = 0
            message = None
            try:
                todo = todo_fn()
                with self._lock:
                    self._probe_job["total"] = len(todo)
                for i, (kind, title, rel) in enumerate(todo, 1):
                    with self._lock:
                        if self._probe_job["stop"]:
                            message = "Stopped"
                            break
                    entry = self._probe_copy(kind, title, rel)
                    probed += entry["ok"]
                    failed += not entry["ok"]
                    busy_in_a_row = busy_in_a_row + 1 if entry.get("busy") else 0
                    with self._lock:
                        self._probe_job["done"] = i
                    if busy_in_a_row >= PROBE_BUSY_STOP:
                        message = "Stopped: the provider has had no free connection for a while"
                        break
                if not todo:
                    message = nothing_message
            except Exception as e:
                self.logger.exception("Probing crashed")
                message = f"Probing crashed: {e}"
            summary = f"Probed {probed} cop{'y' if probed == 1 else 'ies'}" + (f", {failed} failed" if failed else "")
            result = {"message": f"{message}. {summary}" if message and (probed or failed) else message or summary,
                      "probed": probed, "failed": failed}
            self.logger.info("Probe: %s", result["message"])
            with self._lock:
                self._probe_job.update(running=False, result=result, finished_at=time.time())

        threading.Thread(target=work, name="vod2mlib-probe", daemon=True).start()
        return {"ok": True, "job": self.probe_job()}

    def stop_probe_all(self):
        with self._lock:
            if self._probe_job.get("running"):
                self._probe_job["stop"] = True
        return {"ok": True}

    def probe_job(self):
        with self._lock:
            return dict(self._probe_job)

    def pending(self):
        out = []
        for row in self.store.pending():
            if row["desired_selected"] and row.get("flag") == "duplicates":
                action = "tidy"
            elif row["desired_selected"] and row["applied_selected"]:
                action = "change"
            elif row["desired_selected"]:
                action = "add"
            else:
                action = "remove"
            out.append({
                "kind": row["kind"], "uuid": row["content_uuid"],
                "title": row.get("title") or row["content_uuid"],
                "action": action, "error": row.get("last_error"),
            })
        return {"items": out, "counts": self.store.counts(), "last_upkeep": self.store.last_upkeep()}

    def mark_seen(self, kind):
        """'Mark all seen': titles added from now on are new."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        self.store.set_seen(kind)
        return {"ok": True}

    def clear_flag(self, kind, content_uuid):
        row = self.store.get_many(kind, [content_uuid]).get(str(content_uuid)) or {}
        if row.get("flag") == "no_copy":
            # A state, not a notice: its files are gone and upkeep watches for
            # a copy. It clears when one returns or the title is unselected.
            raise ValueError("a title with no copy can't be dismissed; unselect it instead")
        self.store.set_flag(kind, content_uuid, None)
        return {"ok": True}

    # ---------- adoption of an existing library (scan, then adopt) ----------

    def adopt_status(self):
        with self._lock:
            job = {k: v for k, v in self._adopt_job.items() if k != "plan"}
        return {"adopted_at": self.store.adopted_at(), "job": job}

    def start_adopt_scan(self):
        from .adopt import scan, summary
        with self._lock:
            if self._adopt_job.get("running"):
                return {"ok": False, "error": "A library scan is already running"}
            self._adopt_job = {"running": True, "done": 0, "total": 0, "started_at": time.time()}

        def progress(done, total):
            with self._lock:
                self._adopt_job.update(done=done, total=total)

        def work():
            try:
                plan = scan(self.catalogue, self.store, self.settings_provider(), progress)
                update = {"plan": plan, "summary": summary(plan), "error": None}
            except Exception as e:
                self.logger.exception("Library scan crashed")
                update = {"plan": None, "summary": None, "error": f"Scan failed: {e}"}
            with self._lock:
                self._adopt_job.update(update, running=False, finished_at=time.time())

        threading.Thread(target=work, name="vod2mlib-adopt-scan", daemon=True).start()
        return {"ok": True}

    def adopt_now(self):
        from .adopt import adopt
        with self._lock:
            plan = self._adopt_job.get("plan")
            if self._adopt_job.get("running") or plan is None:
                raise ValueError("Scan the library first")
        holder = f"adopt-{os.getpid()}"
        if not self.store.acquire_disk_lease(holder):
            raise ValueError("Apply or upkeep is running; try again in a few minutes")
        try:
            result = adopt(self.catalogue, self.store, plan, self.logger)
        finally:
            self.store.release_disk_lease(holder)
        with self._lock:
            self._adopt_job = {"running": False, "result": result}
        return {"ok": True, **result}

    def skip_adopt(self):
        self.store.set_adopted()
        with self._lock:
            self._adopt_job = {"running": False}
        return {"ok": True}

    # ---------- upkeep on demand (the cron runs the same thing) ----------

    def start_upkeep(self):
        from .upkeep import run_upkeep
        with self._lock:
            if self._upkeep_job.get("running"):
                return {"ok": False, "error": "Upkeep is already running", "job": dict(self._upkeep_job)}
            self._upkeep_job = {"running": True, "done": 0, "total": 0, "started_at": time.time(), "result": None}

        def progress(done, total):
            with self._lock:
                self._upkeep_job.update(done=done, total=total)

        def work():
            try:
                result = run_upkeep(self.plugin, self.catalogue, self.store,
                                    self.settings_provider(), self.logger, progress)
            except Exception as e:
                self.logger.exception("Upkeep crashed")
                result = {"status": "error", "message": f"Upkeep crashed: {e}"}
            with self._lock:
                self._upkeep_job.update(running=False, result=result, finished_at=time.time())

        threading.Thread(target=work, name="vod2mlib-upkeep", daemon=True).start()
        return {"ok": True, "job": self.upkeep_job()}

    def upkeep_job(self):
        with self._lock:
            return dict(self._upkeep_job)

    # ---------- Apply job ----------

    def start_apply(self):
        from .apply import apply_pending
        with self._lock:
            if self._job.get("running"):
                return {"ok": False, "error": "An Apply is already running", "job": dict(self._job)}
            self._job = {"running": True, "done": 0, "total": 0,
                         "started_at": time.time(), "result": None}

        def progress(done, total):
            with self._lock:
                self._job.update(done=done, total=total)

        def work():
            try:
                result = apply_pending(self.plugin, self.catalogue, self.store,
                                       self.settings_provider(), self.logger, progress)
            except Exception as e:
                self.logger.exception("Apply crashed")
                result = {"status": "error", "message": f"Apply crashed: {e}"}
            with self._lock:
                self._job.update(running=False, result=result, finished_at=time.time())

        threading.Thread(target=work, name="vod2mlib-apply", daemon=True).start()
        return {"ok": True, "job": self.job()}

    def job(self):
        with self._lock:
            return dict(self._job)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _better_copy(item, row, prefs):
    """For a selected title: the top-ranked copy, if it beats the chosen one
    on anything but the stable tiebreaker. The chosen copy never changes by
    itself; the page offers the switch."""
    if not row.get("desired_selected") or not item["copies"]:
        return None
    chosen = next((c for c in item["copies"]
                   if c["account_id"] == row["desired_account_id"]
                   and str(c["stream_id"]) == row["desired_stream_id"]), None)
    best = item["copies"][0]
    if chosen is None or chosen is best or rank_key(best, prefs)[:-2] >= rank_key(chosen, prefs)[:-2]:
        return None
    return {"account_id": best["account_id"], "stream_id": str(best["stream_id"])}




def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VOD2MLIB-Selection"
        timeout = REQUEST_TIMEOUT  # a stalled client can't hold a thread forever

        def log_message(self, fmt, *args):  # keep container logs quiet
            pass

        # ----- helpers -----

        def _token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == SESSION_COOKIE:
                    return value
            return None

        def _send_json(self, status, payload, extra_headers=None):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:  # read(-1) would wait for EOF and hold the thread
                raise ValueError("invalid Content-Length")
            if length > MAX_BODY:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _require_auth(self, state_changing=False):
            if not service.is_authenticated(self._token()):
                self._send_json(401, {"error": "not logged in"})
                return False
            if state_changing and self.headers.get(CSRF_HEADER) != "1":
                self._send_json(403, {"error": "missing CSRF header"})
                return False
            return True

        def _cookie(self, value, max_age):
            return (f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age={max_age}")

        def _dispatch(self, method):
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    return self._static("index.html")
                if method == "GET" and path == "/api/session":
                    return self._send_json(200, {"authenticated": service.is_authenticated(self._token())})
                if method == "POST" and path == "/api/login":
                    if self.headers.get(CSRF_HEADER) != "1":
                        return self._send_json(403, {"error": "missing CSRF header"})
                    password = self._body().get("password")
                    # One attempt at a time, and a failed one holds the lock for
                    # a second: parallel guesses can't go faster than 1/s.
                    if not _login_lock.acquire(timeout=LOGIN_WAIT):
                        # Many attempts queued: answer instead of piling up threads.
                        return self._send_json(429, {"error": "too many login attempts; try again shortly"})
                    try:
                        token = service.login(password)
                        if not token:
                            time.sleep(1)
                    finally:
                        _login_lock.release()
                    if not token:
                        return self._send_json(401, {"error": "wrong password"})
                    return self._send_json(200, {"ok": True},
                                           {"Set-Cookie": self._cookie(token, SESSION_TTL)})
                if method == "POST" and path == "/api/logout":
                    service.logout(self._token())
                    return self._send_json(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})

                if not path.startswith("/api/"):
                    return self._send_json(404, {"error": "not found"})

                state_changing = method in ("POST", "PUT", "DELETE")
                if not self._require_auth(state_changing):
                    return

                if method == "GET" and path in ("/api/movies", "/api/series"):
                    kind = "movie" if path == "/api/movies" else "series"
                    return self._send_json(200, service.list_titles(
                        kind, q=query.get("q", ""), state=query.get("state", "all"),
                        page=query.get("page", 1), page_size=query.get("page_size", 50),
                        account=query.get("account"), category=query.get("category", ""),
                        year=query.get("year", "")))
                if method == "GET" and path.startswith("/api/series/") and path.endswith("/seasons"):
                    uuid = _title_uuid(path[len("/api/series/"):-len("/seasons")])
                    if "account_id" not in query or "stream_id" not in query:
                        raise ValueError("account_id and stream_id (the copy) are required")
                    return self._send_json(200, service.series_seasons(
                        uuid, query["account_id"], query["stream_id"]))
                if method == "GET" and path == "/api/adopt":
                    return self._send_json(200, service.adopt_status())
                if method == "POST" and path == "/api/adopt/scan":
                    return self._send_json(200, service.start_adopt_scan())
                if method == "POST" and path == "/api/adopt/apply":
                    return self._send_json(200, service.adopt_now())
                if method == "POST" and path == "/api/adopt/skip":
                    return self._send_json(200, service.skip_adopt())
                if method == "POST" and path == "/api/upkeep":
                    return self._send_json(200, service.start_upkeep())
                if method == "GET" and path == "/api/upkeep-job":
                    return self._send_json(200, service.upkeep_job())
                if method == "POST" and path.startswith("/api/seen/"):
                    return self._send_json(200, service.mark_seen(path[len("/api/seen/"):]))
                if method == "POST" and path.startswith("/api/clear-flag/"):
                    kind, _, uuid = path[len("/api/clear-flag/"):].partition("/")
                    return self._send_json(200, service.clear_flag(kind, _title_uuid(uuid)))
                if method == "GET" and path == "/api/prefs":
                    return self._send_json(200, service.get_prefs())
                if method == "PUT" and path == "/api/prefs":
                    return self._send_json(200, service.set_prefs(self._body()))
                if method == "POST" and path == "/api/probe-all":
                    return self._send_json(200, service.start_probe_all())
                if method == "POST" and path == "/api/probe-all/stop":
                    return self._send_json(200, service.stop_probe_all())
                if method == "GET" and path == "/api/probe-job":
                    return self._send_json(200, service.probe_job())
                if method == "POST" and path.startswith("/api/probe/"):
                    kind, _, uuid = path[len("/api/probe/"):].partition("/")
                    return self._send_json(200, service.start_probe_title(kind, _title_uuid(uuid)))
                if method == "PUT" and path.startswith("/api/override/"):
                    kind, _, uuid = path[len("/api/override/"):].partition("/")
                    return self._send_json(200, service.set_override(kind, _title_uuid(uuid), self._body()))
                if method == "PUT" and path.startswith("/api/selection/"):
                    kind, _, uuid = path[len("/api/selection/"):].partition("/")
                    return self._send_json(200, service.set_selection(kind, _title_uuid(uuid), self._body()))
                if method == "GET" and path == "/api/pending":
                    return self._send_json(200, service.pending())
                if method == "POST" and path == "/api/apply":
                    return self._send_json(200, service.start_apply())
                if method == "GET" and path == "/api/job":
                    return self._send_json(200, service.job())
                return self._send_json(404, {"error": "not found"})
            except LookupError as e:
                return self._send_json(404, {"error": str(e)})
            except (ValueError, TypeError) as e:
                return self._send_json(400, {"error": str(e)})
            except Exception as e:
                service.logger.exception("Selection page request failed: %s %s", method, self.path)
                return self._send_json(500, {"error": str(e)})

        def _static(self, name):
            path = os.path.join(STATIC_DIR, name)
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind an in-use port, which
    # would defeat the port-as-lock. Linux (the container) is unaffected.
    allow_reuse_address = os.name != "nt"



def _title_uuid(value):
    """A title uuid from the URL, canonical (lowercase), or ValueError (400)."""
    try:
        return str(uuid_mod.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"not a title id: {value!r}") from None


# ---------- per-process lifecycle ----------

_state = {"server": None, "thread": None, "port": None, "store": None, "watchdog": None,
          "watchdog_pid": None, "stop": None}
_state_lock = threading.Lock()
_login_lock = threading.Lock()


def start_server(service, port, host="0.0.0.0"):
    """Start serving in this process. Returns True if this process now owns
    the port, False if something else already holds it."""
    with _state_lock:
        if _state["server"] is not None:
            if _state["port"] == port:
                return True
            _stop_locked()
        try:
            server = _Server((host, port), make_handler(service))
        except OSError:
            return False
        thread = threading.Thread(target=server.serve_forever, name="vod2mlib-page", daemon=True)
        thread.start()
        _state.update(server=server, thread=thread, port=port, store=service.store)
        bound_port = server.server_address[1]
        try:
            service.store.set_owner(os.getpid(), bound_port)
        except Exception:
            service.logger.exception("Could not record the selection page owner")
        service.logger.info("Selection page serving on port %s (pid %s)", bound_port, os.getpid())
        return True


def _stop_locked():
    server = _state["server"]
    if server is not None:
        server.shutdown()
        server.server_close()
        try:
            _state["store"].clear_owner(os.getpid())
        except Exception:
            pass  # stale owner records are harmless: status also probes the port
    _state.update(server=None, thread=None, port=None, store=None)


def stop_server():
    with _state_lock:
        _stop_locked()


def owns_server():
    with _state_lock:
        return _state["server"] is not None


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def start_watchdog(tick, interval=30, first_delay=5):
    """Run `tick()` periodically in a daemon thread, once per process.
    Survives fork: a child process (new pid) gets its own watchdog."""
    with _state_lock:
        if _state["watchdog"] is not None and _state["watchdog_pid"] == os.getpid():
            return
        stop = threading.Event()

        def loop():
            if stop.wait(first_delay):
                return
            while True:
                try:
                    tick()
                except Exception:
                    pass  # the next tick retries; never kill the host process
                if stop.wait(interval):
                    return

        thread = threading.Thread(target=loop, name="vod2mlib-watchdog", daemon=True)
        _state.update(watchdog=thread, watchdog_pid=os.getpid(), stop=stop)
        thread.start()


def stop_watchdog():
    with _state_lock:
        if _state["stop"] is not None:
            _state["stop"].set()
        _state.update(watchdog=None, watchdog_pid=None, stop=None)
