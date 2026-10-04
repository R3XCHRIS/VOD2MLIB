"""Independent, model-only metadata rules and bounded catalogue projections."""

import math
import re
from dataclasses import dataclass
from itertools import islice

try:
    from .inventory import BATCH_SIZE, LOOKUP_BATCH_SIZE
except ImportError:
    from inventory import BATCH_SIZE, LOOKUP_BATCH_SIZE


def genre_names(value):
    # Only commas delimit lists. Ampersands, slashes and hyphens belong to names.
    return frozenset(s.strip().casefold() for s in (value or '').split(',') if s.strip())


def title_pattern(settings, key):
    value = settings.get(key)
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str):
        raise ValueError(f'{key} must be a regular expression')
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as error:
        raise ValueError(f'{key} is not a valid regular expression: {error}') from error


def score(value):
    try:
        result = float(value) if not isinstance(value, bool) else float('nan')
        return result if math.isfinite(result) and 0 < result <= 10 else None
    except (TypeError, ValueError, OverflowError):
        return None


def year(value):
    text = str(value).strip()
    return int(text) if re.fullmatch(r'[0-9]+', text) and int(text) > 0 else None


@dataclass(frozen=True)
class Rules:
    minimum_score: float | None = None
    earliest_year: int | None = None
    latest_year: int | None = None
    missing: str = 'keep'
    include: frozenset = frozenset()
    exclude: frozenset = frozenset()
    title_include: re.Pattern | None = None
    title_exclude: re.Pattern | None = None

    def evaluate(self, rating=None, release_year=None, genre=None, title=None):
        rejected, unknown = [], []
        if self.minimum_score is not None:
            actual = score(rating)
            if actual is None:
                unknown.append('score')
                if self.missing == 'reject': rejected.append('score')
            elif actual < self.minimum_score:
                rejected.append('score')
        if self.earliest_year is not None or self.latest_year is not None:
            actual = year(release_year)
            if actual is None:
                unknown.append('year')
                if self.missing == 'reject': rejected.append('year')
            elif ((self.earliest_year is not None and actual < self.earliest_year)
                  or (self.latest_year is not None and actual > self.latest_year)):
                rejected.append('year')
        if self.include or self.exclude:
            actual = genre_names(genre)
            if not actual:
                unknown.append('genre')
                if self.missing == 'reject': rejected.append('genre')
            elif actual & self.exclude or (self.include and not actual & self.include):
                rejected.append('genre')
        if self.title_include or self.title_exclude:
            if title is None or not title.strip():
                unknown.append('title')
                if self.missing == 'reject': rejected.append('title')
            elif ((self.title_exclude and self.title_exclude.search(title))
                  or (self.title_include and not self.title_include.search(title))):
                rejected.append('title')
        return not rejected, tuple(rejected), tuple(unknown)


def configuration(settings):
    rules = {}
    for kind in ('movie', 'series'):
        values = {}
        for suffix in ('minimum_score', 'earliest_year', 'latest_year'):
            key = f'{kind}_{suffix}'
            raw = settings.get(key)
            if raw is None or str(raw).strip() == '':
                values[suffix] = None
                continue
            if suffix == 'minimum_score':
                try:
                    number = float(raw)
                except (TypeError, ValueError, OverflowError):
                    number = float('nan')
                if isinstance(raw, bool) or not math.isfinite(number) or not 0 <= number <= 10:
                    raise ValueError(f'{key} must be a numeric score within 0–10')
            else:
                number = year(raw)
                if number is None:
                    raise ValueError(f'{key} must be a positive integer year')
            values[suffix] = number
        low, high = values['earliest_year'], values['latest_year']
        if low is not None and high is not None and low > high:
            raise ValueError(f'{kind} earliest year must not exceed latest year')
        policy = settings.get(f'{kind}_missing_metadata') or 'keep'
        if policy not in ('keep', 'reject'):
            raise ValueError(f'{kind}_missing_metadata must be keep or reject')
        rules[kind] = Rules(**values, missing=policy,
            include=genre_names(settings.get('series_genre_include')) if kind == 'series' else frozenset(),
            exclude=genre_names(settings.get('series_genre_exclude')) if kind == 'series' else frozenset(),
            title_include=title_pattern(settings, f'{kind}_title_include'),
            title_exclude=title_pattern(settings, f'{kind}_title_exclude'))
    return rules


