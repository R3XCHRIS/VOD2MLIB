"""Rule semantics and generation/reconciliation safety."""
import json
import logging
from pathlib import Path

import pytest

from metadata_filters import Rules, configuration, catalogue_counts, passing_relations, FIELDS
from plugin import Plugin
from tests.test_reconciliation import library, media, relation, Query


@pytest.mark.parametrize('raw', [None, '', '0', 0, 'bad', 'PG-13', 'NaN', 'inf', '-1', '10.1', True])
def test_unknown_scores(raw):
    assert Rules(minimum_score=6).evaluate(raw)[0]
    assert not Rules(minimum_score=6, missing='reject').evaluate(raw)[0]
    assert Rules(missing='reject').evaluate(raw)[0]


def test_independence_inclusive_bounds_and_and():
    rules = configuration(dict(movie_minimum_score='7', series_earliest_year='2000', series_latest_year='2020'))
    assert rules['movie'].evaluate('7', 1990)[0]
    assert not rules['movie'].evaluate('6.9', 2020)[0]
    assert rules['series'].evaluate('1', 2000)[0]
    assert rules['series'].evaluate('1', 2020)[0]
    assert not rules['series'].evaluate('10', 2021)[0]
    assert not Rules(7, 2000, 2020).evaluate('8', 1999)[0]


@pytest.mark.parametrize('year', [None, '', 0, -1, 'no', '2000.5', True])
def test_unknown_years(year):
    assert Rules(earliest_year=2000).evaluate(release_year=year)[0]
    assert not Rules(earliest_year=2000, missing='reject').evaluate(release_year=year)[0]


def test_complete_genres_and_exclusion_precedence():
    rules = configuration(dict(series_genre_include='Drama,Action & Adventure', series_genre_exclude='Reality'))['series']
    assert rules.evaluate(genre='ACTION & ADVENTURE,Comedy')[0]
    assert not rules.evaluate(genre='Action')[0]
    assert not rules.evaluate(genre='Drama,Reality')[0]
    assert rules.evaluate(genre=None)[0]
    assert not configuration(dict(series_genre_exclude='Drama', series_missing_metadata='reject'))['series'].evaluate()[0]
    assert configuration(dict(series_genre_include='Drama'))['movie'].evaluate()[0]


@pytest.mark.parametrize('settings', [
    {'movie_minimum_score': '-1'}, {'series_minimum_score': '11'}, {'movie_minimum_score': 'NaN'},
    {'movie_minimum_score': True}, {'movie_earliest_year': '0'}, {'series_latest_year': '2020.5'},
    {'series_earliest_year': '2021', 'series_latest_year': '2020'}, {'movie_missing_metadata': 'bad'},
])
def test_invalid_config_precedes_reconciliation(library, monkeypatch, settings):
    monkeypatch.setattr('reconciliation.Reconciliation.prepare', lambda *_: pytest.fail('reconciliation ran'))
    assert library.run(**settings)['status'] == 'error'
    assert not Path(library.settings['root_folder']).exists()


def test_fields_match_and_inactive_defaults():
    manifest = json.loads(Path('plugin.json').read_text(encoding='utf-8'))
    for field in FIELDS:
        assert field in Plugin.fields and field in manifest['fields']
    defaults = {field['id']: field['default'] for field in FIELDS}
    assert all(rule == Rules() for rule in configuration(defaults).values())


def test_movies_reject_before_batch_and_reevaluate(library):
    low, high = media(1, rating='2'), media(2, rating='8')
    library.rows['movies'].extend([relation(low), relation(high)])
    result = library.run(movie_minimum_score='7', batch_size='1')
    assert result['created_strm'] == 1
    assert not list(Path(library.settings['root_folder']).glob('Title 1*'))
    low.rating = '9'
    assert library.run(movie_minimum_score='7')['created_strm'] == 1
    assert library.run(movie_minimum_score='7')['reconciliation']['generation_unchanged'] == 2
    assert library.run(movie_minimum_score='6')['reconciliation']['generation_candidates'] == 2


def test_series_reject_before_batch_and_episode_fetch(library):
    for id, genre in [(10, 'Reality'), (20, 'Action & Adventure')]:
        show = media(id, genre=genre, rating='8')
        library.rows['series'].append(relation(show, 'series'))
        library.rows['episodes'].append(relation(media(id+1, series=show, season_number=1, episode_number=1), 'episode'))
    result = library.run('generate_series', series_genre_include='Action & Adventure', series_batch_size='1', refresh_existing=True)
    assert result['episodes_created'] == 1
    assert len(library.calls) == 0
    assert not list(Path(library.settings['series_root_folder']).glob('Title 10*'))


