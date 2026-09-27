"""Apply: make the disk match the desired selection.

Reuses the existing Plugin helpers for folder naming, proxy URLs, NFO content
and no-op-aware writes, so selected titles look exactly like titles the
classic Generate action produces.

Only files recorded in `applied_file` are ever deleted. A change to a title
that stays selected (copy change, season exclusion) writes the new file set
first and then deletes the recorded files it no longer contains, so files
that don't change are left alone and a failed write deletes nothing.
"""
import copy
import os
import uuid as uuidlib


def _remove_title_files(store, kind, content_uuid, root):
    """Delete every file Apply wrote for this title, then prune folders
    that end up empty (never the root itself, never a folder that still
    holds anything else)."""
    removed = 0
    for path in store.files_for(kind, content_uuid):
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        _prune_empty_dirs(os.path.dirname(path), root)
    store.forget_files(kind, content_uuid)
    return removed


def _remove_paths(store, paths, root):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        _prune_empty_dirs(os.path.dirname(path), root)
    store.forget_paths(paths)


def _claim(store, kind, content_uuid, path, owned, adopted=False):
    """Claim a path before writing it (raises if another title owns it).
    Paths this title already owns need no query, so upkeep costs none."""
    if path not in owned:
        store.claim(kind, content_uuid, path, adopted)


def _check_strm_target(store, kind, title_uuid, path, owned, url_kind, content_uuid):
    """True when an existing .strm Apply doesn't own is taken over: it links
    to this same content (a classic-mode file, as Scan library would adopt
    it). Any other file in the way is left alone and the title fails."""
    if path in owned or not os.path.exists(path):
        return False
    store.check_free(kind, title_uuid, path)  # another title's file: say whose
    from .adopt import _read, parse_strm  # adopt imports this module
    parsed = parse_strm(_read(path))
    if parsed and parsed[0] == url_kind and parsed[1] == str(content_uuid).lower():
        return True
    raise FileExistsError(f"{os.path.basename(path)} already exists and isn't a Dispatcharr link to "
                          "this title: move it, or use Scan library to adopt it")


