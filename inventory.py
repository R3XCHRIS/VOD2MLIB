"""Persistent plugin inventory; all database operations belong to the action thread."""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

BATCH_SIZE = 1000
LOOKUP_BATCH_SIZE = 900  # Works even with SQLite builds limited to 999 parameters.


def state_directory():
    # Dispatcharr installs plugins in /data/plugins/<slug>. Never keep state in
    # the replaceable installation or in either media output root.
    return Path(os.environ.get("VOD2MLIB_STATE_DIR", "/data/vod2mlib"))


@contextmanager
def action_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "action.lock", "a+b") as lock:
        if os.name == "nt":
            import msvcrt

            try:
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Another generation or cleanup is running") from None
        else:
            import fcntl

            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("Another generation or cleanup is running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def file_hash(path):
    try:
        with open(path, "rb") as source:
            return (
                hashlib.file_digest(source, "sha256").hexdigest()
                if hasattr(hashlib, "file_digest")
                else hashlib.sha256(source.read()).hexdigest()
            )
    except FileNotFoundError:
        return None


def strm_contents(path):
    """Read a small STRM verbatim for comparison with its recorded URL."""
    try:
        with open(path, "rb") as source:
            contents = source.read(8193)
        if len(contents) > 8192:
            return None
        return contents.decode("utf-8")
    except (FileNotFoundError, UnicodeError):
        return None


def contained(path, roots):
    # Try the lexical parent first, but still resolve both sides below. This only
    # changes lookup order: aliases and symlink escapes retain their semantics.
    absolute = os.path.normcase(os.path.abspath(path))
    roots = sorted(roots, key=lambda root: not absolute.startswith(
        os.path.normcase(os.path.abspath(root)).rstrip(os.sep) + os.sep))
    resolved = os.path.realpath(path)
    for root in roots:
        base = os.path.realpath(root)
        try:
            if resolved != base and os.path.commonpath([resolved, base]) == base:
                return True
        except ValueError:
            pass
    return False


def remove_strm(row, roots, dry_run=False):
    """Verify and remove one STRM without touching SQLite or shared NFOs."""
    path = row['path']
    if not contained(path, roots):
        return 'preserved'
    missing = not os.path.lexists(path)
    if not missing and (os.path.islink(path) or strm_contents(path) != row['strm_url']):
        return 'preserved'
    if dry_run:
        return 'missing' if missing else 'candidate'
    if not missing:
        os.remove(path)
    return 'missing' if missing else 'deleted'


class InventoryStore:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(directory) / "inventory.sqlite3")
        self.db = sqlite3.connect(self.db_path, timeout=10)
        self.db.row_factory = sqlite3.Row
        # Bound the temporary census B-tree cache to 32 MiB. Keep temporary
        # tables on disk and leave persistent inventory durability unchanged.
        self.db.execute("PRAGMA temp.cache_size=-32768")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            self.db.close()
            raise ValueError("Unsupported inventory schema")
        with self.db:
            self.db.executescript("""
                BEGIN;
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    year TEXT, tmdb TEXT, imdb TEXT, season INTEGER, episode INTEGER,
                    strm_url TEXT NOT NULL, nfos TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS provider_ids ON files(kind,tmdb,imdb);
                CREATE INDEX IF NOT EXISTS imdb_ids ON files(kind,imdb);
                CREATE INDEX IF NOT EXISTS title_year ON files(kind,title,year);
                CREATE TABLE IF NOT EXISTS sources (
                    path TEXT NOT NULL REFERENCES files(path), source TEXT NOT NULL,
                    PRIMARY KEY(path,source));
                CREATE INDEX IF NOT EXISTS source_ids ON sources(source);
                CREATE TABLE IF NOT EXISTS discovery_roots (
                    root TEXT PRIMARY KEY, context TEXT NOT NULL, completed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS generation_entries (
                    kind TEXT NOT NULL, source TEXT NOT NULL, signature TEXT NOT NULL,
                    path TEXT NOT NULL, PRIMARY KEY(kind,source));
                CREATE INDEX IF NOT EXISTS generation_paths ON generation_entries(path);
                CREATE TABLE IF NOT EXISTS generation_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                PRAGMA user_version=3;
                COMMIT;
            """)
        self.db.execute("CREATE TEMP TABLE live(source TEXT PRIMARY KEY)")
        self.db.execute(
            "CREATE TEMP TABLE live_series(series_key TEXT, account TEXT, PRIMARY KEY(series_key,account))"
        )
        self.db.execute(
            "CREATE TEMP TABLE catalogue(uuid TEXT, stream TEXT, identity TEXT, source TEXT, season INTEGER, episode INTEGER)"
        )
        self.db.execute("CREATE INDEX temp.catalogue_uuid ON catalogue(uuid,stream)")
        if not self.db.execute(
            "SELECT 1 FROM generation_state WHERE key='absolute_paths'",
        ).fetchone():
            # One-time compatibility for decisions written before path normalization.
            cursor = self.db.execute("SELECT kind,source,path FROM generation_entries WHERE path<>''")
            while True:
                rows = cursor.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                updates = [(os.path.abspath(path), kind, source) for kind, source, path in rows
                           if not os.path.isabs(path)]
                if updates:
                    with self.db:
                        self.db.executemany(
                            'UPDATE generation_entries SET path=? WHERE kind=? AND source=?', updates,
                        )
            with self.db:
                self.db.execute("INSERT INTO generation_state VALUES ('absolute_paths','1')")

    def close(self):
        self.db.close()

    def discovery_complete(self, root, context):
        return bool(self.db.execute(
            "SELECT 1 FROM discovery_roots WHERE root=? AND context=?", (root, context),
        ).fetchone())

    def invalidate_discovery(self, root):
        with self.db:
            self.db.execute("DELETE FROM discovery_roots WHERE root=?", (root,))

    def mark_discovered(self, root, context):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO discovery_roots VALUES (?,?,?)",
                (root, context, time.time()),
            )

    def record(self, path, identity, source="", position=None, nfos=None):
        self.record_many([(path, identity, source, position, nfos)])

    def record_many(self, records):
        if not records:
            return
        if len(records) > BATCH_SIZE:
            raise ValueError(
                f"Inventory batches must contain at most {BATCH_SIZE} records"
            )
        paths = [os.path.abspath(r[0]) for r in records]
        existing = {}
        for offset in range(0, len(paths), LOOKUP_BATCH_SIZE):
            group = paths[offset : offset + LOOKUP_BATCH_SIZE]
            placeholders = ",".join("?" for _ in group)
            existing.update(
                {
                    r["path"]: json.loads(r["nfos"])
                    for r in self.db.execute(
                        f"SELECT path,nfos FROM files WHERE path IN ({placeholders})",
                        group,
                    )
                }
            )
        files, sources = [], []
        for path, (original, identity, source, position, nfos) in zip(paths, records):
            url = strm_contents(path)
            if url is None:
                continue
            hashes = existing.setdefault(path, {})
            hashes.update({os.path.abspath(p): h for p, h in (nfos or {}).items() if h})
            files.append(
                (
                    path,
                    identity.kind,
                    identity.title,
                    str(identity.year) if identity.year else None,
                    identity.tmdb,
                    identity.imdb,
                    *(position or (None, None)),
                    url,
                    json.dumps(hashes),
                )
            )
            if source:
                sources.append((path, source))
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)", files
            )
            self.db.executemany("INSERT OR IGNORE INTO sources VALUES (?,?)", sources)

    def row_batches(self, batch=BATCH_SIZE, skip_filters=False):
        sql = 'SELECT f.* FROM files f WHERE f.path>?'
        if skip_filters:
            sql += ' AND NOT EXISTS (SELECT 1 FROM filter_handled h WHERE h.path=f.path)'
        sql += ' ORDER BY f.path LIMIT ?'
        last_path = ''
        while True:
            rows = self.db.execute(sql, (last_path, batch)).fetchall()
            if not rows:
                break
            last_path = rows[-1]['path']
            yield rows

    def rows(self, batch=BATCH_SIZE, skip_filters=False):
        for rows in self.row_batches(batch, skip_filters):
            yield from rows

    @contextmanager
    def forget_batch(self):
        """Commit one bounded group of removals; rolled-back paths remain retryable."""
        if getattr(self, '_forget_batch_active', False):
            raise RuntimeError('Inventory removal batches cannot nest')
        self._forget_batch_active = True
        self._cleanup_folders = {}
        try:
            with self.db:
                yield
                for folder, (roots, stats) in sorted(
                        self._cleanup_folders.items(), key=lambda item: len(item[0]), reverse=True):
                    self._remove_empty_parents(folder, roots, stats)
        finally:
            self._forget_batch_active = False
            self._cleanup_folders = {}

    def forget(self, path):
        if getattr(self, '_forget_batch_active', False):
            self._forget(path)
        else:
            with self.db:
                self._forget(path)

    def _forget(self, path):
        self.db.execute("DELETE FROM generation_entries WHERE path=?", (path,))
        self.db.execute("DELETE FROM sources WHERE path=?", (path,))
        self.db.execute("DELETE FROM files WHERE path=?", (path,))

    def absent(self, path):
        # Unknown legacy references cannot establish absence.
        return (
            bool(
                self.db.execute(
                    "SELECT 1 FROM sources WHERE path=?", (path,)
                ).fetchone()
            )
            and not self.db.execute(
                "SELECT 1 FROM sources s JOIN live l ON s.source=l.source WHERE s.path=?",
                (path,),
            ).fetchone()
        )

    def delete(self, row, roots, include_nfo=False, dry_run=False, stats=None):
        outcome = remove_strm(row, roots, dry_run)
        return self.finish_delete(row, roots, outcome, include_nfo, dry_run, stats)

    def finish_delete(self, row, roots, outcome, include_nfo=False, dry_run=False, stats=None):
        """Finalize file-only worker results on the inventory's owning thread."""
        if outcome not in ('preserved', 'candidate', 'missing', 'deleted'):
            raise ValueError('Invalid STRM removal result')
        if dry_run or outcome in ('preserved', 'candidate'):
            return outcome
        path = row['path']
        # Retain the record if an NFO deletion fails, so the next run can retry.
        if include_nfo:
            for nfo, digest in json.loads(row["nfos"]).items():
                if (
                    contained(nfo, roots)
                    and not os.path.islink(nfo)
                    and file_hash(nfo) == digest
                ):
                    # Shared tvshow.nfo stays while any episode STRM remains.
                    if os.path.basename(nfo) == "tvshow.nfo" and any(
                        Path(nfo).parent.rglob("*.strm")
                    ):
                        continue
                    os.remove(nfo)
                    if stats is not None:
                        stats["deleted_nfo"] = stats.get("deleted_nfo", 0) + 1
        self.forget(path)
        folder = str(Path(path).parent)
        if getattr(self, '_forget_batch_active', False):
            self._cleanup_folders[folder] = (roots, stats)
        else:
            self._remove_empty_parents(folder, roots, stats)
        return outcome

    def _remove_empty_parents(self, folder, roots, stats):
        folder = Path(folder)
        while contained(str(folder), roots):
            try:
                folder.rmdir()
                if stats is not None:
                    stats["removed_dirs"] = stats.get("removed_dirs", 0) + 1
            except OSError:
                break
            folder = folder.parent
