"""Tests for the single shared core: vodlib.naming + vodlib.playback.

This is the one naming path and the one playback path used by BOTH the .strm
generator and the HTTP mount, so it carries the title-cleaning coverage that used
to live against the plugin's (now-removed) private helpers.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vodlib import naming as n
from vodlib import playback


# ---------- parse_title ----------

class TestParseTitle:
    def test_strips_language_pipe_prefix(self):
        assert n.parse_title("EN| The Matrix (1999)")["title"] == "The Matrix"

    def test_strips_quality_language_dash_prefix(self):
        r = n.parse_title("4K-EN - Deadpool (2016)")
        assert r["title"] == "Deadpool" and r["year"] == 2016

    def test_strips_list_number_prefix(self):
        assert n.parse_title("117. Die Hard (1988)")["title"] == "Die Hard"

    def test_truncates_at_first_paren_year(self):
        r = n.parse_title("Cool Hand Luke 4K (1967) PAUL NEWMAN (1967)")
        assert r["title"] == "Cool Hand Luke" and r["year"] == 1967

    def test_prefers_year_field_over_title(self):
        assert n.parse_title("Some Movie", 1995)["year"] == 1995

    def test_extracts_bare_year_when_no_field(self):
        r = n.parse_title("Wicked For Good 2025")
        assert r["year"] == 2025 and r["title"] == "Wicked For Good"

    def test_preserves_year_that_is_part_of_title(self):
        # Blade Runner 2049 — bare year before any paren is part of the title.
        assert n.parse_title("Blade Runner 2049", 2017)["title"].startswith("Blade Runner 2049")

    def test_year_as_whole_title_kept(self):
        r = n.parse_title("1984", 1984)
        assert r["title"] == "1984"

    def test_strips_inline_quality_tokens(self):
        assert n.parse_title("Whiplash 1080p HEVC (2014)")["title"] == "Whiplash"

    def test_does_not_overstrip_real_words(self):
        # MAX/HBO/CAM are excluded from inline stripping.
        assert n.parse_title("Mad Max Fury Road (2015)")["title"] == "Mad Max Fury Road"

    def test_dotted_release_name(self):
        assert n.parse_title("The.Matrix.1999")["title"] == "The Matrix"

    def test_empty(self):
        assert n.parse_title("")["title"] == ""


# ---------- folder_name ----------

class TestFolderName:
    def test_plain(self):
        assert n.folder_name("Aladdin", 2019) == "Aladdin (2019)"

    def test_with_tmdb(self):
        assert n.folder_name("Avatar", 2009, "19995") == "Avatar (2009) {tmdb-19995}"

    def test_with_tmdb_and_imdb(self):
        assert n.folder_name("Avatar", 2009, "19995", "tt0499549") == \
            "Avatar (2009) {tmdb-19995} {imdb-tt0499549}"

    def test_imdb_gets_tt_prefix(self):
        assert n.folder_name("X", 2000, None, "0499549").endswith("{imdb-tt0499549}")

    def test_dirty_name_cleaned(self):
        assert n.folder_name("4K-EN - Deadpool & Wolverine (2024)", None, "533535") == \
            "Deadpool & Wolverine (2024) {tmdb-533535}"

    def test_no_year(self):
        assert n.folder_name("Untitled") == "Untitled"


# ---------- file names ----------

class TestFileNames:
    def test_strm_matches_folder(self):
        f = n.folder_name("Avatar", 2009, "19995")
        assert n.strm_filename(f) == "Avatar (2009) {tmdb-19995}.strm"

    def test_episode_basename(self):
        assert n.episode_basename("'Allo 'Allo! (1984)", 1984, 1, 2, "S01E02 - The Wine") == \
            "'Allo 'Allo! (1984) - S01E02 - The Wine"

    def test_episode_basename_no_title(self):
        assert n.episode_basename("Show", 2000, 3, 4, "") == "Show (2000) - S03E04"

    def test_season_dir(self):
        assert n.season_dir_name(1) == "Season 01"
        assert n.season_dir_name(12) == "Season 12"
        assert n.season_dir_name(None) == "Season 01"

    def test_episode_display_title(self):
        assert n.episode_display_title("A+ - Berlin ER (2025) (DE) - S01E01 - Symptoms") == "Symptoms"


# ---------- external ids ----------

class TestExternalIds:
    def test_none(self):
        assert n.format_external_ids() == ""

    def test_tmdb_only(self):
        assert n.format_external_ids("378") == " {tmdb-378}"

    def test_imdb_adds_tt(self):
        assert n.format_external_ids(None, "1234") == " {imdb-tt1234}"

    def test_imdb_keeps_tt(self):
        assert n.format_external_ids(None, "tt1234") == " {imdb-tt1234}"


# ---------- sanitize ----------

class TestSanitize:
    def test_illegal_to_space(self):
        assert n.sanitize_filename('a/b:c*d') == "a b c d"

    def test_traversal_guard(self):
        assert n.sanitize_filename("..") == "Unknown"
        assert n.sanitize_filename(".") == "Unknown"

    def test_empty(self):
        assert n.sanitize_filename("") == "Unknown"
        assert n.sanitize_filename(None) == "Unknown"

    def test_preserves_normal(self):
        assert n.sanitize_filename("Aladdin (2019)") == "Aladdin (2019)"


# ---------- category segment ----------

class TestCategorySegment:
    def test_nest_off(self):
        assert n.category_segment("Action", False) == ""

    def test_nest_on(self):
        assert n.category_segment("Action", True) == "Action"

    def test_nest_on_empty(self):
        assert n.category_segment("", True) == "Unassigned"


# ---------- playback ----------

class TestPlayback:
    def test_movie_url(self):
        assert playback.proxy_url("http://x:9191", "movie", "uuid-1", "680339") == \
            "http://x:9191/proxy/vod/movie/uuid-1?stream_id=680339"

    def test_episode_url(self):
        assert playback.proxy_url("http://x:9191/", "episode", "u", "5").endswith(
            "/proxy/vod/episode/u?stream_id=5")

    def test_strips_trailing_slash(self):
        assert "9191//proxy" not in playback.proxy_url("http://x:9191/", "movie", "u", "1")

    def test_bad_content_type(self):
        try:
            playback.proxy_url("http://x", "channel", "u", "1")
            assert False
        except ValueError:
            pass

    def test_validate_rejects_empty(self):
        assert playback.validate_base_url("") is not None

    def test_validate_rejects_non_http(self):
        assert playback.validate_base_url("ftp://x") is not None

    def test_validate_accepts_lan(self):
        assert playback.validate_base_url("http://192.168.1.10:9191") is None