SECTION = dict(
    id='_section_metadata_filters',
    label='[METADATA FILTERS]',
    type='info',
    description='Filter movies and series independently by score, year, and title regex, and series by genre, '
                'using Dispatcharr metadata. Blank rules are disabled; unknown metadata is kept '
                'by default. The next generation run removes verified generated STRMs and NFOs '
                'that fail current filters, while preserving edited files. Scheduled runs use '
                'current saved filters automatically.',
)

FIELDS = []
for _kind in ('movie', 'series'):
    for _suffix, _label, _help in (
        ('minimum_score', 'Minimum Score', 'Inclusive numeric score 0–10. Blank disables. Zero or invalid model ratings are unknown.'),
        ('earliest_year', 'Earliest Year', 'Inclusive positive integer year. Blank disables.'),
        ('latest_year', 'Latest Year', 'Inclusive positive integer year. Blank disables.'),
    ):
        FIELDS.append(dict(id=f'{_kind}_{_suffix}', label=f'{_label} ({_kind.title()})',
                           type='string', default='', help_text=_help))
    FIELDS.append(dict(id=f'{_kind}_missing_metadata', label=f'Missing Metadata ({_kind.title()})',
        type='select', default='keep', options=[{'value': 'keep', 'label': 'Keep unknowns'},
        {'value': 'reject', 'label': 'Reject unknowns'}], help_text='Applies only to enabled metadata rules. Uses Dispatcharr model metadata.'))
    for _suffix in ('include', 'exclude'):
        FIELDS.append(dict(id=f'{_kind}_title_{_suffix}', label=f'Title {_suffix.title()} Regex ({_kind.title()})',
            type='string', default='',
            help_text=r'Case-insensitive regular expression searched in the original Dispatcharr title before cleanup. '
                      r'Use ^\s*(AF|AR)\s*[-:|]\s* for provider prefixes or ^\s*\[(AF|AR)\]\s* for bracketed tags. '
                      r'Include requires a match; exclude rejects a match and wins over include. Blank disables.'))
for _suffix in ('include', 'exclude'):
    FIELDS.append(dict(id=f'series_genre_{_suffix}', label=f'Genre {_suffix.title()} (Series)',
        type='string', default='', help_text='Comma-separated complete genre names, case-insensitive. Compound names remain intact. Any include qualifies; any exclude rejects. Blank disables.'))

SETTING_KEYS = tuple(field['id'] for field in FIELDS)


def metadata_fields(kind):
    fields = [f'{kind}__rating', f'{kind}__year']
    if kind == 'series': fields.append('series__genre')
    fields.append(f'{kind}__name')
    return fields


def evaluate_metadata(rules, kind, values):
    return rules.evaluate(values[0], values[1], values[2] if kind == 'series' else None, values[-1])


def passing_relations(query, kind, rules):
    """Hydrate passing relations only, in original order and bounded groups."""
    iterator = query.values_list('id', *metadata_fields(kind)).iterator(chunk_size=BATCH_SIZE)
    while True:
        rows = list(islice(iterator, BATCH_SIZE))
        if not rows: return
        ids = [row[0] for row in rows if evaluate_metadata(rules, kind, row[1:])[0]]
        for offset in range(0, len(ids), LOOKUP_BATCH_SIZE):
            group = ids[offset:offset + LOOKUP_BATCH_SIZE]
            models = {rel.id: rel for rel in query.filter(id__in=group)}
            for pk in group:
                if pk in models: yield models[pk]


def catalogue_counts(query, kind, rules):
    counts = dict(eligible=0, passing=0, rejected_score=0, rejected_year=0,
                  rejected_genre=0, rejected_title=0, retained_unknown=0)
    # Unique model IDs, not provider relations. SQL distinct bounds Python memory.
    rows = query.order_by().values_list(f'{kind}_id', *metadata_fields(kind)).distinct()
    for row in rows.iterator(chunk_size=BATCH_SIZE):
        passed, rejected, unknown = evaluate_metadata(rules, kind, row[1:])
        counts['eligible'] += 1
        counts['passing'] += int(passed)
        counts['retained_unknown'] += int(passed and bool(unknown))
        for reason in rejected: counts[f'rejected_{reason}'] += 1
    return counts