def test_filtered_existing_titles_are_still_present_upstream(library):
    library.rows['movies'].append(relation(media(1, rating='2')))
    assert library.run()['created_strm'] == 1
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    result = library.run('rescan_all', movie_minimum_score='8', m3u_cleanup_enabled=True)
    assert result['reconciliation']['deleted'] == 1
    assert result['reconciliation']['filter_deleted'] == 1
    assert not path.exists()
    assert len(library.rows['movies']) == 1


def test_preview_matches_candidates_and_counts_unknown(monkeypatch):
    # Add SQL DISTINCT behavior to the in-memory query used by integration tests.
    def distinct(self):
        return Query(list(dict.fromkeys(self.rows)))
    monkeypatch.setattr(Query, 'distinct', distinct, raising=False)
    rows = [relation(media(1, rating='2')), relation(media(2, rating='8')), relation(media(3, rating=''))]
    rows.append(relation(rows[1].movie))
    query = Query(rows)
    rules = Rules(minimum_score=7)
    counts = catalogue_counts(query, 'movie', rules)
    passing = {rel.movie.id for rel in passing_relations(query, 'movie', rules)}
    assert counts == dict(eligible=3, passing=2, rejected_score=1, rejected_year=0, rejected_genre=0, rejected_title=0, retained_unknown=1)
    assert passing == {2, 3}


def test_hydration_is_bounded_and_only_for_passing_rows(monkeypatch):
    import metadata_filters as filters
    monkeypatch.setattr(filters, 'BATCH_SIZE', 3)
    monkeypatch.setattr(filters, 'LOOKUP_BATCH_SIZE', 2)
    calls = []
    original = Query.filter
    def recorded(self, **kwargs):
        calls.append(kwargs['id__in'])
        return original(self, **kwargs)
    monkeypatch.setattr(Query, 'filter', recorded)
    rows = [relation(media(i, rating='2' if i == 2 else '8')) for i in range(7)]
    passing = list(passing_relations(Query(rows), 'movie', Rules(minimum_score=7)))
    assert [rel.movie.id for rel in passing] == [0, 1, 3, 4, 5, 6]
    assert all(len(group) <= 2 for group in calls)
    assert rows[2].id not in [pk for group in calls for pk in group]


def test_series_metadata_and_settings_invalidate_episode_decisions(library):
    show = media(10, rating='8', genre='Drama')
    library.rows['series'].append(relation(show, 'series'))
    library.rows['episodes'].append(relation(media(11, series=show, season_number=1, episode_number=1), 'episode'))
    assert library.run('generate_series', refresh_existing=True)['episodes_created'] == 1
    assert library.run('generate_series', refresh_existing=True)['reconciliation']['generation_unchanged'] == 1
    show.rating = '9'
    assert library.run('generate_series', refresh_existing=True)['reconciliation']['generation_unchanged'] == 0
    assert library.run('generate_series', refresh_existing=True, series_minimum_score='7')['reconciliation']['generation_unchanged'] == 0


@pytest.mark.parametrize('title', ['AF - Yard Palava', 'ar: Title', ' AR|Title', 'AR - +Title'])
def test_title_regex_excludes_provider_variants(title):
    rules = configuration({'series_title_exclude': r'^\s*(AF|AR)\s*[-:|]\s*'})['series']
    assert rules.evaluate(title=title) == (False, ('title',), ())
    assert rules.evaluate(title='AC-130')[0]
    assert rules.evaluate(title='The AR - Story')[0]


def test_title_include_exclude_independent_of_genre_and_media_type():
    rules = configuration({'series_title_include': r'^\[(EN|AR)\]',
                           'series_title_exclude': r'^\[AR\]', 'series_genre_include': 'Drama,Comedy'})
    assert rules['series'].evaluate(genre='Comedy', title='[EN] Title')[0]
    assert not rules['series'].evaluate(genre='Drama', title='[AR] Title')[0]
    assert not rules['series'].evaluate(genre='Reality', title='[EN] Title')[0]
    assert not rules['series'].evaluate(genre='Drama', title='Title')[0]
    assert rules['movie'].evaluate(title='[AR] Title')[0]


@pytest.mark.parametrize('kind', ['movie', 'series'])
@pytest.mark.parametrize('suffix', ['include', 'exclude'])
def test_invalid_title_pattern_precedes_reconciliation(library, monkeypatch, kind, suffix):
    monkeypatch.setattr('reconciliation.Reconciliation.prepare', lambda *_: pytest.fail('reconciliation ran'))
    key = f'{kind}_title_{suffix}'
    result = library.run(**{key: '['})
    assert result['status'] == 'error' and key in result['message']
    assert not Path(library.settings['root_folder']).exists()