def _write_nfo(plugin, store, kind, content_uuid, path, owned, adopted, make_content, written,
               take_over=False):
    """Existing .nfo files may hold user edits: never overwrite one Apply
    didn't create, adopted ones included. An existing one is claimed (as
    adopted) only beside a .strm that was taken over, as adoption would."""
    if path in owned:
        if path in adopted and os.path.exists(path):
            pass  # adopted: may hold the user's edits
        else:
            if path in adopted:
                store.unadopt(path)  # it went missing: ours again
            plugin._write_if_different_preserve_times(path, make_content())
    elif os.path.exists(path):
        if not take_over:
            return  # someone else's .nfo: not ours, never touched
        store.claim(kind, content_uuid, path, adopted=True)
    else:
        store.claim(kind, content_uuid, path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(make_content())
    written.add(path)


def _prune_empty_dirs(start, root):
    root_real = os.path.realpath(root)
    current = os.path.realpath(start)
    while current.startswith(root_real + os.sep) and current != root_real:
        try:
            os.rmdir(current)  # fails if not empty, which is exactly the stop signal
        except OSError:
            return
        current = os.path.dirname(current)


def _write_movie(plugin, store, settings, movie, relation, owned=frozenset()):
    """Write the movie's files; returns the set of paths it claimed."""
    written = set()
    root = root_for(settings, "movie")
    dispatcharr_url = (settings.get("dispatcharr_url") or "").rstrip("/")
    cat_name = relation.category.name if relation.category else ""
    folder, strm_name, _clean, _year = plugin._movie_target_paths(
        movie, root, cat_name,
        bool(settings.get("nest_movies_by_category", False)),
        bool(settings.get("append_tmdb_id_to_folder", False)),
        (settings.get("tmdb_tag_format") or "plex").strip().lower(),
    )
    os.makedirs(folder, exist_ok=True)
    strm_path = os.path.join(folder, strm_name)
    url = plugin._build_proxy_url(
        dispatcharr_url, "movie", movie.uuid, relation.stream_id,
        bool(settings.get("omit_stream_id", False)),
    )
    took_over = _check_strm_target(store, "movie", movie.uuid, strm_path, owned, "movie", movie.uuid)
    _claim(store, "movie", movie.uuid, strm_path, owned, adopted=took_over)
    plugin._write_if_different_preserve_times(strm_path, url)
    written.add(strm_path)

    if settings.get("generate_nfo", True):
        nfo_path = strm_path[: -len(".strm")] + ".nfo"
        omit_title = bool(settings.get("nfo_omit_title", False))
        adopted = store.adopted_paths("movie", movie.uuid) if owned else set()
        _write_nfo(plugin, store, "movie", movie.uuid, nfo_path, owned, adopted,
                   lambda: plugin._generate_nfo(movie, cat_name, omit_title), written, took_over)
    return written


def _write_series(plugin, store, settings, series, relation, episodes, owned=frozenset()):
    """Write tvshow.nfo and the given episodes of the chosen copy, named
    exactly as classic mode names them; returns the set of paths claimed."""
    written = set()
    root = root_for(settings, "series")
    dispatcharr_url = (settings.get("dispatcharr_url") or "").rstrip("/")
    omit_stream_id = bool(settings.get("omit_stream_id", False))
    generate_nfo = settings.get("generate_series_nfo", True)
    cat_name = relation.category.name if relation.category else ""
    folder, series_name, _year = plugin._series_target_folder(
        series, root, cat_name,
        bool(settings.get("nest_series_by_category", False)),
        bool(settings.get("append_tmdb_id_to_folder", False)),
        (settings.get("tmdb_tag_format") or "plex").strip().lower(),
    )
    os.makedirs(folder, exist_ok=True)
    adopted = store.adopted_paths("series", series.uuid) if owned and generate_nfo else set()
    tvshow_nfo = os.path.join(folder, "tvshow.nfo")
    if generate_nfo and tvshow_nfo not in owned:
        # Two series whose names come out the same share this folder: fail
        # before writing any episode into it.
        store.check_free("series", series.uuid, tvshow_nfo)
    took_over_any = False
    for episode, stream_id, title in episodes:
        if title and title != episode.name:
            # Name files and NFO from the chosen copy's title; the copy is
            # never saved, so the shared Episode row is untouched.
            episode = copy.copy(episode)
            episode.name = title
        season_name, filename = plugin._episode_target_names(series_name, episode)
        season_folder = os.path.join(folder, season_name)
        os.makedirs(season_folder, exist_ok=True)
        strm_path = os.path.join(season_folder, filename + ".strm")
        url = plugin._build_proxy_url(dispatcharr_url, "episode", episode.uuid, stream_id, omit_stream_id)
        took_over = _check_strm_target(store, "series", series.uuid, strm_path, owned, "episode", episode.uuid)
        took_over_any = took_over_any or took_over
        _claim(store, "series", series.uuid, strm_path, owned, adopted=took_over)
        plugin._write_if_different_preserve_times(strm_path, url)
        written.add(strm_path)
        if generate_nfo:
            _write_nfo(plugin, store, "series", series.uuid,
                       os.path.join(season_folder, filename + ".nfo"), owned, adopted,
                       lambda episode=episode: plugin._generate_episode_nfo(episode), written, took_over)
    if generate_nfo:
        # After the episodes, so a classic show folder they took over hands
        # its tvshow.nfo over too.
        omit_title = bool(settings.get("nfo_omit_title", False))
        _write_nfo(plugin, store, "series", series.uuid, tvshow_nfo, owned, adopted,
                   lambda: plugin._generate_tvshow_nfo(series, cat_name, omit_title), written, took_over_any)
    return written


def _season(episode):
    return episode.season_number or 0


def season_counts(episodes):
    """[{season, episodes}] in season order, for the page's checklist."""
    counts = {}
    for episode, _stream_id, _title in episodes:
        counts[_season(episode)] = counts.get(_season(episode), 0) + 1
    return [{"season": n, "episodes": counts[n]} for n in sorted(counts)]


_ROOTS = {
    "movie": ("root_folder", "/VODS/Movies", "Movies"),
    "series": ("series_root_folder", "/VODS/Series", "Series"),
}


def root_for(settings, kind):
    key, default, _label = _ROOTS[kind]
    return settings.get(key) or default


def root_label(kind):
    return _ROOTS[kind][2]


def root_problem(root):
    """Why Apply can't write under `root`, or None if it can.

    A missing root is fine as long as its nearest existing parent is
    writable, because Apply creates it."""
    who = f"uid {os.getuid()}" if hasattr(os, "getuid") else "the Dispatcharr user"
    path = root
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    if not os.path.isdir(path):
        return f"{root} can't be created: {path} is a file, not a folder."
    if not os.access(path, os.W_OK | os.X_OK):
        where = f"{root} is" if path == root else f"{root} can't be created: {path} is"
        return f"{where} not writable by {who}. Fix the folder's owner or permissions on the host."
    return None


def apply_pending(plugin, catalogue, store, settings, logger, progress=None):
    """Apply every pending change. Returns a summary dict.

    `progress(done, total)` is called after each title, if given. Holds the
    disk lease so scheduled upkeep (another process) doesn't write meanwhile.
    """
    holder = f"apply-{uuidlib.uuid4().hex[:8]}"
    if not store.acquire_disk_lease(holder):
        return {"status": "error",
                "message": "Scheduled upkeep is updating files right now; try Apply again in a few minutes."}
    try:
        return _apply_pending(plugin, catalogue, store, settings, logger, progress)
    finally:
        store.release_disk_lease(holder)


def _apply_pending(plugin, catalogue, store, settings, logger, progress):
    dispatcharr_url = (settings.get("dispatcharr_url") or "").rstrip("/")
    ok, err = plugin._validate_dispatcharr_url(dispatcharr_url, logger)
    if not ok:
        return {"status": "error", "message": err}

    # Removals first, so a title taking over paths another title is giving up
    # in the same Apply finds them free.
    pending = sorted(store.pending(), key=lambda row: bool(row["desired_selected"]))
    # Only the roots of kinds with pending changes must be writable, so a
    # broken Series folder doesn't block movie changes.
    roots = {}
    for kind in _ROOTS:
        if not any(row["kind"] == kind for row in pending):
            continue
        root = root_for(settings, kind)
        problem = root_problem(root)
        if problem is None:
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as e:
                problem = f"{root} can't be created: {e.strerror or e}."
        if problem:
            return {"status": "error", "message": f"{root_label(kind)} root folder: {problem}"}
        roots[kind] = root

    added = removed = changed = errors = 0
    failures = []
    for i, row in enumerate(pending, 1):
        kind = row["kind"]
        uuid = row["content_uuid"]
        title = row.get("title") or uuid
        try:
            was_selected = bool(row["applied_selected"])
            want_selected = bool(row["desired_selected"])
            item = relation = episodes = None
            if want_selected:
                item, relation = catalogue.resolve_copy(
                    kind, uuid, row["desired_account_id"], row["desired_stream_id"])
                if item is None:
                    raise LookupError("selected copy is no longer available from the provider")
                # Fetched before anything is removed, so a failed fetch
                # during a copy change leaves the old files in place.
                if kind == "series":
                    episodes = catalogue.series_episodes(relation)
                    if not episodes:
                        raise LookupError("the provider lists no episodes for this copy")
                    excluded = set(store.excluded_seasons(row, "desired"))
                    episodes = [e for e in episodes if _season(e[0]) not in excluded]
                    if not episodes:
                        raise ValueError("every season is excluded; unselect the series instead")
            if want_selected:
                # Write the new set, then delete what it no longer contains
                # (another copy's names, excluded seasons).
                old = set(store.files_for(kind, uuid))
                if kind == "movie":
                    written = _write_movie(plugin, store, settings, item, relation, old)
                else:
                    written = _write_series(plugin, store, settings, item, relation, episodes, old)
                _remove_paths(store, sorted(old - written), roots[kind])
            elif was_selected:
                _remove_title_files(store, kind, uuid, roots[kind])
            store.mark_applied(kind, uuid, row)
            if want_selected and was_selected:
                changed += 1
            elif want_selected:
                added += 1
            else:
                removed += 1
        except Exception as e:  # keep going; the title stays pending
            errors += 1
            failures.append(f"{title}: {e}")
            store.set_error(kind, uuid, str(e))
            logger.error("Apply failed for %s: %s", title, e)
        if progress:
            progress(i, len(pending))

    message = f"Applied: {added} added, {removed} removed, {changed} changed"
    if errors:
        message += f", {errors} failed"
    logger.info(message)
    return {
        "status": "ok" if not errors else "partial",
        "message": message,
        "added": added, "removed": removed, "changed": changed,
        "errors": errors, "failures": failures[:50],
    }
