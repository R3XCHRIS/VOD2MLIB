"""Adoption: take over a library that classic mode already generated
by reading the files themselves.

Every .strm holds a Dispatcharr proxy URL, /proxy/vod/<movie|episode>/<uuid>
optionally followed by ?stream_id=<id>, which says exactly which title and
which copy it plays, whatever naming settings (or older naming versions)
produced the file. So adoption walks the Movies and Series roots and reads
the .strm files instead of guessing paths.

scan() only reads; adopt() records what scan() found. Nothing is ever
deleted here: a title found in more than one place (two copies, or two
folders) is flagged 'duplicates', which makes it pending, and Apply then
rewrites it and removes the extra files. Unrecognised files are never
recorded, so they can never be deleted.
"""
import os
import re

from .apply import root_for
from .catalogue import copy_key
from .upkeep import _ranked_copies

_PROXY_URL_RE = re.compile(
    r"/proxy/vod/(movie|episode)/([0-9a-fA-F-]{36})(?:/[^?\s]*)?(?:\?(?:[^\s#]*&)?stream_id=([^&\s#]+))?")
MAX_STRM_BYTES = 4096
SAMPLE = 20


def parse_strm(text):
    """(url_kind, uuid, stream_id | None) from a .strm's contents, or None."""
    m = _PROXY_URL_RE.search(text or "")
    return (m.group(1), m.group(2).lower(), m.group(3)) if m else None


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(MAX_STRM_BYTES)
    except OSError:
        return None


def _companions(strm_path, url_kind):
    """The .nfo next to a .strm, and a series' tvshow.nfo (show folder =
    parent of the Season folder)."""
    out = [strm_path[: -len(".strm")] + ".nfo"]
    if url_kind == "episode":
        out.append(os.path.join(os.path.dirname(os.path.dirname(strm_path)), "tvshow.nfo"))
    return [p for p in out if os.path.isfile(p)]


def _place(strm_path, kind):
    """The folder that identifies one on-disk instance of a title."""
    folder = os.path.dirname(strm_path)
    return folder if kind == "movie" else os.path.dirname(folder)


def scan(catalogue, store, settings, progress=None):
    """Read-only. Returns a plan: per title the files found and copies seen,
    plus counts and samples of what can't be adopted."""
    strms = []
    for kind in ("movie", "series"):
        root = root_for(settings, kind)
        if os.path.isdir(root):
            for dirpath, _dirs, files in os.walk(root):
                strms.extend(os.path.join(dirpath, f) for f in files if f.endswith(".strm"))
    titles, unknown, not_ours, cache = {}, [], [], {}
    for i, path in enumerate(sorted(strms), 1):
        parsed = parse_strm(_read(path))
        if parsed is None:
            not_ours.append(path)
        else:
            if parsed not in cache:
                cache[parsed] = catalogue.identify_strm(*parsed)
            found = cache[parsed]
            if found is None:
                unknown.append(path)
            else:
                key = (found["kind"], found["uuid"])
                t = titles.setdefault(key, {"kind": found["kind"], "uuid": found["uuid"], "name": found["name"],
                                            "files": set(), "copies": set(), "places": set()})
                t["files"].update([path] + _companions(path, parsed[0]))
                if found["copy"]:
                    t["copies"].add(found["copy"])
                t["places"].add(_place(path, found["kind"]))
        if progress and (i % 50 == 0 or i == len(strms)):
            progress(i, len(strms))

    out = []
    for t in titles.values():
        t["known"] = store.is_known(t["kind"], t["uuid"])
        t["duplicates"] = len(t["copies"]) > 1 or len(t["places"]) > 1
        out.append(t)
    out.sort(key=lambda t: (t["kind"], t["name"].lower()))
    new = [t for t in out if not t["known"]]
    return {
        "titles": out,
        "counts": {
            "strm_files": len(strms),
            "movies": sum(t["kind"] == "movie" for t in new),
            "series": sum(t["kind"] == "series" for t in new),
            "duplicates": sum(t["duplicates"] for t in new),
            "already_known": len(out) - len(new),
            "unknown_content": len(unknown),
            "not_dispatcharr": len(not_ours),
        },
        "samples": {"unknown_content": unknown[:SAMPLE], "not_dispatcharr": not_ours[:SAMPLE]},
    }


def adopt(catalogue, store, plan, logger):
    """Record every new title of a scan() plan; returns counts."""
    prefs = store.prefs()
    adopted = duplicates = skipped = 0
    problems = []
    for t in plan["titles"]:
        if t["known"]:
            continue
        ranked = _ranked_copies(catalogue, store, t["kind"], t["uuid"], prefs)
        if not ranked:
            skipped += 1
            problems.append(f"{t['name']}: no copy is available from any provider now; left alone")
            continue
        # The best of the copies the files actually play; any copy if the
        # files don't say (URLs written without ?stream_id=).
        seen = [(title, rel) for title, rel in ranked if copy_key(t["kind"], rel) in t["copies"]]
        _title, rel = (seen or ranked)[0]
        account_id, copy_id = copy_key(t["kind"], rel)
        detail = None
        if t["duplicates"]:
            detail = (f"Found in {len(t['places'])} place(s) with {len(t['copies']) or 1} copy/copies on disk; "
                      "Apply keeps the chosen copy and removes the other files.")
        if store.adopt_title(t["kind"], t["uuid"], t["name"], account_id, copy_id, sorted(t["files"]), detail):
            adopted += 1
            duplicates += bool(detail)
    store.set_adopted()
    message = f"Adopted {adopted} title{'' if adopted == 1 else 's'}"
    if duplicates:
        message += f"; {duplicates} found more than once are pending (Apply removes the extra files)"
    if skipped:
        message += f"; {skipped} skipped (no copy available)"
    logger.info(message)
    return {"adopted": adopted, "duplicates": duplicates, "skipped": skipped,
            "message": message, "problems": problems[:50]}


def summary(plan):
    """The plan without the per-title file lists, for the page."""
    return {
        "counts": plan["counts"],
        "samples": plan["samples"],
        "titles": [{"kind": t["kind"], "name": t["name"], "files": len(t["files"]),
                    "duplicates": t["duplicates"], "known": t["known"]}
                   for t in plan["titles"] if not t["known"]][:500],
    }