def test_title_patterns_preserve_commas_whitespace_and_unicode():
    rules = configuration({'series_title_include': r'^A{1,2}, '})['series']
    assert rules.evaluate(title='AA, Title')[0]
    assert not rules.evaluate(title='AA,Title')[0]
    rules = configuration({'series_title_include': '^\u0627\u0644'})['series']
    assert rules.evaluate(title='\u0627\u0644\u0639\u0646\u0648\u0627\u0646')[0]


@pytest.mark.parametrize('title', [None, '', '   '])
def test_unknown_titles_only_use_policy_when_enabled(title):
    assert configuration({'series_title_include': 'Drama'})['series'].evaluate(title=title)[0]
    assert not configuration({'series_title_include': 'Drama', 'series_missing_metadata': 'reject'})['series'].evaluate(title=title)[0]
    assert Rules(missing='reject').evaluate(title=title)[0]


def test_title_filters_precede_movies_batch_and_recheck_name(library):
    a, b = media(1, name='AR - Title'), media(2, name='Title 2')
    library.rows['movies'].extend([relation(a), relation(b)])
    assert library.run(movie_title_exclude=r'^AR\s*-', batch_size='1')['created_strm'] == 1
    assert not list(Path(library.settings['root_folder']).glob('Title (2000)*'))
    a.name = 'Title 1'
    assert library.run(movie_title_exclude=r'^AR\s*-')['created_strm'] == 1
    assert library.run(movie_title_exclude=r'^AR\s*-')['reconciliation']['generation_unchanged'] == 2
    assert library.run(movie_title_exclude=r'^ZZ\s*-')['reconciliation']['generation_candidates'] == 2


def test_title_filters_precede_series_fetch_and_reject_before_cleanup(library):
    for id, name in [(10, 'AR - Title 10'), (20, 'Title 20')]:
        show = media(id, name=name)
        library.rows['series'].append(relation(show, 'series'))
        library.rows['episodes'].append(relation(media(id+1, series=show, season_number=1, episode_number=1), 'episode'))
    result = library.run('generate_series', series_title_exclude=r'^AR\s*-', series_batch_size='1', refresh_existing=True)
    assert result['episodes_created'] == 1 and len(library.calls) == 0
    assert library.run('generate_series')['episodes_created'] == 1
    paths = list(Path(library.settings['series_root_folder']).rglob('*.strm'))
    result = library.run('rescan_all', series_title_exclude='.*', m3u_cleanup_enabled=True)
    assert result['reconciliation']['filter_deleted'] == 2
    assert not any(path.exists() for path in paths)


def test_title_snapshot_matches_generation_candidates(monkeypatch):
    monkeypatch.setattr(Query, 'distinct', lambda self: Query(list(dict.fromkeys(self.rows))), raising=False)
    rows = [relation(media(1, name='AR - Title')), relation(media(2, name='Title 2')), relation(media(3, name=''))]
    rules = configuration({'movie_title_exclude': '^AR'})['movie']
    counts = catalogue_counts(Query(rows), 'movie', rules)
    passing = list(passing_relations(Query(rows), 'movie', rules))
    assert counts['passing'] == len(passing) == 2
    assert counts['rejected_title'] == 1 and counts['retained_unknown'] == 1


def test_genre_lists_keep_regex_characters_literal():
    rules = configuration({'series_genre_include': 'Drama+,Sci-Fi (TV)'})['series']
    assert rules.evaluate(genre='SCI-FI (TV)')[0]
    assert rules.evaluate(genre='Drama+')[0]
    assert not rules.evaluate(genre='Drama')[0]
    assert not configuration({'series_genre_include': 'Drama|Comedy'})['series'].evaluate(genre='Drama')[0]


def test_series_title_change_invalidates_episode_decisions(library):
    show = media(10, name='EN - Title 10')
    library.rows['series'].append(relation(show, 'series'))
    library.rows['episodes'].append(relation(media(11, series=show, season_number=1, episode_number=1), 'episode'))
    assert library.run('generate_series', refresh_existing=True, series_title_include='Title')['episodes_created'] == 1
    assert library.run('generate_series', refresh_existing=True, series_title_include='Title')['reconciliation']['generation_unchanged'] == 1
    # Same cleaned folder and episode title, changed raw series title.
    show.name = 'Title 10'
    assert library.run('generate_series', refresh_existing=True, series_title_include='Title')['reconciliation']['generation_unchanged'] == 0
