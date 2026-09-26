"""SQLite store for Selection mode.

Each title has a *desired* state (edited on the page) and an *applied* state
(what is on disk). Pending changes are simply the rows where the two differ,
and Apply copies desired -> applied one title at a time.

A copy is identified by (account_id, stream_id), not the relation's primary
key, because Dispatcharr may re-create relations during a rescan.

A series also has excluded seasons (a sorted JSON list of season numbers).
Seasons are listed as excluded rather than included, so a season that
appears later is included automatically.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

SCHEMA_VERSION = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS selection (
    kind                TEXT NOT NULL,
    content_uuid        TEXT NOT NULL,
    title               TEXT,
    desired_selected    INTEGER NOT NULL DEFAULT 0,
    desired_account_id  INTEGER,
    desired_stream_id   TEXT,
    applied_selected    INTEGER NOT NULL DEFAULT 0,
    applied_account_id  INTEGER,
    applied_stream_id   TEXT,
    desired_excluded_seasons TEXT NOT NULL DEFAULT '[]',
    applied_excluded_seasons TEXT NOT NULL DEFAULT '[]',
    flag                TEXT,
    flag_detail         TEXT,
    audio_override      TEXT,
    tmdb_id             TEXT,
    last_error          TEXT,
    updated_at          REAL,
    PRIMARY KEY (kind, content_uuid)
);
CREATE TABLE IF NOT EXISTS applied_file (
    path          TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    content_uuid  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS applied_file_title ON applied_file (kind, content_uuid);
CREATE TABLE IF NOT EXISTS copy_probe (
    kind        TEXT NOT NULL,
    account_id  INTEGER NOT NULL,
    stream_id   TEXT NOT NULL,
    probed_at   REAL NOT NULL,
    result      TEXT,
    error       TEXT,
    PRIMARY KEY (kind, account_id, stream_id)
);
CREATE TABLE IF NOT EXISTS page_session (
    token_hash  TEXT PRIMARY KEY,  -- sha256 of the cookie; the cookie itself is never stored
    password_tag TEXT NOT NULL,    -- which page password it was issued under
    expires_at  REAL NOT NULL
);
"""

# Rows whose desired state differs from what is on disk. An adopted title
# found in more than one place (flag 'duplicates') is pending until Apply
# rewrites it and removes the extra files.
_PENDING_WHERE = """
    desired_selected != applied_selected
    OR (desired_selected = 1 AND (
        desired_account_id IS NOT applied_account_id
        OR desired_stream_id IS NOT applied_stream_id
        OR desired_excluded_seasons != applied_excluded_seasons
        OR flag IS 'duplicates'))
"""

# Columns added after version 1, with the definitions used to add them.
_ADDED_COLUMNS = {
    "desired_excluded_seasons": "TEXT NOT NULL DEFAULT '[]'",
    "applied_excluded_seasons": "TEXT NOT NULL DEFAULT '[]'",
    "flag": "TEXT",          # 'fallback' | 'no_copy', set by scheduled upkeep
    "flag_detail": "TEXT",
    "audio_override": "TEXT",  # per-title audio language (a preference, never pending)
    "tmdb_id": "TEXT",  # remembered to find the title again if Dispatcharr re-creates it
}
DISK_LEASE_TTL = 6 * 3600  # a crashed holder can't block writers for longer than this


def _seasons_json(seasons):
    return json.dumps(sorted({int(s) for s in seasons or []}))


