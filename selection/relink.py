"""Find selected titles again after Dispatcharr re-creates its content.

A title is known by Dispatcharr's content uuid. When a provider refresh comes
back empty, Dispatcharr deletes every movie as orphaned and the next refresh
imports them again with new uuids. Each selection row
is then moved to the new uuid: first by its copy (account + stream id, or
series id), which is exact, then by TMDB id when exactly one title has it.
"""

from .catalogue import KINDS


def titles_missing(catalogue, store):
    """True if a row worth keeping points at a title Dispatcharr no longer
    has. Reads only; callers take the disk lease just when this is true."""
    for kind in KINDS:
        uuids = {r["content_uuid"] for r in store.relink_candidates(kind)}
        if uuids and len(catalogue.title_ids(kind, list(uuids))) < len(uuids):
            return True
    return False


def relink_missing(catalogue, store, logger=None):
    """Move rows whose title vanished to the title's new uuid. Also records
    each existing title's TMDB id, the fallback for next time. Returns the
    number of titles moved."""
    moved = 0
    for kind in KINDS:
        rows = store.relink_candidates(kind)
        if not rows:
            continue
        present = catalogue.title_ids(kind, [r["content_uuid"] for r in rows])
        store.set_tmdb_ids(kind, {u: t for u, t in present.items() if _usable_tmdb(t)})
        for row in rows:
            old = row["content_uuid"]
            if old in present:
                continue
            new = _find(catalogue, kind, row)
            if new and new != old:
                store.move_title(kind, old, new)
                moved += 1
                if logger:
                    logger.info("Relinked %s %s: Dispatcharr id %s -> %s", kind, row.get("title"), old, new)
    return moved


def _usable_tmdb(value):
    return bool(value) and str(value).strip() not in ("0", "None")


def _find(catalogue, kind, row):
    for account_id, copy_id in ((row.get("applied_account_id"), row.get("applied_stream_id")),
                                (row.get("desired_account_id"), row.get("desired_stream_id"))):
        if account_id is not None and copy_id is not None:
            found = catalogue.find_by_copy(kind, account_id, copy_id)
            if found:
                return found
    if _usable_tmdb(row.get("tmdb_id")):
        found = catalogue.find_by_tmdb(kind, row["tmdb_id"])
        if len(found) == 1:  # ambiguous: leave it to the user
            return found[0]
    return None
