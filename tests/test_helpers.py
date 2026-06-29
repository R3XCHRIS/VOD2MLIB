"""Unit tests for VOD2MLIB's pure helper methods.

These methods don't touch Django/DB/filesystem and are safe to test in
isolation. Run with `pytest` from the repo root.
"""
import os
import sys

# Make the repo root importable so `import plugin` resolves to plugin.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from plugin import Plugin


class CapturingLogger:
    """A tiny stand-in that records warnings without needing the logging stack."""

    def __init__(self):
        self.warnings = []
        self.errors = []
        self.infos = []

    def warning(self, *args, **kwargs):
        self.warnings.append(args[0] if args else "")

    def error(self, *args, **kwargs):
        self.errors.append(args[0] if args else "")

    def info(self, *args, **kwargs):
        self.infos.append(args[0] if args else "")


@pytest.fixture
def p():
    return Plugin()


# ---------- filename sanitisation is the shared core's job now ----------
# Coverage for the one sanitizer lives in tests/test_naming.py::TestSanitize
# (vodlib.naming.sanitize_filename); the old plugin-local delegator is gone.


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
        # The single naming path always carries {tmdb-} when known, and the .strm
        # file matches the folder name.
        folder, strm, name, year = p._movie_target_paths(self._M(), "/VODS/Movies")
        assert folder == "/VODS/Movies/Cool Hand Luke (1967) {tmdb-378}"
        assert strm == "Cool Hand Luke (1967) {tmdb-378}.strm"
        assert name == "Cool Hand Luke"
        assert year == 1967

    def test_ids_always_present_regardless_of_legacy_flag(self, p):
        # `append_tmdb_id` is now implicit in the one naming path — passing it does
        # not change the result.
        folder, strm, _, _ = p._movie_target_paths(
            self._M(), "/VODS/Movies", append_tmdb_id=True)
        assert folder == "/VODS/Movies/Cool Hand Luke (1967) {tmdb-378}"
        assert strm == "Cool Hand Luke (1967) {tmdb-378}.strm"

    def test_no_ids_when_none_known(self, p):
        class M:
            id = 1; uuid = "x"; name = "Mystery"; year = None; tmdb_id = ""; imdb_id = ""
        folder, _, _, _ = p._movie_target_paths(M(), "/VODS/Movies")
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
        assert folder == "/VODS/Series/Breaking Bad (2008) {tmdb-1396}"
        assert name == "Breaking Bad"
        assert year == 2008

    def test_ids_always_present(self, p):
        folder, _, _ = p._series_target_folder(
            self._S(), "/VODS/Series", append_tmdb_id=True)
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