class SelectionStore:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c):
        """Bring an older database up to SCHEMA_VERSION, keeping its rows.
        Adding a column is idempotent here, so a half-finished run is safe
        to repeat."""
        have = {r["name"] for r in c.execute("PRAGMA table_info(selection)")}
        for name, definition in _ADDED_COLUMNS.items():
            if name not in have:
                c.execute(f"ALTER TABLE selection ADD COLUMN {name} {definition}")
        c.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def schema_version(self):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else None

    @staticmethod
    def excluded_seasons(row, which="applied"):
        """The excluded season numbers of a row, as a sorted list."""
        return json.loads(row.get(f"{which}_excluded_seasons") or "[]")

    @contextmanager
    def _conn(self):
        # A connection per operation: the page server is multi-threaded and
        # sqlite3 connections must not be shared across threads.
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------- desired state (page) ----------

    def set_desired(self, kind, content_uuid, selected, account_id=None, stream_id=None, title=None,
                    excluded_seasons=None):
        """`excluded_seasons=None` keeps the current exclusions (a copy change
        keeps them); unselecting clears them."""
        if not selected:
            seasons = _seasons_json([])
        elif excluded_seasons is not None:
            seasons = _seasons_json(excluded_seasons)
        else:
            seasons = None
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO selection (kind, content_uuid, title, desired_selected,
                                       desired_account_id, desired_stream_id,
                                       desired_excluded_seasons, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, '[]'), ?)
                ON CONFLICT (kind, content_uuid) DO UPDATE SET
                    title = COALESCE(excluded.title, selection.title),
                    desired_selected = excluded.desired_selected,
                    desired_account_id = excluded.desired_account_id,
                    desired_stream_id = excluded.desired_stream_id,
                    desired_excluded_seasons = COALESCE(?, selection.desired_excluded_seasons),
                    updated_at = excluded.updated_at
                """,
                (kind, str(content_uuid), title, 1 if selected else 0,
                 account_id if selected else None,
                 str(stream_id) if (selected and stream_id is not None) else None,
                 seasons, time.time(), seasons),
            )

    def set_audio_override(self, kind, content_uuid, language, title=None):
        """Per-title audio language (None = follow the global preferences).
        Ranking only: it never makes a title pending."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO selection (kind, content_uuid, title, audio_override, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (kind, content_uuid) DO UPDATE SET
                    title = COALESCE(excluded.title, selection.title),
                    audio_override = excluded.audio_override
                """,
                (kind, str(content_uuid), title, language, time.time()),
            )

    def get_many(self, kind, content_uuids):
        """{uuid: row-dict} for the given uuids (missing uuids are simply absent)."""
        uuids = [str(u) for u in content_uuids]
        if not uuids:
            return {}
        marks = ",".join("?" * len(uuids))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM selection WHERE kind = ? AND content_uuid IN ({marks})",
                [kind, *uuids],
            ).fetchall()
        return {r["content_uuid"]: dict(r) for r in rows}

    def uuids_where(self, kind, desired_selected):
        with self._conn() as c:
            rows = c.execute(
                "SELECT content_uuid FROM selection WHERE kind = ? AND desired_selected = ?",
                (kind, 1 if desired_selected else 0),
            ).fetchall()
        return [r["content_uuid"] for r in rows]

    # ---------- pending / applied ----------

    def pending(self, kind=None):
        sql = f"SELECT * FROM selection WHERE ({_PENDING_WHERE})"
        args = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY title COLLATE NOCASE"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def pending_uuids(self, kind, content_uuids):
        """The given titles that are pending, by the same rule as pending()."""
        uuids = [str(u) for u in content_uuids]
        if not uuids:
            return set()
        with self._conn() as c:
            return {r[0] for r in c.execute(
                f"SELECT content_uuid FROM selection WHERE kind = ? AND content_uuid IN "
                f"({','.join('?' * len(uuids))}) AND ({_PENDING_WHERE})", [kind, *uuids])}

    def mark_applied(self, kind, content_uuid, written=None):
        """Record what Apply wrote. `written` is the row Apply worked from; a
        change made on the page meanwhile then stays pending instead of being
        recorded as applied. Without it, the current desired state is used.

        Upkeep flags describe the copy upkeep left behind, so they are
        cleared when the title is unselected or another copy is applied;
        'duplicates' is cleared by any Apply (it tidies the extras)."""
        with self._conn() as c:
            if written is None:
                written = c.execute("SELECT * FROM selection WHERE kind = ? AND content_uuid = ?",
                                    (kind, str(content_uuid))).fetchone()
                if written is None:
                    return
                written = dict(written)
            selected = int(bool(written["desired_selected"]))
            account_id = written["desired_account_id"] if selected else None
            stream_id = written["desired_stream_id"] if selected else None
            seasons = written["desired_excluded_seasons"]
            c.execute(
                """
                UPDATE selection SET
                    flag = CASE WHEN flag = 'duplicates' OR NOT :sel
                                     OR applied_account_id IS NOT :acc
                                     OR applied_stream_id IS NOT :stream
                                THEN NULL ELSE flag END,
                    flag_detail = CASE WHEN flag = 'duplicates' OR NOT :sel
                                            OR applied_account_id IS NOT :acc
                                            OR applied_stream_id IS NOT :stream
                                       THEN NULL ELSE flag_detail END,
                    applied_selected = :sel,
                    applied_account_id = :acc,
                    applied_stream_id = :stream,
                    applied_excluded_seasons = :seasons,
                    last_error = NULL,
                    updated_at = :now
                WHERE kind = :kind AND content_uuid = :uuid
                """,
                {"sel": selected, "acc": account_id, "stream": stream_id, "seasons": seasons,
                 "now": time.time(), "kind": kind, "uuid": str(content_uuid)},
            )

    def set_error(self, kind, content_uuid, error):
        with self._conn() as c:
            c.execute(
                "UPDATE selection SET last_error = ? WHERE kind = ? AND content_uuid = ?",
                (error, kind, str(content_uuid)),
            )

    def counts(self):
        with self._conn() as c:
            by_kind = dict(c.execute(
                "SELECT kind, COUNT(*) FROM selection WHERE applied_selected = 1 GROUP BY kind"
            ).fetchall())
            pending = c.execute(
                f"SELECT COUNT(*) FROM selection WHERE ({_PENDING_WHERE})").fetchone()[0]
        return {"applied_selected": sum(by_kind.values()), "pending": pending,
                "movies_on_disk": by_kind.get("movie", 0),
                "series_on_disk": by_kind.get("series", 0)}

    # ---------- files written by Apply ----------

    def record_file(self, kind, content_uuid, path):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO applied_file (path, kind, content_uuid) VALUES (?, ?, ?)",
                (path, kind, str(content_uuid)),
            )

    def files_for(self, kind, content_uuid):
        with self._conn() as c:
            rows = c.execute(
                "SELECT path FROM applied_file WHERE kind = ? AND content_uuid = ?",
                (kind, str(content_uuid)),
            ).fetchall()
        return [r["path"] for r in rows]

    def forget_paths(self, paths):
        with self._conn() as c:
            c.executemany("DELETE FROM applied_file WHERE path = ?", [(p,) for p in paths])

    def forget_files(self, kind, content_uuid):
        with self._conn() as c:
            c.execute(
                "DELETE FROM applied_file WHERE kind = ? AND content_uuid = ?",
                (kind, str(content_uuid)),
            )

    # ---------- adoption of an existing library ----------

    def adopted_at(self):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'adopted_at'").fetchone()
        return float(row["value"]) if row else None

    def set_adopted(self, when=None):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('adopted_at', ?)",
                      (str(when or time.time()),))

    def is_known(self, kind, content_uuid):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM selection WHERE kind = ? AND content_uuid = ?",
                             (kind, str(content_uuid))).fetchone() is not None

    def adopt_title(self, kind, content_uuid, title, account_id, copy_id, paths, duplicates_detail=None):
        """Record files already on disk as this title's, selected and applied
        with the given copy. Does nothing for a title the store already has."""
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO selection (kind, content_uuid, title, desired_selected,
                    desired_account_id, desired_stream_id, applied_selected, applied_account_id,
                    applied_stream_id, flag, flag_detail, updated_at)
                VALUES (?, ?, ?, 1, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (kind, str(content_uuid), title, account_id, str(copy_id), account_id, str(copy_id),
                 "duplicates" if duplicates_detail else None, duplicates_detail, time.time()),
            )
            if cur.rowcount == 0:
                return False
            c.executemany(
                "INSERT OR IGNORE INTO applied_file (path, kind, content_uuid) VALUES (?, ?, ?)",
                [(p, kind, str(content_uuid)) for p in paths],
            )
            return True

    # ---------- scheduled upkeep ----------

    def applied_rows(self):
        """Every applied title, whatever its desired state. A 'no_copy' title
        stays applied (with its files deleted), so it is looked at again on
        every run and restored when a copy comes back."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM selection WHERE applied_selected = 1 ORDER BY kind, title COLLATE NOCASE")]

    def set_flag(self, kind, content_uuid, flag, detail=None):
        with self._conn() as c:
            c.execute("UPDATE selection SET flag = ?, flag_detail = ? WHERE kind = ? AND content_uuid = ?",
                      (flag, detail, kind, str(content_uuid)))

    def flagged_uuids(self, kind):
        with self._conn() as c:
            return [r["content_uuid"] for r in c.execute(
                "SELECT content_uuid FROM selection WHERE kind = ? AND flag IS NOT NULL", (kind,))]

    def switch_applied_copy(self, kind, content_uuid, account_id, stream_id, selected=True):
        """Upkeep changed what is on disk. The desired copy follows only when
        it was the applied one (no un-Applied change of the user's is lost)."""
        with self._conn() as c:
            c.execute(
                """
                UPDATE selection SET
                    desired_account_id = CASE WHEN desired_selected = 1
                        AND desired_account_id IS applied_account_id
                        AND desired_stream_id IS applied_stream_id THEN ? ELSE desired_account_id END,
                    desired_stream_id = CASE WHEN desired_selected = 1
                        AND desired_account_id IS applied_account_id
                        AND desired_stream_id IS applied_stream_id THEN ? ELSE desired_stream_id END,
                    applied_selected = ?, applied_account_id = ?, applied_stream_id = ?,
                    updated_at = ?
                WHERE kind = ? AND content_uuid = ?
                """,
                (account_id, None if stream_id is None else str(stream_id),
                 1 if selected else 0, account_id, None if stream_id is None else str(stream_id),
                 time.time(), kind, str(content_uuid)),
            )

    def set_last_upkeep(self, result):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_upkeep', ?)",
                      (json.dumps(result),))

    def last_upkeep(self):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'last_upkeep'").fetchone()
        return json.loads(row["value"]) if row else None

    def acquire_disk_lease(self, holder, now=None):
        """Apply (page process) and upkeep (Celery worker) must not write at
        the same time. True if `holder` now holds the lease."""
        now = now or time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT value FROM meta WHERE key = 'disk_lease'").fetchone()
            lease = json.loads(row["value"]) if row else None
            if lease and lease["holder"] != holder and lease["until"] > now:
                return False
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('disk_lease', ?)",
                      (json.dumps({"holder": holder, "until": now + DISK_LEASE_TTL}),))
            return True

    def release_disk_lease(self, holder):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'disk_lease'").fetchone()
            if row and json.loads(row["value"])["holder"] == holder:
                c.execute("DELETE FROM meta WHERE key = 'disk_lease'")

    def disk_lease_holder(self):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'disk_lease'").fetchone()
        lease = json.loads(row["value"]) if row else None
        return lease["holder"] if lease and lease["until"] > time.time() else None

    # ---------- deep probe results, per copy ----------

    def save_probe(self, kind, account_id, stream_id, result=None, error=None):
        """A failed probe keeps the last good result and records the error."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO copy_probe (kind, account_id, stream_id, probed_at, result, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (kind, account_id, stream_id) DO UPDATE SET
                    probed_at = excluded.probed_at,
                    result = COALESCE(excluded.result, copy_probe.result),
                    error = excluded.error
                """,
                (kind, int(account_id), str(stream_id), time.time(),
                 json.dumps(result) if result is not None else None, error),
            )

    def probes(self, kind, copies):
        """{(account_id, stream_id): {"probed_at", "result", "error"}} for the
        given (account_id, stream_id) pairs that have been probed."""
        keys = {(int(a), str(s)) for a, s in copies}
        if not keys:
            return {}
        accounts = sorted({a for a, _ in keys})
        streams = sorted({s for _, s in keys})
        rows = []
        with self._conn() as c:
            # Only the requested stream ids (the primary key's index serves
            # this), in chunks that stay under SQLite's parameter limit.
            for i in range(0, len(streams), 500):
                chunk = streams[i:i + 500]
                rows += c.execute(
                    f"SELECT * FROM copy_probe WHERE kind = ? "
                    f"AND account_id IN ({','.join('?' * len(accounts))}) "
                    f"AND stream_id IN ({','.join('?' * len(chunk))})",
                    [kind, *accounts, *chunk],
                ).fetchall()
        out = {}
        for r in rows:
            key = (r["account_id"], r["stream_id"])
            if key in keys:
                out[key] = {"probed_at": r["probed_at"], "error": r["error"],
                            "result": json.loads(r["result"]) if r["result"] else None}
        return out

    # ---------- ranking preferences (edited on the page) ----------

    def prefs(self):
        from .copyinfo import DEFAULT_PREFS
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'prefs'").fetchone()
        return dict(DEFAULT_PREFS, **json.loads(row["value"])) if row else dict(DEFAULT_PREFS)

    def set_prefs(self, prefs):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('prefs', ?)", (json.dumps(prefs),))

    def probed_languages(self):
        """({audio}, {subtitle}) language codes seen in any probe result."""
        audio, subs = set(), set()
        with self._conn() as c:
            for (result,) in c.execute("SELECT result FROM copy_probe WHERE result IS NOT NULL"):
                r = json.loads(result)
                audio.update(r.get("langs") or [])
                subs.update(r.get("subtitle_langs") or [])
        return audio, subs

    # ---------- page server owner ----------
    # Page status runs in whichever web worker handles the click, so the
    # process serving the page records itself here for status to report.

    # ---------- relink (Dispatcharr re-created a title under a new uuid) ----------

    def relink_candidates(self, kind):
        """Rows worth keeping: selected, on disk, or with a language override."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM selection WHERE kind = ? AND (desired_selected = 1 OR applied_selected = 1 "
                "OR audio_override IS NOT NULL)", (kind,))]

    def set_tmdb_ids(self, kind, mapping):
        with self._conn() as c:
            c.executemany(
                "UPDATE selection SET tmdb_id = ? WHERE kind = ? AND content_uuid = ? AND tmdb_id IS NOT ?",
                [(str(t), kind, str(u), str(t)) for u, t in mapping.items()])

    _DESIRED = ("desired_selected", "desired_account_id", "desired_stream_id", "desired_excluded_seasons")
    _APPLIED = ("applied_selected", "applied_account_id", "applied_stream_id", "applied_excluded_seasons",
                "flag", "flag_detail", "last_error")

    def move_title(self, kind, old_uuid, new_uuid):
        """Move a row and its recorded files to a new uuid. If the new uuid
        already has a row (e.g. selected meanwhile), the old row's on-disk
        state moves into it; its choices win only if it isn't selected."""
        old_uuid, new_uuid = str(old_uuid), str(new_uuid)
        with self._conn() as c:
            old = c.execute("SELECT * FROM selection WHERE kind = ? AND content_uuid = ?",
                            (kind, old_uuid)).fetchone()
            new = c.execute("SELECT * FROM selection WHERE kind = ? AND content_uuid = ?",
                            (kind, new_uuid)).fetchone()
            if old is None:
                return
            if new is None:
                c.execute("UPDATE selection SET content_uuid = ? WHERE kind = ? AND content_uuid = ?",
                          (new_uuid, kind, old_uuid))
            else:
                merged = dict(new)
                for col in self._APPLIED:
                    merged[col] = old[col]
                if not new["desired_selected"]:
                    for col in self._DESIRED:
                        merged[col] = old[col]
                for col in ("title", "audio_override", "tmdb_id"):
                    merged[col] = new[col] if new[col] is not None else old[col]
                cols = [k for k in merged if k not in ("kind", "content_uuid")]
                c.execute(f"UPDATE selection SET {', '.join(f'{k} = ?' for k in cols)} "
                          "WHERE kind = ? AND content_uuid = ?",
                          [merged[k] for k in cols] + [kind, new_uuid])
                c.execute("DELETE FROM selection WHERE kind = ? AND content_uuid = ?", (kind, old_uuid))
            c.execute("UPDATE applied_file SET content_uuid = ? WHERE kind = ? AND content_uuid = ?",
                      (new_uuid, kind, old_uuid))

    # ---------- "new since last visit" (per kind) ----------

    def seen_at(self, kind):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (f"seen_at_{kind}",)).fetchone()
        return float(row["value"]) if row else None

    def set_seen(self, kind, when=None):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                      (f"seen_at_{kind}", str(time.time() if when is None else when)))

    # ---------- page sessions (kept across Dispatcharr restarts) ----------

    def add_session(self, token_hash, password_tag, expires_at):
        with self._conn() as c:
            c.execute("DELETE FROM page_session WHERE expires_at <= ?", (time.time(),))
            c.execute("INSERT OR REPLACE INTO page_session VALUES (?, ?, ?)",
                      (token_hash, password_tag, expires_at))

    def session(self, token_hash):
        with self._conn() as c:
            row = c.execute("SELECT * FROM page_session WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash):
        with self._conn() as c:
            c.execute("DELETE FROM page_session WHERE token_hash = ?", (token_hash,))

    def set_owner(self, pid, port):
        owner = json.dumps({"pid": pid, "port": port, "started_at": time.time()})
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('owner', ?)", (owner,))

    def owner(self):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'owner'").fetchone()
        return json.loads(row["value"]) if row else None

    def clear_owner(self, pid):
        """Clear the record only if `pid` still owns it, so a process that
        lost the port can't wipe the record of the one that now serves."""
        owner = self.owner()
        if owner and owner.get("pid") == pid:
            with self._conn() as c:
                c.execute("DELETE FROM meta WHERE key = 'owner'")
