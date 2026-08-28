"""TDD for the flush-all bug in VOD2MLIB movie generation (PR1).

Sanity-checked: 2026-08-28. These tests drive the Django-free core
`_generate_movies_from_relations` (extracted from `_generate_movies`) so the
grouping + flush logic can be exercised without a live Dispatcharr DB.

The bug under test: when the relation iterator is exhausted WITHOUT hitting the
batch_size limit, the original loop only flushed the LAST pending movie group,
silently dropping every other grouped movie. A full run therefore wrote ~1 file
instead of N. The fix flushes ALL pending groups on loop exhaustion (and on the
batch break, which already did).
"""
import logging
import os

import pytest

from plugin import Plugin


# A real logger carries info/warning/error/debug so any method can log freely.
def _logger():
    log = logging.getLogger("tdd_flush")
    log.setLevel(logging.DEBUG)
    return log


class _Cat:
    def __init__(self, name):
        self.name = name


class _Movie:
    def __init__(self, mid, name, uid, year=None, tmdb_id=""):
        self.id = mid
        self.name = name
        self.uuid = uid
        self.year = year
        self.tmdb_id = tmdb_id
        self.description = ""
        self.imdb_id = ""
        self.rating = ""
        self.genre = ""


class _Rel:
    def __init__(self, rid, movie, cat, sid):
        self.id = rid
        self.movie = movie
        self.category = cat
        self.stream_id = sid


def _base_settings(tmp_path, **over):
    s = {
        "root_folder": str(tmp_path),
        "dispatcharr_url": "http://example:9191",
        "batch_size": "all",
        "generate_nfo": False,
        "version_format": "",
        "ffprobe_path": "ffprobe",
        "nest_movies_by_category": False,
        "dedupe_movies_across_categories": False,
        "append_tmdb_id_to_folder": False,
        "tmdb_tag_format": "plex",
        "omit_stream_id": False,
        "category_filter": "",
        "category_exclude": "",
        "nfo_omit_title": False,
    }
    s.update(over)
    return s


@pytest.fixture
def p():
    return Plugin()


# ---------------------------------------------------------------------------
# RED target: exhausting the iterator must flush EVERY pending group, not just
# the last one.
# ---------------------------------------------------------------------------
class TestFlushAllPendingOnExhaustion:
    def test_three_distinct_movies_all_written(self, p, tmp_path, monkeypatch):
        monkeypatch.setattr(p, "_detect_variant", lambda *a, **k: "DUB")
        monkeypatch.setattr(p, "_detect_resolution", lambda *a, **k: "720p")

        rels = [
            _Rel(1, _Movie(1, "Alpha (2020)", "u1", 2020), _Cat("FILM"), 11),
            _Rel(2, _Movie(2, "Beta (2021)", "u2", 2021), _Cat("FILM"), 22),
            _Rel(3, _Movie(3, "Gamma (2019)", "u3", 2019), _Cat("FILM"), 33),
        ]
        settings = _base_settings(tmp_path)
        result = p._generate_movies_from_relations(
            iter(rels), settings, _logger(),
            dispatcharr_url="http://example:9191",
            root_folder=str(tmp_path),
            batch_size="all", target_batch=3,
            version_format="", ffprobe_path="ffprobe",
            nest_by_cat=False, append_tmdb_id=False, tmdb_tag_format="plex",
            generate_nfo=False, nfo_omit_title=False, omit_stream_id=False,
            refresh_existing=False, dedupe_across_cats=False,
            detect_cache={},
        )

        strms = sorted(str(os.path.relpath(f, tmp_path)) for f in tmp_path.rglob("*.strm"))
        assert len(strms) == 3, f"expected 3 .strm files, got {len(strms)}: {strms}"
        assert result["counts"]["created"] == 3, result

    def test_dub_leg_twins_collapse_into_one_folder_two_strm(self, p, tmp_path, monkeypatch):
        # Two Dispatcharr Movies with differently-formatted names but the same
        # clean_base (a DUB and a LEG twin) must land in ONE folder as two
        # .strm files (Jellyfin "Versions"). version_format turns the suffix on.
        # Stub detection so the two twins get DIFFERENT variants (the real
        # collapse case): [DUB] -> DUB, [LEG] -> LEG. The grouping key (norm2)
        # collapses the bracket form so both land in one folder.
        def _fake_variant(name, *a, **k):
            return "LEG" if "[LEG]" in (name or "") else "DUB"
        monkeypatch.setattr(p, "_detect_variant", _fake_variant)
        monkeypatch.setattr(p, "_detect_resolution", lambda *a, **k: "720p")

        rels = [
            _Rel(1, _Movie(1, "SalveRosa [DUB] (2025)", "u1", 2025), _Cat("FILM"), 11),
            _Rel(2, _Movie(2, "SalveRosa [LEG] (2025)", "u2", 2025), _Cat("FILM"), 22),
        ]
        settings = _base_settings(tmp_path, version_format=" - {variant} {res}")
        result = p._generate_movies_from_relations(
            iter(rels), settings, _logger(),
            dispatcharr_url="http://example:9191",
            root_folder=str(tmp_path),
            batch_size="all", target_batch=2,
            version_format=" - {variant} {res}", ffprobe_path="ffprobe",
            nest_by_cat=False, append_tmdb_id=False, tmdb_tag_format="plex",
            generate_nfo=False, nfo_omit_title=False, omit_stream_id=False,
            refresh_existing=False, dedupe_across_cats=False,
            detect_cache={},
        )

        # ONE folder, TWO .strm inside it.
        folders = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(folders) == 1, f"expected 1 folder, got {folders}"
        strms = sorted(f.name for f in folders[0].glob("*.strm"))
        assert strms == [
            "SalveRosa (2025) - DUB 720p.strm",
            "SalveRosa (2025) - LEG 720p.strm",
        ], strms
        assert result["counts"]["created"] == 2, result
