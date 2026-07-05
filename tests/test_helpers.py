"""Unit tests for VOD2MLIB's pure helper methods.

These methods don't touch Django/DB/filesystem and are safe to test in
isolation. Run with `pytest` from the repo root.
"""
import os
import sys

# Make the repo root importable so `import plugin` resolves to plugin.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import io
import json
import logging
import urllib.error
import urllib.request

import pytest

from plugin import Plugin


class CapturingLogger:
    """A tiny stand-in that records warnings without needing the logging stack."""

    def __init__(self):
        self.warnings = []
        self.errors = []
        self.infos = []

    @staticmethod
    def _render(args):
        if not args:
            return ""
        if len(args) > 1:
            try:
                return args[0] % args[1:]
            except Exception:
                pass
        return args[0]

    def warning(self, *args, **kwargs):
        self.warnings.append(self._render(args))

    def error(self, *args, **kwargs):
        self.errors.append(self._render(args))

    def info(self, *args, **kwargs):
        self.infos.append(self._render(args))


@pytest.fixture
def p():
    return Plugin()


# ---------- _clean_title ----------

class TestCleanTitle:
    def test_strips_two_letter_language_prefix(self, p):
        assert p._clean_title("EN - Inception") == "Inception"

    def test_strips_three_letter_language_prefix(self, p):
        assert p._clean_title("ENG - Inception") == "Inception"

    def test_preserves_AC_130_style_titles(self, p):
        # The whole point of the v1.5 regex tightening
        assert p._clean_title("AC-130") == "AC-130"
        assert p._clean_title("MI-5") == "MI-5"

    def test_no_prefix_unchanged(self, p):
        assert p._clean_title("The Matrix") == "The Matrix"

    def test_empty_input(self, p):
        assert p._clean_title("") == ""
        assert p._clean_title(None) is None

    def test_whitespace_trimmed_after_strip(self, p):
        assert p._clean_title("FR -   Amélie") == "Amélie"


# ---------- _strip_trailing_year ----------

class TestStripTrailingYear:
    def test_strips_year(self, p):
        assert p._strip_trailing_year("Aladdin (2026)") == ("Aladdin", 2026)

    def test_no_year_returns_none(self, p):
        cleaned, year = p._strip_trailing_year("Aladdin")
        assert cleaned == "Aladdin"
        assert year is None

    def test_year_in_middle_not_stripped(self, p):
        # "(2026)" not trailing
        cleaned, year = p._strip_trailing_year("The Year (2026) Movie")
        assert cleaned == "The Year (2026) Movie"
        assert year is None

    def test_extra_trailing_whitespace(self, p):
        assert p._strip_trailing_year("Aladdin (2026)  ") == ("Aladdin", 2026)

    def test_empty_input(self, p):
        cleaned, year = p._strip_trailing_year("")
        assert cleaned == ""
        assert year is None
        cleaned, year = p._strip_trailing_year(None)
        assert cleaned == ""
        assert year is None

    def test_three_digit_year_not_matched(self, p):
        # Regex requires exactly 4 digits
        cleaned, year = p._strip_trailing_year("Old Film (123)")
        assert cleaned == "Old Film (123)"
        assert year is None

    def test_double_year_strips_only_outermost(self, p):
        # A pre-v1.5 folder name that somehow makes it back into a title
        cleaned, year = p._strip_trailing_year("Aladdin (2026) (2026)")
        assert cleaned == "Aladdin (2026)"
        assert year == 2026


# ---------- _sanitize_filename ----------

class TestSanitizeFilename:
    def test_strips_invalid_chars(self, p):
        assert p._sanitize_filename('a<b>c:"d/e\\f|g?h*i') == "abcdefghi"

    def test_strips_control_chars(self, p):
        assert p._sanitize_filename("a\x00b\x1fc") == "abc"

    def test_collapses_runs_of_spaces(self, p):
        assert p._sanitize_filename("a   b   c") == "a b c"

    def test_tabs_stripped_as_control_chars(self, p):
        # Tabs and other \x00-\x1f bytes are stripped BEFORE whitespace collapse.
        # Documenting current behaviour: "a\t\tb" loses its separator.
        assert p._sanitize_filename("a\t\tb") == "ab"

    def test_trims_to_max_length(self, p):
        long = "x" * 500
        result = p._sanitize_filename(long)
        assert len(result) == p.MAX_FILENAME_LEN

    def test_strips_trailing_dots_and_spaces(self, p):
        assert p._sanitize_filename("name. . .") == "name"

    def test_dotdot_becomes_unknown(self, p):
        # Path traversal defense: '..' rstrips to empty, falls back to Unknown
        assert p._sanitize_filename("..") == "Unknown"

    def test_empty_input(self, p):
        assert p._sanitize_filename("") == "Unknown"
        assert p._sanitize_filename(None) == "Unknown"

    def test_normal_movie_name(self, p):
        assert p._sanitize_filename("Aladdin (2026)") == "Aladdin (2026)"


# ---------- _parse_cron ----------

class TestParseCron:
    def test_valid_5_field(self, p):
        assert p._parse_cron("0 3 * * *") == ("0", "3", "*", "*", "*")

    def test_complex_expression(self, p):
        assert p._parse_cron("*/15 9-17 1,15 * 1-5") == ("*/15", "9-17", "1,15", "*", "1-5")

    def test_empty_raises(self, p):
        with pytest.raises(ValueError, match="empty"):
            p._parse_cron("")

    def test_too_few_fields_raises(self, p):
        with pytest.raises(ValueError, match="5 fields"):
            p._parse_cron("0 3 * *")

    def test_too_many_fields_raises(self, p):
        with pytest.raises(ValueError, match="5 fields"):
            p._parse_cron("0 3 * * * *")

    def test_extra_whitespace_normalised(self, p):
        assert p._parse_cron("  0   3 * * *  ") == ("0", "3", "*", "*", "*")


# ---------- _extract_genres ----------

class TestExtractGenres:
    def test_strips_language_prefix(self, p):
        # EN - prefix should be removed, NOT the AC- in AC-130 style names
        assert p._extract_genres("EN - Action") == ["Action"]

    def test_preserves_AC_130_in_category(self, p):
        # Regression: was previously stripped, leaving "130 Action"
        assert p._extract_genres("AC-130 Action") == ["Ac-130 Action"]

    def test_strips_movie_suffix(self, p):
        assert p._extract_genres("Action (movie)") == ["Action"]
        assert p._extract_genres("Drama (series)") == ["Drama"]

    def test_splits_on_separators(self, p):
        assert p._extract_genres("Action / Adventure") == ["Action", "Adventure"]
        assert p._extract_genres("Action & Adventure") == ["Action", "Adventure"]
        assert p._extract_genres("Action, Adventure") == ["Action", "Adventure"]

    def test_capitalises_each_word(self, p):
        assert p._extract_genres("science fiction") == ["Science Fiction"]

    def test_empty_returns_empty_list(self, p):
        assert p._extract_genres("") == []
        assert p._extract_genres(None) == []

    def test_unknown_fallback(self, p):
        # If everything is stripped away
        assert p._extract_genres("(movie)") == ["Unknown"]


# ---------- _mask_url ----------

class TestMaskUrl:
    def test_masks_host(self, p):
        assert p._mask_url("http://192.168.100.111:9191/path") == "http://<host>:9191/path"

    def test_masks_host_no_port(self, p):
        assert p._mask_url("http://example.com/path") == "http://<host>/path"

    def test_handles_no_path(self, p):
        assert p._mask_url("http://example.com:8080") == "http://<host>:8080"

    def test_unrecognised_url_passthrough(self, p):
        assert p._mask_url("not-a-url") == "not-a-url"

    def test_empty(self, p):
        assert p._mask_url("") == ""


# ---------- _valid_schedule_targets ----------

class TestValidScheduleTargets:
    def test_returns_action_ids_from_manifest(self, p):
        targets = p._valid_schedule_targets()
        # Should match the schedule_target field's options
        assert "rescan_all" in targets
        assert "scan_all_vods" in targets
        assert "generate_movies" in targets
        assert "generate_series" in targets
        # Should NOT contain non-target actions
        assert "cleanup_movies" not in targets
        assert "apply_schedule" not in targets


# ---------- _validate_dispatcharr_url ----------

