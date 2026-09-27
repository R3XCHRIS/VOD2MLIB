"""Scheduled upkeep in selection mode.

The cron keeps running in selection mode, but only maintains what is
*applied* (on disk); desired-but-not-applied changes are left for Apply.
Per applied title:

  * its copy still resolves: rewrite its files (URL refresh; writes are
    no-ops when nothing changed) and, for a series, write new episodes
    unless their season is excluded. Additive only for series: a provider
    that briefly lists fewer episodes must not delete anything.
  * its copy is gone: fall back to the top-ranked remaining copy (by the
    page's preferences), rewrite, and flag the title 'fallback' for review.
  * no copy left: delete its files, keep it selected, flag it 'no_copy';
    a later run restores it (flag 'fallback') when a copy comes back.
"""
import os
import time
import uuid as uuidlib

from .apply import _remove_paths, _remove_title_files, _season, _write_movie, _write_series
from .apply import root_for, root_label, root_problem
from .catalogue import _copy_dict, copy_key
from .copyinfo import merge_probe, rank, title_prefs
from .relink import relink_missing


def _show_folder(path):
    """A series file's show folder: tvshow.nfo sits in it, episodes one
    level down (Season NN)."""
    parent = os.path.dirname(path)
    return parent if os.path.basename(path) == "tvshow.nfo" else os.path.dirname(parent)


def _renamed_episode_files(kept, written_episode_uuids):
    """The recorded .strm files among `kept` that play an episode written
    this run under another name, plus their .nfo files."""
    from .adopt import _read, parse_strm  # adopt imports this module
    kept = set(kept)
    out = set()
    for path in kept:
        if not path.endswith(".strm"):
            continue
        parsed = parse_strm(_read(path))
        if parsed and parsed[0] == "episode" and parsed[1] in written_episode_uuids:
            out.add(path)
            nfo = path[: -len(".strm")] + ".nfo"
            if nfo in kept:
                out.add(nfo)
    return out


def _ranked_copies(catalogue, store, kind, content_uuid, prefs):
    """[(title, relation)] for the title's available copies, best first."""
    copies = catalogue.title_copies(kind, content_uuid)
    by_key = {copy_key(kind, rel): (title, rel) for title, rel in copies}
    probes = store.probes(kind, list(by_key))
    dicts = []
    for key, (_title, rel) in by_key.items():
        d = _copy_dict(kind, rel)
        d["info"] = merge_probe(d["info"], probes.get(key))
        dicts.append(d)
    return [by_key[(d["account_id"], str(d["stream_id"]))] for d in rank(dicts, prefs)]


def _episodes(catalogue, store, row, relation):
    episodes = catalogue.series_episodes(relation)
    if not episodes:
        raise LookupError("the provider lists no episodes for this copy; nothing was changed")
    excluded = set(store.excluded_seasons(row, "applied"))
    return [e for e in episodes if _season(e[0]) not in excluded]


def _write(plugin, catalogue, store, settings, row, item, relation, owned):
    kind = row["kind"]
    if kind == "movie":
        return _write_movie(plugin, store, settings, item, relation, owned)
    return _write_series(plugin, store, settings, item, relation,
                         _episodes(catalogue, store, row, relation), owned)


def _upkeep_title(plugin, catalogue, store, settings, row, root, prefs, stats):
    kind, content_uuid = row["kind"], row["content_uuid"]
    if row.get("flag") == "duplicates":
        return  # adopted with extra files: the user's Apply tidies it, not upkeep
    item, relation = catalogue.resolve_copy(
        kind, content_uuid, row["applied_account_id"], row["applied_stream_id"])
    old = set(store.files_for(kind, content_uuid))

    if item is not None and row.get("flag") != "no_copy":
        if kind == "movie":
            written = _write(plugin, catalogue, store, settings, row, item, relation, old)
            # One file per movie, so a changed name (e.g. a naming fix) is
            # safe to tidy up here.
            stale = old - written
        else:
            episodes = _episodes(catalogue, store, row, relation)
            written = _write_series(plugin, store, settings, item, relation, episodes, old)
            # Episodes the provider stopped listing stay, but files in a show
            # folder that is no longer produced (the name changed) go, or the
            # media server shows the series twice. So do the old files of an
            # episode just written under another name (a renamed episode).
            folders = {_show_folder(p) for p in written}
            stale = {p for p in old - written if _show_folder(p) not in folders}
            stale |= _renamed_episode_files(old - written - stale, {str(e[0].uuid) for e in episodes})
        _remove_paths(store, sorted(stale), root)
        stats["refreshed"] += 1
        stats["new_files"] += len(written - old)
        return

    candidates = [(item, relation)] if item is not None else \
        _ranked_copies(catalogue, store, kind, content_uuid, title_prefs(prefs, row.get("audio_override")))
    if not candidates:
        if row.get("flag") != "no_copy":
            _remove_title_files(store, kind, content_uuid, root)
            store.set_flag(kind, content_uuid, "no_copy",
                           "No provider has a copy of this title any more; its files were deleted. "
                           "It stays selected and comes back when a copy reappears.")
            stats["no_copy"] += 1
        return

    new_item, new_rel = candidates[0]
    # Written before the old copy's files go, as Apply does for a copy change.
    written = _write(plugin, catalogue, store, settings, row, new_item, new_rel, old)
    _remove_paths(store, sorted(old - written), root)
    account_id, copy_id = copy_key(kind, new_rel)
    store.switch_applied_copy(kind, content_uuid, account_id, copy_id)
    account = getattr(new_rel.m3u_account, "name", "") or f"account {account_id}"
    if row.get("flag") == "no_copy":
        detail = f"A copy is available again ({account}, #{copy_id}); its files were written back."
        stats["restored"] += 1
    else:
        detail = (f"The chosen copy (#{row['applied_stream_id']}) disappeared from the provider; "
                  f"switched to the best remaining copy ({account}, #{copy_id}).")
        stats["fallback"] += 1
    store.set_flag(kind, content_uuid, "fallback", detail)


# Mass-loss guard: when this many applied titles (and this share of them)
# lose every copy in one run, the provider most likely returned too little
# (e.g. an empty movie list). Nothing is deleted then.
GUARD_MIN_TITLES = 5
GUARD_SHARE = 0.2


def _held_titles(catalogue, rows):
    watched = [r for r in rows if r.get("flag") not in ("no_copy", "duplicates")]
    lost = set()
    for kind in {r["kind"] for r in watched}:
        uuids = [r["content_uuid"] for r in watched if r["kind"] == kind]
        present = catalogue.titles_with_copies(kind, uuids)
        lost |= {(kind, u) for u in uuids if str(u) not in present}
    if len(lost) >= GUARD_MIN_TITLES and len(lost) > GUARD_SHARE * len(watched):
        return lost
    return set()


def run_upkeep(plugin, catalogue, store, settings, logger, progress=None):
    """Maintain every applied title. Returns a summary dict (also stored as
    the last upkeep result for the page)."""
    dispatcharr_url = (settings.get("dispatcharr_url") or "").rstrip("/")
    ok, err = plugin._validate_dispatcharr_url(dispatcharr_url, logger)
    if not ok:
        return _finish(store, logger, {"status": "error", "message": err})

    holder = f"upkeep-{uuidlib.uuid4().hex[:8]}"
    if not store.acquire_disk_lease(holder):
        return _finish(store, logger, {"status": "skipped",
                                       "message": "Upkeep skipped: an Apply or another upkeep is writing files"})
    try:
        relinked = relink_missing(catalogue, store, logger)
        rows = store.applied_rows()
        held = _held_titles(catalogue, rows)
        prefs = store.prefs()
        roots, problems = {}, []
        for kind in {r["kind"] for r in rows}:
            root = root_for(settings, kind)
            problem = root_problem(root)
            if problem:
                problems.append(f"{root_label(kind)} root folder: {problem}")
            else:
                roots[kind] = root
        stats = {"refreshed": 0, "new_files": 0, "fallback": 0, "no_copy": 0, "restored": 0, "errors": 0,
                 "relinked": relinked, "held": len(held)}
        failures = list(problems)
        for i, row in enumerate(rows, 1):
            if (row["kind"], row["content_uuid"]) in held:
                pass  # files stay; the next run looks again
            elif row["kind"] in roots:
                try:
                    _upkeep_title(plugin, catalogue, store, settings, row, roots[row["kind"]], prefs, stats)
                except Exception as e:  # keep going; the next run retries
                    stats["errors"] += 1
                    failures.append(f"{row.get('title') or row['content_uuid']}: {e}")
                    logger.error("Upkeep failed for %s: %s", row.get("title"), e)
            if progress:
                progress(i, len(rows))
    finally:
        store.release_disk_lease(holder)

    n, files = stats["refreshed"], stats["new_files"]
    parts = [f"{n} title{'' if n == 1 else 's'} refreshed ({files} new file{'' if files == 1 else 's'})"]
    for key, label in (("relinked", "found again under a new Dispatcharr id"),
                       ("fallback", "switched to another copy"), ("restored", "restored"),
                       ("no_copy", "lost every copy")):
        if stats[key]:
            parts.append(f"{stats[key]} {label}")
    if stats["held"]:
        parts.append(f"{stats['held']} lost every copy at once; nothing deleted "
                     "(the provider probably returned too little; the next run looks again)")
    if stats["errors"] or problems:
        parts.append(f"{stats['errors'] + len(problems)} failed")
    return _finish(store, logger, dict(
        stats, status="ok" if not failures and not stats["held"] else "partial",
        message="Upkeep: " + ", ".join(parts), failures=failures[:50]))


def _finish(store, logger, result):
    result["finished_at"] = time.time()
    (logger.info if result["status"] in ("ok", "skipped") else logger.warning)(result["message"])
    try:
        store.set_last_upkeep(result)
    except Exception:
        logger.exception("Could not record the upkeep result")
    return result