class TestValidateDispatcharrUrl:
    def test_valid_lan_url(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url("http://192.168.1.10:9191", log)
        assert ok is True
        assert err is None
        assert log.warnings == []

    def test_empty_string_rejected(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url("", log)
        assert ok is False
        assert "empty" in err.lower()

    def test_whitespace_only_rejected(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url("   ", log)
        assert ok is False
        assert "empty" in err.lower()

    def test_none_rejected(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url(None, log)
        assert ok is False
        assert "empty" in err.lower()

    def test_placeholder_rejected(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url(p.PLACEHOLDER_DISPATCHARR_URL, log)
        assert ok is False
        assert "placeholder" in err.lower()

    def test_localhost_warns_but_passes(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url("http://localhost:9191", log)
        assert ok is True
        assert err is None
        assert len(log.warnings) == 1
        assert "localhost" in log.warnings[0].lower()

    def test_127_0_0_1_warns_but_passes(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url("http://127.0.0.1:9191", log)
        assert ok is True
        assert len(log.warnings) == 1

    def test_localhost_with_path(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url("http://localhost:9191/proxy", log)
        assert ok is True
        assert len(log.warnings) == 1

    def test_real_url_no_warning(self, p):
        log = CapturingLogger()
        ok, err = p._validate_dispatcharr_url("https://dispatcharr.example.com", log)
        assert ok is True
        assert log.warnings == []


# ---------- _validate_timezone ----------

class TestValidateTimezone:
    def test_empty_string_ok(self, p):
        ok, err = p._validate_timezone("")
        assert ok is True
        assert err is None

    def test_none_ok(self, p):
        ok, err = p._validate_timezone(None)
        assert ok is True

    def test_whitespace_only_ok(self, p):
        ok, err = p._validate_timezone("   ")
        assert ok is True

    def test_utc(self, p):
        ok, err = p._validate_timezone("UTC")
        assert ok is True

    def test_iana_zones(self, p):
        for tz in ("Europe/London", "America/New_York", "Australia/Sydney", "Asia/Tokyo"):
            ok, err = p._validate_timezone(tz)
            assert ok is True, f"expected {tz!r} to be valid"

    def test_invalid_zone_rejected(self, p):
        ok, err = p._validate_timezone("Not/A/Real/Zone")
        assert ok is False
        assert "Invalid timezone" in err

    def test_garbage_rejected(self, p):
        ok, err = p._validate_timezone("definitely-not-a-zone")
        assert ok is False


# ---------- _split_genres_clean ----------

class TestSplitGenresClean:
    def test_empty(self, p):
        assert p._split_genres_clean("") == []
        assert p._split_genres_clean(None) == []

    def test_preserves_case_for_tmdb_style(self, p):
        # 'Sci-Fi' must NOT become 'Sci-fi' (which is what _extract_genres would do)
        assert p._split_genres_clean("Sci-Fi & Fantasy") == ["Sci-Fi", "Fantasy"]

    def test_splits_on_comma(self, p):
        assert p._split_genres_clean("Crime, Drama") == ["Crime", "Drama"]

    def test_splits_on_slash(self, p):
        assert p._split_genres_clean("Action / Adventure") == ["Action", "Adventure"]

    def test_single_genre(self, p):
        assert p._split_genres_clean("Crime") == ["Crime"]


# ---------- _resolve_genres ----------

class TestResolveGenres:
    def test_db_genre_preferred(self, p):
        # When series.genre is populated (TMDB-grade), use it.
        result = p._resolve_genres("Sci-Fi & Fantasy", "EN - Australian Tv (series)")
        assert result == ["Sci-Fi", "Fantasy"]

    def test_falls_back_to_category(self, p):
        result = p._resolve_genres("", "EN - Action / Adventure (series)")
        assert "Action" in result
        assert "Adventure" in result

    def test_falls_back_with_none(self, p):
        result = p._resolve_genres(None, "Drama (series)")
        assert "Drama" in result

    def test_whitespace_db_genre_falls_back(self, p):
        result = p._resolve_genres("   ", "Action (movie)")
        assert "Action" in result

    def test_year_bucket_category_suppressed(self, p):
        # Pure year-bucket category like "2026 Movies" yields no genre.
        # The TMDB id in the NFO will let the media server fetch real genres.
        assert p._resolve_genres("", "2026 Movies") == []
        assert p._resolve_genres("", "2025 Movie") == []
        assert p._resolve_genres("", "1990s Movies") == []
        assert p._resolve_genres("", "2026 Series") == []
        assert p._resolve_genres("", "2026 TV Shows") == []

    def test_year_bucket_filter_preserves_real_genres(self, p):
        # Real categorical genres pass through.
        assert "Action" in p._resolve_genres("", "Action")
        assert "Drama" in p._resolve_genres("", "Drama (movie)")

    def test_mixed_year_bucket_and_real_genre(self, p):
        # Slash-separated mix: keep the real genre, drop the bucket.
        result = p._resolve_genres("", "Action / 2026 Movies")
        assert "Action" in result
        assert not any("Movies" in g for g in result)


# ---------- _is_year_bucket_genre ----------

class TestIsYearBucketGenre:
    def test_matches_year_movies(self, p):
        assert p._is_year_bucket_genre("2026 Movies") is True
        assert p._is_year_bucket_genre("2025 Movie") is True
        assert p._is_year_bucket_genre("1990s Movies") is True

    def test_matches_year_series(self, p):
        assert p._is_year_bucket_genre("2026 Series") is True

    def test_matches_year_tv_shows(self, p):
        assert p._is_year_bucket_genre("2026 TV Shows") is True
        assert p._is_year_bucket_genre("2026 TVShows") is True

    def test_case_insensitive(self, p):
        assert p._is_year_bucket_genre("2026 movies") is True
        assert p._is_year_bucket_genre("2026 MOVIES") is True

    def test_real_genres_not_matched(self, p):
        assert p._is_year_bucket_genre("Action") is False
        assert p._is_year_bucket_genre("Sci-Fi") is False
        assert p._is_year_bucket_genre("Drama") is False

    def test_year_plus_genre_not_matched(self, p):
        # "2026 Action Movies" has more than just year + Movies — keep it
        assert p._is_year_bucket_genre("2026 Action Movies") is False

    def test_movies_with_qualifier_not_matched(self, p):
        # "Movies 2026" reverses order — keep it
        assert p._is_year_bucket_genre("Movies 2026") is False

    def test_empty_string(self, p):
        assert p._is_year_bucket_genre("") is False

    def test_none(self, p):
        assert p._is_year_bucket_genre(None) is False


# ---------- NFO generation: tmdbid / uniqueid / rating / aired / runtime ----------

class _FakeSeries:
    def __init__(self, **kw):
        self.name = kw.get("name", "Tidelands")
        self.year = kw.get("year", 2018)
        self.description = kw.get("description", "")
        self.tmdb_id = kw.get("tmdb_id", "")
        self.imdb_id = kw.get("imdb_id", "")
        self.rating = kw.get("rating", "")
        self.genre = kw.get("genre", "")


class _FakeEpisode:
    def __init__(self, **kw):
        self.name = kw.get("name", "Pilot")
        self.season_number = kw.get("season_number", 1)
        self.episode_number = kw.get("episode_number", 1)
        self.description = kw.get("description", "")
        self.tmdb_id = kw.get("tmdb_id", "")
        self.imdb_id = kw.get("imdb_id", "")
        self.rating = kw.get("rating", "")
        self.air_date = kw.get("air_date", None)
        self.duration_secs = kw.get("duration_secs", 0)


class _FakeMovie:
    def __init__(self, **kw):
        self.name = kw.get("name", "Aladdin")
        self.year = kw.get("year", 1992)
        self.description = kw.get("description", "")
        self.tmdb_id = kw.get("tmdb_id", "")
        self.imdb_id = kw.get("imdb_id", "")
        self.rating = kw.get("rating", "")
        self.genre = kw.get("genre", "")


class TestTvshowNfo:
    def test_emits_tmdbid_and_uniqueid(self, p):
        s = _FakeSeries(tmdb_id="83381")
        out = p._generate_tvshow_nfo(s, "")
        assert "<tmdbid>83381</tmdbid>" in out
        assert '<uniqueid type="tmdb" default="true">83381</uniqueid>' in out

    def test_no_tmdbid_when_unset(self, p):
        s = _FakeSeries(tmdb_id="")
        out = p._generate_tvshow_nfo(s, "")
        assert "<tmdbid>" not in out
        assert "<uniqueid" not in out

    def test_emits_rating(self, p):
        s = _FakeSeries(rating="7.0")
        out = p._generate_tvshow_nfo(s, "")
        assert "<rating>7.0</rating>" in out

    def test_prefers_db_genre(self, p):
        s = _FakeSeries(genre="Sci-Fi & Fantasy")
        out = p._generate_tvshow_nfo(s, "EN - Australian Tv (series)")
        assert "<genre>Sci-Fi</genre>" in out
        assert "<genre>Fantasy</genre>" in out
        assert "<genre>Australian Tv</genre>" not in out

    def test_falls_back_to_category_genre(self, p):
        s = _FakeSeries(genre="")
        out = p._generate_tvshow_nfo(s, "Drama (series)")
        assert "<genre>Drama</genre>" in out

    def test_title_does_not_include_year(self, p):
        s = _FakeSeries(name="Tidelands (2018)", year=2018)
        out = p._generate_tvshow_nfo(s, "")
        assert "<title>Tidelands</title>" in out
        assert "<year>2018</year>" in out


class TestEpisodeNfo:
    def test_basic(self, p):
        e = _FakeEpisode()
        out = p._generate_episode_nfo(e)
        assert "<season>1</season>" in out
        assert "<episode>1</episode>" in out

    def test_emits_aired(self, p):
        import datetime
        e = _FakeEpisode(air_date=datetime.date(2018, 12, 14))
        out = p._generate_episode_nfo(e)
        assert "<aired>2018-12-14</aired>" in out

    def test_emits_runtime_minutes_from_seconds(self, p):
        e = _FakeEpisode(duration_secs=2700)  # 45 min
        out = p._generate_episode_nfo(e)
        assert "<runtime>45</runtime>" in out

    def test_zero_duration_omitted(self, p):
        e = _FakeEpisode(duration_secs=0)
        out = p._generate_episode_nfo(e)
        assert "<runtime>" not in out

    def test_emits_episode_tmdbid_when_set(self, p):
        e = _FakeEpisode(tmdb_id="123")
        out = p._generate_episode_nfo(e)
        assert "<tmdbid>123</tmdbid>" in out


class TestMovieNfoWithDbGenre:
    def test_db_genre_preferred(self, p):
        m = _FakeMovie(genre="Action & Adventure")
        out = p._generate_nfo(m, "EN - Crap Category (movie)")
        assert "<genre>Action</genre>" in out
        assert "<genre>Adventure</genre>" in out

    def test_uniqueid_added(self, p):
        m = _FakeMovie(tmdb_id="11", imdb_id="tt0103639")
        out = p._generate_nfo(m, "")
        assert "<tmdbid>11</tmdbid>" in out
        assert '<uniqueid type="tmdb" default="true">11</uniqueid>' in out
        assert "<imdbid>tt0103639</imdbid>" in out
        assert '<uniqueid type="imdb">tt0103639</uniqueid>' in out

    def test_year_bucket_category_emits_no_genre(self, p):
        # The whole point of v1.10.1: 'YYYY Movies' category produces no <genre>
        m = _FakeMovie(genre="", tmdb_id="42")
        out = p._generate_nfo(m, "2026 Movies")
        assert "<genre>" not in out
        # but the tmdbid is still there so media servers can fetch genre via TMDB
        assert "<tmdbid>42</tmdbid>" in out


# ---------- _movie_target_paths ----------

class TestMovieTargetPaths:
    def test_uses_db_year(self, p):
        # Build a minimal stand-in with the attributes the helper reads
        class M:
            id = 1
            uuid = "abc"
            name = "Aladdin"
            year = 1992
        folder, strm, name, year = p._movie_target_paths(M(), "/VODS/Movies")
        assert folder == "/VODS/Movies/Aladdin (1992)"
        assert strm == "Aladdin (1992).strm"
        assert name == "Aladdin"
        assert year == 1992

    def test_strips_year_from_title_and_dedupes(self, p):
        class M:
            id = 1
            uuid = "abc"
            name = "Aladdin (2026)"
            year = 2026
        folder, strm, name, year = p._movie_target_paths(M(), "/VODS/Movies")
        # The fix: no double year
        assert folder == "/VODS/Movies/Aladdin (2026)"
        assert strm == "Aladdin (2026).strm"
        assert name == "Aladdin"
        assert year == 2026

    def test_recovers_year_from_title_when_db_year_missing(self, p):
        class M:
            id = 1
            uuid = "abc"
            name = "Aladdin (1992)"
            year = None
        folder, strm, name, year = p._movie_target_paths(M(), "/VODS/Movies")
        assert folder == "/VODS/Movies/Aladdin (1992)"
        assert year == 1992

    def test_no_year_anywhere(self, p):
        class M:
            id = 7
            uuid = "abc"
            name = "Mystery Title"
            year = None
        folder, strm, name, year = p._movie_target_paths(M(), "/VODS/Movies")
        assert folder == "/VODS/Movies/Mystery Title"
        assert strm == "Mystery Title.strm"
        assert year is None


# ---------- nesting by category ----------

class TestCategorySubfolder:
    def test_nest_off_returns_empty(self, p):
        assert p._category_subfolder("Action", nest=False) == ""
        assert p._category_subfolder("", nest=False) == ""

    def test_nest_on_with_category(self, p):
        # Raw category preserved (just sanitised for filesystem)
        assert p._category_subfolder("Action", nest=True) == "Action"
        assert p._category_subfolder("EN - Action (movie)", nest=True) == "EN - Action (movie)"

    def test_nest_on_no_category_returns_unassigned(self, p):
        assert p._category_subfolder("", nest=True) == "Unassigned"
        assert p._category_subfolder(None, nest=True) == "Unassigned"
        assert p._category_subfolder("   ", nest=True) == "Unassigned"

    def test_nest_on_sanitises_invalid_chars(self, p):
        # Slashes and other invalid filesystem chars must be stripped (and the
        # surrounding whitespace then collapsed by the sanitiser)
        assert p._category_subfolder("Action / Drama", nest=True) == "Action Drama"
        assert "/" not in p._category_subfolder("a/b", nest=True)
        assert "\\" not in p._category_subfolder("a\\b", nest=True)


class TestMovieTargetPathsNested:
    class _M:
        id = 1
        uuid = "abc"
        name = "Aladdin"
        year = 1992

    def test_nest_off_unchanged(self, p):
        folder, _, _, _ = p._movie_target_paths(self._M(), "/VODS/Movies", "Action", nest=False)
        assert folder == "/VODS/Movies/Aladdin (1992)"

    def test_nest_on_with_category(self, p):
        folder, _, _, _ = p._movie_target_paths(self._M(), "/VODS/Movies", "Action", nest=True)
        assert folder == "/VODS/Movies/Action/Aladdin (1992)"

    def test_nest_on_empty_category(self, p):
        folder, _, _, _ = p._movie_target_paths(self._M(), "/VODS/Movies", "", nest=True)
        assert folder == "/VODS/Movies/Unassigned/Aladdin (1992)"

    def test_nest_on_raw_category_preserved(self, p):
        # Raw category — even ugly ones go in verbatim (per design choice 1)
        folder, _, _, _ = p._movie_target_paths(self._M(), "/VODS/Movies", "EN - Action (movie)", nest=True)
        assert folder == "/VODS/Movies/EN - Action (movie)/Aladdin (1992)"


class TestSeriesTargetFolderNested:
    class _S:
        id = 1
        uuid = "abc"
        name = "Tidelands"
        year = 2018

    def test_nest_off_unchanged(self, p):
        folder, _, _ = p._series_target_folder(self._S(), "/VODS/Series", "Drama", nest=False)
        assert folder == "/VODS/Series/Tidelands (2018)"

    def test_nest_on_with_category(self, p):
        folder, _, _ = p._series_target_folder(self._S(), "/VODS/Series", "Drama", nest=True)
        assert folder == "/VODS/Series/Drama/Tidelands (2018)"

    def test_nest_on_empty_category(self, p):
        folder, _, _ = p._series_target_folder(self._S(), "/VODS/Series", "", nest=True)
        assert folder == "/VODS/Series/Unassigned/Tidelands (2018)"


# ---------- cleanup walk ----------

class TestWalkAndCleanup:
    """Tests _walk_and_cleanup_plugin_files against real temp dirs.

    Uses tmp_path (pytest builtin) — covers both flat and nested layouts and
    the user-files-preserved case.
    """

    def test_flat_layout_cleaned(self, p, tmp_path):
        log = CapturingLogger()
        # Movies/Aladdin (1992)/{.strm, .nfo}
        movie = tmp_path / "Aladdin (1992)"
        movie.mkdir()
        (movie / "Aladdin (1992).strm").write_text("http://...")
        (movie / "Aladdin (1992).nfo").write_text("<movie/>")

        r = p._walk_and_cleanup_plugin_files(str(tmp_path), log)
        assert r["deleted_strm"] == 1
        assert r["deleted_nfo"] == 1
        assert r["removed_dirs"] == 1
        assert r["errors"] == 0
        assert not movie.exists()
        assert tmp_path.exists()  # root preserved

    def test_nested_layout_cleaned(self, p, tmp_path):
        log = CapturingLogger()
        # Movies/Action/Aladdin (1992)/{.strm, .nfo}
        cat = tmp_path / "Action"
        cat.mkdir()
        movie = cat / "Aladdin (1992)"
        movie.mkdir()
        (movie / "Aladdin (1992).strm").write_text("http://...")
        (movie / "Aladdin (1992).nfo").write_text("<movie/>")

        r = p._walk_and_cleanup_plugin_files(str(tmp_path), log)
        assert r["deleted_strm"] == 1
        assert r["deleted_nfo"] == 1
        # Both the movie folder AND the category folder removed (both empty)
        assert r["removed_dirs"] == 2
        assert not cat.exists()
        assert tmp_path.exists()

    def test_series_with_seasons(self, p, tmp_path):
        log = CapturingLogger()
        # Series/Tidelands/{tvshow.nfo, Season 01/{strm, nfo}}
        series = tmp_path / "Tidelands"
        series.mkdir()
        (series / "tvshow.nfo").write_text("<tvshow/>")
        season = series / "Season 01"
        season.mkdir()
        (season / "ep1.strm").write_text("http://...")
        (season / "ep1.nfo").write_text("<episodedetails/>")

        r = p._walk_and_cleanup_plugin_files(str(tmp_path), log)
        assert r["deleted_strm"] == 1
        assert r["deleted_nfo"] == 2  # episode.nfo + tvshow.nfo
        assert r["removed_dirs"] == 2  # Season 01 + series folder

    def test_user_files_preserved(self, p, tmp_path):
        log = CapturingLogger()
        movie = tmp_path / "Aladdin (1992)"
        movie.mkdir()
        (movie / "Aladdin (1992).strm").write_text("http://...")
        (movie / "Aladdin (1992).nfo").write_text("<movie/>")
        # User added files
        (movie / "poster.jpg").write_text("not a real image")
        (movie / "Aladdin (1992).en.srt").write_text("subtitles")

        r = p._walk_and_cleanup_plugin_files(str(tmp_path), log)
        assert r["deleted_strm"] == 1
        assert r["deleted_nfo"] == 1
        assert r["removed_dirs"] == 0  # movie folder preserved (user files inside)
        assert r["preserved_dirs"] >= 1
        assert movie.exists()
        assert (movie / "poster.jpg").exists()
        assert (movie / "Aladdin (1992).en.srt").exists()

    def test_nonexistent_root_no_error(self, p, tmp_path):
        log = CapturingLogger()
        missing = tmp_path / "does-not-exist"
        r = p._walk_and_cleanup_plugin_files(str(missing), log)
        assert r["errors"] == 0
        assert r["deleted_strm"] == 0

    def test_root_itself_never_removed(self, p, tmp_path):
        log = CapturingLogger()
        # Tree that becomes entirely empty
        (tmp_path / "Aladdin (1992)").mkdir()
        (tmp_path / "Aladdin (1992)" / "Aladdin (1992).strm").write_text("x")
        r = p._walk_and_cleanup_plugin_files(str(tmp_path), log)
        # Root must still exist
        assert tmp_path.exists()
        assert r["removed_dirs"] == 1  # only the Aladdin folder, not the root


# ---------- _extract_clean_name_and_year (v1.15.0) ----------

class TestExtractCleanNameAndYear:
    """The aggressive cleanup used for folder names. Truncates at the first
    (YYYY), strips quality tokens, leaves the gentler _clean_title /
    _strip_trailing_year helpers untouched for NFO title generation."""

    def test_sjsteve_discord_example_cool_hand_luke(self, p):
        # The exact example from the Discord report: trailing cast + duplicate
        # year defeat ChannelsDVR's metadata scraper. Expected output is the
        # clean canonical title with the first (YYYY) only.
        title, year = p._extract_clean_name_and_year("Cool Hand Luke 4K (1967) PAUL NEWMAN (1967)")
        assert title == "Cool Hand Luke"
        assert year == 1967

    def test_simple_year_in_parens(self, p):
        title, year = p._extract_clean_name_and_year("The Matrix (1999)")
        assert title == "The Matrix"
        assert year == 1999

    def test_language_prefix_stripped(self, p):
        title, year = p._extract_clean_name_and_year("EN - The Matrix (1999)")
        assert title == "The Matrix"
        assert year == 1999

    def test_quality_token_stripped(self, p):
        # 1080p / HEVC inside the title — stripped after year truncation.
        title, year = p._extract_clean_name_and_year("Whiplash 1080p HEVC (2014)")
        assert title == "Whiplash"
        assert year == 2014

    def test_quality_token_4k_uhd_hdr(self, p):
        title, year = p._extract_clean_name_and_year("Dune 4K UHD HDR (2021)")
        assert title == "Dune"
        assert year == 2021

    def test_first_year_wins_when_two_present(self, p):
        # Sanity for the truncate-at-first-year rule: garbage AND a second year.
        title, year = p._extract_clean_name_and_year("Title (1995) extra (2000)")
        assert title == "Title"
        assert year == 1995

    def test_no_year_anywhere(self, p):
        title, year = p._extract_clean_name_and_year("Avatar")
        assert title == "Avatar"
        assert year is None

    def test_only_quality_tokens_no_year(self, p):
        title, year = p._extract_clean_name_and_year("Inception 4K HEVC")
        assert title == "Inception"
        assert year is None

    def test_empty_input(self, p):
        assert p._extract_clean_name_and_year("") == ("", None)
        assert p._extract_clean_name_and_year(None) == (None, None)

    def test_legit_substrings_preserved(self, p):
        # 'HD' must not eat 'Indiana' / 'Headhunter' / etc — boundary anchored.
        title, year = p._extract_clean_name_and_year("Indiana Jones (1981)")
        assert title == "Indiana Jones"
        assert year == 1981
        title, _ = p._extract_clean_name_and_year("Headhunter")
        assert title == "Headhunter"

    def test_ac_130_preserved(self, p):
        # The original _clean_title carve-out — AC-130 / MI-5 must not be
        # treated as a language prefix.
        title, year = p._extract_clean_name_and_year("AC-130 (2018)")
        assert title == "AC-130"
        assert year == 2018

    def test_trailing_separator_stripped(self, p):
        # Quality token removal can leave " - " or " ," dangling — clean it.
        title, year = p._extract_clean_name_and_year("Title 4K - (2020)")
        assert title == "Title"
        assert year == 2020


# ---------- _apply_tmdb_suffix (v1.15.0) ----------

class TestApplyTmdbSuffix:
    def test_off_returns_unchanged(self, p):
        class M:
            tmdb_id = "378"
        assert p._apply_tmdb_suffix("Cool Hand Luke (1967)", M(), False) == "Cool Hand Luke (1967)"

    def test_on_with_id_appends_suffix(self, p):
        class M:
            tmdb_id = "378"
        assert p._apply_tmdb_suffix("Cool Hand Luke (1967)", M(), True) == "Cool Hand Luke (1967) {tmdb-378}"

    def test_on_without_id_returns_unchanged(self, p):
        # No garbage suffix when the TMDB ID is missing.
        class M:
            tmdb_id = ""
        assert p._apply_tmdb_suffix("Cool Hand Luke (1967)", M(), True) == "Cool Hand Luke (1967)"

    def test_on_with_whitespace_id_returns_unchanged(self, p):
        class M:
            tmdb_id = "   "
        assert p._apply_tmdb_suffix("Cool Hand Luke (1967)", M(), True) == "Cool Hand Luke (1967)"

    def test_on_with_none_obj_attr_returns_unchanged(self, p):
        # Defensive: getattr default catches a missing attribute.
        class M:
            pass
        assert p._apply_tmdb_suffix("Cool Hand Luke (1967)", M(), True) == "Cool Hand Luke (1967)"


# ---------- _logo_url (v1.15.0) ----------

class TestLogoUrl:
    def test_returns_url_from_fk_logo(self, p):
        # The shape Dispatcharr's VODLogo presents: a related model with .url.
        class L:
            url = "https://image.tmdb.org/t/p/w600/abc.jpg"
        class M:
            logo = L()
        assert p._logo_url(M()) == "https://image.tmdb.org/t/p/w600/abc.jpg"

    def test_returns_empty_when_no_logo(self, p):
        class M:
            logo = None
        assert p._logo_url(M()) == ""

    def test_returns_empty_when_missing_attr(self, p):
        class M:
            pass
        assert p._logo_url(M()) == ""

    def test_returns_empty_when_logo_url_blank(self, p):
        class L:
            url = "   "
        class M:
            logo = L()
        assert p._logo_url(M()) == ""

    def test_handles_plain_string_logo(self, p):
        # Defensive: if a future schema swaps the FK for a flat string column,
        # the helper still picks up the URL.
        class M:
            logo = "https://image.tmdb.org/t/p/w400/xyz.jpg"
        assert p._logo_url(M()) == "https://image.tmdb.org/t/p/w400/xyz.jpg"


# ---------- folder paths with append_tmdb_id (v1.15.0) ----------

class TestMovieTargetPathsWithTmdbSuffix:
    class _M:
        id = 1
        uuid = "abc"
        name = "Cool Hand Luke 4K (1967) PAUL NEWMAN (1967)"
        year = None
        tmdb_id = "378"

    def test_dirty_provider_name_cleaned_to_canonical_folder(self, p):
        # Without the toggle, just the cleanup applies.
        folder, strm, name, year = p._movie_target_paths(self._M(), "/VODS/Movies")
        assert folder == "/VODS/Movies/Cool Hand Luke (1967)"
        assert strm == "Cool Hand Luke (1967).strm"
        assert name == "Cool Hand Luke"
        assert year == 1967

    def test_tmdb_suffix_appended_when_toggle_on(self, p):
        folder, strm, name, year = p._movie_target_paths(
            self._M(), "/VODS/Movies", category_name="", nest=False, append_tmdb_id=True,
        )
        assert folder == "/VODS/Movies/Cool Hand Luke (1967) {tmdb-378}"
        # The strm filename inside the folder is unaffected — scrapers only
        # care about the folder name.
        assert strm == "Cool Hand Luke (1967).strm"

    def test_tmdb_suffix_skipped_when_id_missing(self, p):
        class M:
            id = 1; uuid = "x"; name = "Mystery"; year = None; tmdb_id = ""
        folder, _, _, _ = p._movie_target_paths(M(), "/VODS/Movies", append_tmdb_id=True)
        assert folder == "/VODS/Movies/Mystery"


class TestSeriesTargetFolderWithTmdbSuffix:
    class _S:
        id = 1
        uuid = "abc"
        name = "Breaking Bad UHD (2008)"
        year = None
        tmdb_id = "1396"

    def test_dirty_series_name_cleaned(self, p):
        folder, name, year = p._series_target_folder(self._S(), "/VODS/Series")
        assert folder == "/VODS/Series/Breaking Bad (2008)"
        assert name == "Breaking Bad"
        assert year == 2008

    def test_tmdb_suffix_appended_when_toggle_on(self, p):
        folder, _, _ = p._series_target_folder(
            self._S(), "/VODS/Series", category_name="", nest=False, append_tmdb_id=True,
        )
        assert folder == "/VODS/Series/Breaking Bad (2008) {tmdb-1396}"


# ---------- NFO emits <thumb> when logo URL present (v1.15.0) ----------

class TestNfoThumbEmission:
    def test_movie_nfo_emits_thumb_when_logo_present(self, p):
        class L:
            url = "https://image.tmdb.org/t/p/w600/abc.jpg"
        class M:
            name = "The Matrix"
            year = 1999
            description = ""
            rating = ""
            tmdb_id = "603"
            imdb_id = ""
            genre = ""
            logo = L()
        nfo = p._generate_nfo(M(), category_name="")
        assert '<thumb aspect="poster">https://image.tmdb.org/t/p/w600/abc.jpg</thumb>' in nfo

    def test_movie_nfo_omits_thumb_when_no_logo(self, p):
        class M:
            name = "The Matrix"
            year = 1999
            description = ""
            rating = ""
            tmdb_id = "603"
            imdb_id = ""
            genre = ""
            logo = None
        nfo = p._generate_nfo(M(), category_name="")
        assert "<thumb" not in nfo

    def test_tvshow_nfo_emits_thumb_when_logo_present(self, p):
        class L:
            url = "https://image.tmdb.org/t/p/w400/show.jpg"
        class S:
            name = "Breaking Bad"
            year = 2008
            description = ""
            rating = ""
            tmdb_id = "1396"
            imdb_id = ""
            genre = ""
            logo = L()
        nfo = p._generate_tvshow_nfo(S(), category_name="")
        assert '<thumb aspect="poster">https://image.tmdb.org/t/p/w400/show.jpg</thumb>' in nfo


# ---------- dedupe across categories (v1.15.1) ----------

class TestDedupeAcrossCategoriesDecision:
    """The dedupe-across-categories logic is inline in `_generate_movies` and
    `_generate_series` (a `seen` set + a continue-on-hit branch). These tests
    pin down the exact dedup contract by simulating the inline pattern — same
    membership check + set-add the production code uses — so a refactor that
    changes the semantics would surface here.

    Closes #1 — duplicates when nesting is ON and a movie is tagged with
    multiple categories upstream.
    """

    def _simulate_dedupe(self, uuids, dedupe_on):
        """Mirrors the inline pattern in `_generate_movies`:

            seen = set() if dedupe_on else None
            for rel in iter:
                if seen is not None:
                    if rel.uuid in seen:
                        deduped += 1
                        continue
                    seen.add(rel.uuid)
                processed.append(rel)

        Returns (processed_uuids, dedup_count).
        """
        seen = set() if dedupe_on else None
        processed = []
        deduped = 0
        for u in uuids:
            if seen is not None:
                if u in seen:
                    deduped += 1
                    continue
                seen.add(u)
            processed.append(u)
        return processed, deduped

    def test_off_preserves_all_rows(self, p):
        # With the toggle OFF, every row (including dupes) is processed —
        # current default behaviour, matches the 4K-vs-HD variant case.
        uuids = ["A", "B", "A", "C", "B"]
        processed, deduped = self._simulate_dedupe(uuids, dedupe_on=False)
        assert processed == uuids
        assert deduped == 0

    def test_on_keeps_only_first_occurrence(self, p):
        # The exact bug from #1: same movie under multiple categories. Toggle
        # ON => keep first encounter, skip duplicates, count them.
        uuids = ["A", "B", "A", "C", "B", "A"]
        processed, deduped = self._simulate_dedupe(uuids, dedupe_on=True)
        assert processed == ["A", "B", "C"]
        assert deduped == 3

    def test_on_no_duplicates_is_lossless(self, p):
        # If the input has no duplicates, dedup ON is identical to dedup OFF.
        uuids = ["A", "B", "C", "D"]
        processed, deduped = self._simulate_dedupe(uuids, dedupe_on=True)
        assert processed == uuids
        assert deduped == 0

    def test_on_empty_input(self, p):
        processed, deduped = self._simulate_dedupe([], dedupe_on=True)
        assert processed == []
        assert deduped == 0

    def test_on_preserves_first_occurrence_order(self, p):
        # With the production ORDER BY category__name, id the first occurrence
        # of each UUID is the alphabetically-first category. The dedup logic
        # itself preserves whatever order the iterator presents — these tests
        # don't assert the SQL ordering, only that the FIRST-SEEN behaviour
        # is deterministic given a fixed input order.
        uuids = ["zebra", "ant", "zebra", "ant", "horse"]
        processed, deduped = self._simulate_dedupe(uuids, dedupe_on=True)
        assert processed == ["zebra", "ant", "horse"]
        assert deduped == 2


# ---------- _strip_redundant_trailing_year (v1.15.2) ----------

class TestStripRedundantTrailingYear:
    """Bare trailing year de-duplication for folder names. Mode (b):
    strip-when-matching, and adopt-when-no-year-known."""

    def test_lid_example_strips_when_matching_db_year(self, p):
        # "Wicked: For Good - 2025" + DB year 2025 -> "Wicked: For Good"
        name, year = p._strip_redundant_trailing_year("Wicked: For Good - 2025", 2025)
        assert name == "Wicked: For Good"
        assert year == 2025

    def test_strips_with_only_a_space_separator(self, p):
        name, year = p._strip_redundant_trailing_year("The Matrix 1999", 1999)
        assert name == "The Matrix"
        assert year == 1999

    def test_adopts_bare_year_when_no_db_year(self, p):
        # No DB year, bare trailing year present -> adopt it AND strip.
        name, year = p._strip_redundant_trailing_year("Wicked: For Good - 2025", None)
        assert name == "Wicked: For Good"
        assert year == 2025

    def test_does_not_adopt_implausible_trailing_number(self, p):
        # Room 1408, no DB year: 1408 < 1900 -> not a year, leave it.
        name, year = p._strip_redundant_trailing_year("Room 1408", None)
        assert name == "Room 1408"
        assert year is None

    def test_preserves_blade_runner_2049(self, p):
        # Trailing 2049 != DB year 2017 -> it's part of the title, keep it.
        name, year = p._strip_redundant_trailing_year("Blade Runner 2049", 2017)
        assert name == "Blade Runner 2049"
        assert year == 2017

    def test_preserves_room_1408_with_db_year(self, p):
        name, year = p._strip_redundant_trailing_year("Room 1408", 2007)
        assert name == "Room 1408"
        assert year == 2007

    def test_year_is_the_whole_title_not_emptied(self, p):
        # "1984" with year 1984 must NOT become "" — the year is the title.
        name, year = p._strip_redundant_trailing_year("1984", 1984)
        assert name == "1984"
        assert year == 1984
        # Same for "2012" adopt path.
        name2, year2 = p._strip_redundant_trailing_year("2012", None)
        assert name2 == "2012"

    def test_no_trailing_year_is_noop(self, p):
        name, year = p._strip_redundant_trailing_year("Avatar", 2009)
        assert name == "Avatar"
        assert year == 2009

    def test_not_part_of_longer_digit_run(self, p):
        # Negative lookbehind: a 5-digit trailing run isn't treated as a year.
        name, year = p._strip_redundant_trailing_year("Catalog 12345", None)
        assert name == "Catalog 12345"
        assert year is None

    def test_empty_input(self, p):
        assert p._strip_redundant_trailing_year("", 2025) == ("", 2025)
        assert p._strip_redundant_trailing_year(None, None) == (None, None)


class TestMovieTargetPathsBareYear:
    """End-to-end: the bare-year fix flows through _movie_target_paths."""

    def test_bare_trailing_year_no_double(self, p):
        # NB: the ':' is removed downstream by _sanitize_filename (invalid on
        # Windows), so the folder is "Wicked For Good (2025)" — the point of
        # this test is the absence of the doubled "- 2025 (2025)".
        class M:
            id = 1
            uuid = "x"
            name = "Wicked: For Good - 2025"
            year = 2025
        folder, strm, name, year = p._movie_target_paths(M(), "/VODS/Movies")
        assert folder == "/VODS/Movies/Wicked For Good (2025)"
        assert strm == "Wicked For Good (2025).strm"
        assert year == 2025

    def test_bare_trailing_year_adopted_when_db_year_missing(self, p):
        class M:
            id = 2
            uuid = "y"
            name = "Wicked: For Good - 2025"
            year = None
        folder, strm, name, year = p._movie_target_paths(M(), "/VODS/Movies")
        assert folder == "/VODS/Movies/Wicked For Good (2025)"
        assert year == 2025


# ---------- _settings_drift_keys (v1.15.2) ----------

class _FakeTask:
    def __init__(self, kwargs_str):
        self.kwargs = kwargs_str


class TestSettingsDriftKeys:
    def test_no_drift_when_identical(self, p):
        import json
        snap = {"action": "rescan_all", "settings": {"batch_size": "250", "generate_nfo": True}}
        task = _FakeTask(json.dumps(snap))
        current = {"batch_size": "250", "generate_nfo": True, "schedule_cron": "0 3 * * *"}
        assert p._settings_drift_keys(task, current) == []

    def test_detects_changed_value(self, p):
        import json
        snap = {"settings": {"append_tmdb_id_to_folder": False, "batch_size": "250"}}
        task = _FakeTask(json.dumps(snap))
        current = {"append_tmdb_id_to_folder": True, "batch_size": "250"}
        assert p._settings_drift_keys(task, current) == ["append_tmdb_id_to_folder"]

    def test_new_setting_not_in_snapshot_is_not_flagged(self, p):
        # A setting added by a plugin upgrade (absent from the old snapshot)
        # must not raise a false drift warning.
        import json
        snap = {"settings": {"batch_size": "250"}}
        task = _FakeTask(json.dumps(snap))
        current = {"batch_size": "250", "dedupe_movies_across_categories": True}
        assert p._settings_drift_keys(task, current) == []

    def test_malformed_kwargs_returns_empty(self, p):
        task = _FakeTask("{not valid json")
        assert p._settings_drift_keys(task, {"batch_size": "250"}) == []

    def test_schedule_prefixed_keys_ignored(self, p):
        import json
        snap = {"settings": {"batch_size": "250"}}
        task = _FakeTask(json.dumps(snap))
        # changing schedule_cron must NOT count as settings drift
        current = {"batch_size": "250", "schedule_cron": "0 4 * * *"}
        assert p._settings_drift_keys(task, current) == []


# ---------- _build_proxy_url (#6 / omit_stream_id) ----------

class TestBuildProxyUrl:
    def test_movie_includes_stream_id_by_default(self, p):
        url = p._build_proxy_url("http://d:9191", "movie", "abc-uuid", "615487")
        assert url == "http://d:9191/proxy/vod/movie/abc-uuid?stream_id=615487"

    def test_episode_includes_stream_id_by_default(self, p):
        url = p._build_proxy_url("http://d:9191", "episode", "ep-uuid", "42")
        assert url == "http://d:9191/proxy/vod/episode/ep-uuid?stream_id=42"

    def test_omit_flag_drops_stream_id_movie(self, p):
        url = p._build_proxy_url("http://d:9191", "movie", "abc-uuid", "615487", omit_stream_id=True)
        assert url == "http://d:9191/proxy/vod/movie/abc-uuid"

    def test_omit_flag_drops_stream_id_episode(self, p):
        url = p._build_proxy_url("http://d:9191", "episode", "ep-uuid", "42", omit_stream_id=True)
        assert url == "http://d:9191/proxy/vod/episode/ep-uuid"

    def test_missing_stream_id_drops_query_even_when_not_omitting(self, p):
        # No stream_id available -> can't pin, so no dangling "?stream_id=".
        assert p._build_proxy_url("http://d:9191", "movie", "u", None) == "http://d:9191/proxy/vod/movie/u"
        assert p._build_proxy_url("http://d:9191", "movie", "u", "") == "http://d:9191/proxy/vod/movie/u"


# ---------- language-prefix formats (v1.16.0, issue #3) ----------

class TestLanguagePrefixFormats:
    """The v1.16.0 expansion of _LANGUAGE_PREFIX_RE — pipe / bare-EN / bullet
    formats, with guards so real titles survive. Exercised through _clean_title
    (the public consumer)."""

    def test_pipe_any_code(self, p):
        assert p._clean_title("EN| Alita: Battle Angel 3D") == "Alita: Battle Angel 3D"
        assert p._clean_title("FR| Le Voyage") == "Le Voyage"
        assert p._clean_title("DE|Der Film") == "Der Film"

    def test_bare_space_en_only(self, p):
        assert p._clean_title("EN 27 Gone Too Soon") == "27 Gone Too Soon"
        assert p._clean_title("EN The Matrix") == "The Matrix"

    def test_bare_space_preserves_non_en_titles(self, p):
        # These must NOT be treated as language prefixes.
        assert p._clean_title("IT Chapter Two") == "IT Chapter Two"
        assert p._clean_title("UP (2009)") == "UP (2009)"
        assert p._clean_title("ED TV") == "ED TV"

    def test_bullet_wrapped(self, p):
        assert p._clean_title("▪️NL▪️ Some Movie") == "Some Movie"
        assert p._clean_title("▪MULTIG▪ Another Film") == "Another Film"

    def test_dash_still_works(self, p):
        assert p._clean_title("EN - Inception") == "Inception"
        assert p._clean_title("ENG - Inception") == "Inception"
        assert p._clean_title("FR -   Amélie") == "Amélie"

    def test_ac130_mi5_still_preserved(self, p):
        assert p._clean_title("AC-130") == "AC-130"
        assert p._clean_title("MI-5") == "MI-5"

    def test_no_prefix_unchanged(self, p):
        assert p._clean_title("The Matrix") == "The Matrix"


# ---------- _parse_category_filter (v1.16.0) ----------

class TestParseCategoryFilter:
    def test_empty_returns_empty_list(self, p):
        assert p._parse_category_filter("") == []
        assert p._parse_category_filter(None) == []
        assert p._parse_category_filter("   ") == []

    def test_single_prefix(self, p):
        assert p._parse_category_filter("[EN]") == ["[EN]"]

    def test_comma_separated_trimmed(self, p):
        assert p._parse_category_filter("[EN], [FR] , [DE]") == ["[EN]", "[FR]", "[DE]"]

    def test_drops_empty_segments(self, p):
        assert p._parse_category_filter("[EN],,, [FR] ,") == ["[EN]", "[FR]"]


# ---------- Webhook notifications (v1.17.0) ----------

class TestDetectWebhookFormat:
    def test_discord_url(self, p):
        assert p._detect_webhook_format("https://discord.com/api/webhooks/123/abc") == "discord"

    def test_discordapp_legacy_host(self, p):
        assert p._detect_webhook_format("https://discordapp.com/api/webhooks/123/abc") == "discord"

    def test_slack_url(self, p):
        assert p._detect_webhook_format("https://hooks.slack.com/services/T00/B00/xyz") == "slack"

    def test_unknown_host_is_generic(self, p):
        assert p._detect_webhook_format("https://example.com/hook") == "generic"

    def test_empty_url_is_generic(self, p):
        assert p._detect_webhook_format("") == "generic"


class TestWebhookStats:
    def test_generate_movies_result(self, p):
        result = {
            "status": "ok", "message": "ok", "total_in_db": 100, "scanned": 100,
            "created_strm": 5, "refreshed_strm": 0, "created_nfo": 5,
            "skipped": 95, "deduped": 0, "errors": 0,
        }
        stats, errors, total_changed = p._webhook_stats("generate_movies", result)
        assert ("Movies added", 5) in stats
        assert ("Movie NFOs written", 5) in stats
        assert ("Movies skipped (on disk)", 95) in stats
        # zero-valued stats are omitted
        assert not any(label == "Movies refreshed" for label, _ in stats)
        assert errors == 0
        # 'skipped' (already on disk) is informational, not a change, and is
        # excluded from the no-op gate — otherwise a fully-cached rerun would
        # still "count" as a change because it re-skipped 95 files.
        assert total_changed == 5 + 5

    def test_cleanup_result(self, p):
        result = {
            "status": "ok", "message": "ok",
            "deleted_strm": 12, "deleted_nfo": 12, "removed_dirs": 3,
            "preserved_dirs": 1, "errors": 0,
        }
        stats, errors, total_changed = p._webhook_stats("cleanup_movies", result)
        assert ("Folders removed", 3) in stats
        assert ("Folders preserved (user files)", 1) in stats
        assert errors == 0
        # preserved_dirs is informational (nothing was deleted there)
        assert total_changed == 12 + 12 + 3

    def test_rescan_all_merges_nested_movies_and_series(self, p):
        result = {
            "status": "ok", "message": "ok",
            "movies": {"created_strm": 3, "refreshed_strm": 2, "skipped": 1, "errors": 1},
            "series": {"episodes_created": 4, "series_processed": 2, "errors": 0},
        }
        stats, errors, total_changed = p._webhook_stats("rescan_all", result)
        assert ("Movies added", 3) in stats
        assert ("Movies refreshed", 2) in stats
        assert ("Episodes added", 4) in stats
        assert ("Series updated", 2) in stats
        assert errors == 1
        # movies' 'skipped': 1 is informational, excluded from the gate
        assert total_changed == 3 + 2 + 4 + 2

    def test_all_zero_gives_no_stats_and_no_errors(self, p):
        result = {"status": "ok", "message": "nothing to do", "created_strm": 0, "skipped": 0, "errors": 0}
        stats, errors, total_changed = p._webhook_stats("generate_movies", result)
        assert stats == []
        assert errors == 0
        assert total_changed == 0

    def test_fully_cached_rerun_is_a_no_op_despite_large_scan_counts(self, p):
        # A nightly cron rerun where every movie is already on disk still
        # reports a large 'total_in_db'/'scanned' — those must NOT make the
        # run look like it "changed" something, or webhook_notify_on_no_changes
        # would never actually suppress anything.
        result = {
            "status": "ok", "message": "already done", "total_in_db": 5000, "scanned": 5000,
            "created_strm": 0, "refreshed_strm": 0, "created_nfo": 0, "skipped": 5000, "errors": 0,
        }
        stats, errors, total_changed = p._webhook_stats("generate_movies", result)
        assert total_changed == 0
        assert errors == 0


class TestWebhookPayloads:
    def test_discord_payload_shape(self, p):
        payload = p._discord_webhook_payload("Title", "msg", [("Movies added", 3)], 0)
        embed = payload["embeds"][0]
        assert embed["title"] == "Title"
        assert embed["description"] == "msg"
        assert embed["fields"] == [{"name": "Movies added", "value": "3", "inline": True}]
        assert embed["color"] != 0xE74C3C  # not error-red when errors == 0

    def test_discord_payload_uses_red_on_errors(self, p):
        payload = p._discord_webhook_payload("Title", "msg", [], 2)
        assert payload["embeds"][0]["color"] == 0xE74C3C
        assert {"name": "Errors", "value": "2", "inline": True} in payload["embeds"][0]["fields"]

    def test_slack_payload_is_plain_text(self, p):
        payload = p._slack_webhook_payload("Title", "msg", [("Movies added", 3)], 0)
        assert "Title" in payload["text"]
        assert "msg" in payload["text"]
        assert "Movies added: 3" in payload["text"]

    def test_generic_payload_shape(self, p):
        payload = p._generic_webhook_payload("generate_movies", "Title", "msg", [("Movies added", 3)], 0)
        assert payload["action"] == "generate_movies"
        assert payload["stats"] == {"Movies added": 3}
        assert payload["errors"] == 0


class TestSendWebhook:
    def test_no_url_skips_network_call(self, p, monkeypatch):
        called = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
        p._send_webhook({"webhook_url": ""}, CapturingLogger(), "generate_movies", {"status": "ok", "created_strm": 5})
        assert called == []

    def test_error_result_skips_network_call(self, p, monkeypatch):
        called = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
        p._send_webhook(
            {"webhook_url": "https://example.com/hook"}, CapturingLogger(),
            "generate_movies", {"status": "error", "message": "boom"},
        )
        assert called == []

    def test_no_changes_skips_by_default(self, p, monkeypatch):
        called = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
        p._send_webhook(
            {"webhook_url": "https://example.com/hook"}, CapturingLogger(),
            "generate_movies", {"status": "ok", "created_strm": 0, "skipped": 0, "errors": 0},
        )
        assert called == []

    def test_no_changes_sent_when_opted_in(self, p, monkeypatch):
        class FakeResp:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *a): return False
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResp()
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        p._send_webhook(
            {"webhook_url": "https://example.com/hook", "webhook_notify_on_no_changes": True},
            CapturingLogger(), "generate_movies", {"status": "ok", "created_strm": 0, "skipped": 0, "errors": 0},
        )
        assert captured["url"] == "https://example.com/hook"
        assert captured["body"]["action"] == "generate_movies"

    def test_posts_discord_payload_for_discord_url(self, p, monkeypatch):
        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["content_type"] = req.get_header("Content-type")
            return FakeResp()
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        p._send_webhook(
            {"webhook_url": "https://discord.com/api/webhooks/1/abc"}, CapturingLogger(),
            "generate_movies", {"status": "ok", "message": "Wrote 5 new .strm files", "created_strm": 5, "errors": 0},
        )
        assert "embeds" in captured["body"]
        assert captured["content_type"] == "application/json"

    def test_webhook_failure_is_logged_not_raised(self, p, monkeypatch):
        def raising_urlopen(*a, **k):
            raise OSError("network unreachable")
        monkeypatch.setattr(urllib.request, "urlopen", raising_urlopen)
        logger = CapturingLogger()
        p._send_webhook(
            {"webhook_url": "https://example.com/hook"}, logger,
            "generate_movies", {"status": "ok", "created_strm": 5, "errors": 0},
        )
        assert any("Webhook delivery failed" in w for w in logger.warnings)

    def test_http_error_body_surfaced_in_log(self, p, monkeypatch):
        # Discord/Slack sit behind WAFs that return a bare 403 with no other
        # signal — surfacing code + reason + response body is the difference
        # between "something's wrong" and "here's exactly why".
        def raising_urlopen(*a, **k):
            raise urllib.error.HTTPError(
                "https://discord.com/api/webhooks/1/abc", 403, "Forbidden", None,
                io.BytesIO(b"Cloudflare blocked this request"),
            )
        monkeypatch.setattr(urllib.request, "urlopen", raising_urlopen)
        logger = CapturingLogger()
        p._send_webhook(
            {"webhook_url": "https://discord.com/api/webhooks/1/abc"}, logger,
            "generate_movies", {"status": "ok", "created_strm": 5, "errors": 0},
        )
        assert len(logger.warnings) == 1
        assert "403" in logger.warnings[0]
        assert "Forbidden" in logger.warnings[0]
        assert "Cloudflare blocked this request" in logger.warnings[0]

    def test_sets_custom_user_agent(self, p, monkeypatch):
        # urllib's default "Python-urllib/x.y" User-Agent is a known bot
        # signature that Cloudflare (in front of discord.com) blocks with a
        # bare 403 — this is the actual fix, not just an error-message nicety.
        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["user_agent"] = req.get_header("User-agent")
            return FakeResp()
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        p._send_webhook(
            {"webhook_url": "https://discord.com/api/webhooks/1/abc"}, CapturingLogger(),
            "generate_movies", {"status": "ok", "created_strm": 5, "errors": 0},
        )
        assert captured["user_agent"]
        assert "python-urllib" not in captured["user_agent"].lower()


# ---------- _dir_has_plugin_files (orphan-prune guardrail) ----------

class TestDirHasPluginFiles:
    def test_empty_dir_is_false(self, p, tmp_path):
        assert p._dir_has_plugin_files(str(tmp_path)) is False

    def test_nonexistent_is_false(self, p, tmp_path):
        assert p._dir_has_plugin_files(str(tmp_path / "nope")) is False

    def test_finds_nested_strm(self, p, tmp_path):
        d = tmp_path / "Movie (2020)"
        d.mkdir()
        (d / "Movie (2020).strm").write_text("x")
        assert p._dir_has_plugin_files(str(tmp_path)) is True

    def test_finds_nested_nfo(self, p, tmp_path):
        d = tmp_path / "Movie (2020)"
        d.mkdir()
        (d / "Movie (2020).nfo").write_text("<movie/>")
        assert p._dir_has_plugin_files(str(tmp_path)) is True

    def test_user_files_only_is_false(self, p, tmp_path):
        d = tmp_path / "Movie (2020)"
        d.mkdir()
        (d / "poster.jpg").write_text("x")
        (d / "Movie (2020).en.srt").write_text("subs")
        assert p._dir_has_plugin_files(str(tmp_path)) is False


# ---------- _prune_orphans_under (differential cleanup) ----------

class TestPruneOrphansUnder:
    """Tests _prune_orphans_under against real temp dirs (no DB).

    The keep-set is a set of realpaths, built here by hand exactly as the
    collectors would. Only files NOT in the set (and not under a protected
    subtree) should be removed; user files survive and empty dirs are cleaned
    bottom-up. Sanitisation lives in the collectors, so these tests are
    sanitisation-agnostic on purpose — they compare realpaths directly.
    """

    def _mk(self, path, text="http://x"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return os.path.realpath(str(path))

    def test_orphan_deleted_kept_preserved(self, p, tmp_path):
        log = CapturingLogger()
        keep_strm = self._mk(tmp_path / "Kept (2020)" / "Kept (2020).strm")
        keep_nfo = self._mk(tmp_path / "Kept (2020)" / "Kept (2020).nfo", "<movie/>")
        # Orphaned movie folder — neither file in the keep set.
        self._mk(tmp_path / "Gone (2019)" / "Gone (2019).strm")
        self._mk(tmp_path / "Gone (2019)" / "Gone (2019).nfo", "<movie/>")

        r = p._prune_orphans_under(str(tmp_path), {keep_strm, keep_nfo}, set(), log)

        assert r["deleted_strm"] == 1
        assert r["deleted_nfo"] == 1
        assert r["removed_dirs"] == 1  # the Gone folder
        assert (tmp_path / "Kept (2020)" / "Kept (2020).strm").exists()
        assert (tmp_path / "Kept (2020)" / "Kept (2020).nfo").exists()
        assert not (tmp_path / "Gone (2019)").exists()
        assert tmp_path.exists()  # root preserved

    def test_kept_nfo_survives(self, p, tmp_path):
        # Collectors always add both the .strm and its sidecar .nfo, so a valid
        # NFO next to a kept movie must never be treated as an orphan.
        log = CapturingLogger()
        strm = self._mk(tmp_path / "Kept (2020)" / "Kept (2020).strm")
        nfo = self._mk(tmp_path / "Kept (2020)" / "Kept (2020).nfo", "<movie/>")
        r = p._prune_orphans_under(str(tmp_path), {strm, nfo}, set(), log)
        assert r["deleted_nfo"] == 0
        assert (tmp_path / "Kept (2020)" / "Kept (2020).nfo").exists()

    def test_user_files_preserved(self, p, tmp_path):
        log = CapturingLogger()
        # Orphaned .strm sharing a folder with a user-added poster.
        self._mk(tmp_path / "Gone (2019)" / "Gone (2019).strm")
        poster = tmp_path / "Gone (2019)" / "poster.jpg"
        poster.write_text("img")
        r = p._prune_orphans_under(str(tmp_path), set(), set(), log)
        assert r["deleted_strm"] == 1
        assert r["removed_dirs"] == 0  # folder kept — poster still inside
        assert r["preserved_dirs"] >= 1
        assert poster.exists()

    def test_protected_subtree_untouched(self, p, tmp_path):
        log = CapturingLogger()
        protected_dir = tmp_path / "MysterySeries (2021)"
        # Episode not in keep set, but under a protected series folder.
        self._mk(protected_dir / "Season 01" / "MysterySeries - S01E01.strm")
        r = p._prune_orphans_under(
            str(tmp_path), set(), {os.path.realpath(str(protected_dir))}, log,
        )
        assert r["deleted_strm"] == 0
        assert r["protected_files"] == 1
        assert (protected_dir / "Season 01" / "MysterySeries - S01E01.strm").exists()

    def test_protected_prefix_does_not_match_sibling(self, p, tmp_path):
        # A protected 'Show' folder must not accidentally protect 'Show 2'.
        log = CapturingLogger()
        protected_dir = tmp_path / "Show"
        self._mk(protected_dir / "keep.strm")
        self._mk(tmp_path / "Show 2" / "orphan.strm")
        r = p._prune_orphans_under(
            str(tmp_path), set(), {os.path.realpath(str(protected_dir))}, log,
        )
        assert r["deleted_strm"] == 1  # only Show 2's orphan
        assert (protected_dir / "keep.strm").exists()
        assert not (tmp_path / "Show 2").exists()

    def test_series_episode_prune_keeps_current_removes_dropped(self, p, tmp_path):
        log = CapturingLogger()
        series = tmp_path / "Show (2020)"
        keep_ep = self._mk(series / "Season 01" / "Show - S01E01.strm")
        keep_epnfo = self._mk(series / "Season 01" / "Show - S01E01.nfo", "<ep/>")
        keep_tv = self._mk(series / "tvshow.nfo", "<tvshow/>")
        # Dropped episode — no longer upstream.
        self._mk(series / "Season 01" / "Show - S01E99.strm")
        keep = {keep_ep, keep_epnfo, keep_tv}
        r = p._prune_orphans_under(str(tmp_path), keep, set(), log)
        assert r["deleted_strm"] == 1
        assert (series / "Season 01" / "Show - S01E01.strm").exists()
        assert (series / "tvshow.nfo").exists()
        assert not (series / "Season 01" / "Show - S01E99.strm").exists()

    def test_empty_season_folder_removed(self, p, tmp_path):
        log = CapturingLogger()
        series = tmp_path / "Show (2020)"
        self._mk(series / "tvshow.nfo", "<tvshow/>")
        # A whole season went away.
        self._mk(series / "Season 05" / "Show - S05E01.strm")
        keep = {os.path.realpath(str(series / "tvshow.nfo"))}
        r = p._prune_orphans_under(str(tmp_path), keep, set(), log)
        assert r["deleted_strm"] == 1
        assert not (series / "Season 05").exists()
        assert (series / "tvshow.nfo").exists()  # series folder itself preserved

    def test_nonexistent_root_no_error(self, p, tmp_path):
        log = CapturingLogger()
        r = p._prune_orphans_under(str(tmp_path / "nope"), set(), set(), log)
        assert r["errors"] == 0
        assert r["deleted_strm"] == 0

    def test_root_never_removed_even_when_emptied(self, p, tmp_path):
        log = CapturingLogger()
        self._mk(tmp_path / "Gone (2019)" / "Gone (2019).strm")
        r = p._prune_orphans_under(str(tmp_path), set(), set(), log)
        assert tmp_path.exists()
        assert r["removed_dirs"] == 1  # only the Gone folder, not the root


# ---------- prune action wiring ----------

class TestPruneActionsWebhookRegistered:
    def test_prune_actions_are_notify_worthy(self, p):
        assert "prune_movies" in p._WEBHOOK_ACTION_LABELS
        assert "prune_series" in p._WEBHOOK_ACTION_LABELS

    def test_prune_actions_not_schedule_targets(self, p):
        # Prune isn't offered in the schedule dropdown (like cleanup); the
        # prune-on-rescan toggle covers scheduled pruning instead.
        targets = p._valid_schedule_targets()
        assert "prune_movies" not in targets
        assert "prune_series" not in targets

    def test_prune_stat_keys_are_in_webhook_labels(self, p):
        # The counts prune emits must be renderable by the webhook summary.
        labelled = {key for key, _label in p._WEBHOOK_STAT_LABELS}
        for key in ("deleted_strm", "deleted_nfo", "removed_dirs", "preserved_dirs"):
            assert key in labelled
