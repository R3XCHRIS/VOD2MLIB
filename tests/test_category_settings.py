"""Regression checks for retiring the plugin's category prefix settings."""
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugin import Plugin


def test_legacy_category_settings_are_not_advertised():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "plugin.json").read_text(encoding="utf-8"))
    for fields in (Plugin.fields, manifest["fields"]):
        ids = {field["id"] for field in fields}
        assert not ids.intersection({"category_filter", "category_exclude"})


@pytest.mark.parametrize("generator, message", [
    ("_generate_movies", "No movies found to process"),
    ("_generate_series", "No series found"),
])
def test_saved_legacy_filters_do_not_change_empty_catalogue_result(monkeypatch, generator, message):
    class EmptyQuery:
        def select_related(self, *args):
            return self

        def count(self):
            return 0

    # Native eligibility is validated separately against Dispatcharr's models.
    # This regression focuses on saved settings and the public action result.
    model = SimpleNamespace(objects=EmptyQuery())
    monkeypatch.setitem(sys.modules, "apps.vod.models", SimpleNamespace(
        M3UMovieRelation=model, M3USeriesRelation=model,
    ))
    plugin = Plugin()
    monkeypatch.setattr(plugin, "_eligible_vod_relations", lambda query, vod_type: query)
    settings = {
        "dispatcharr_url": "http://dispatcharr.test:9191",
        "category_filter": "DoesNotExist",
        "category_exclude": "Common,Second",
    }
    result = getattr(plugin, generator)(settings, logging.getLogger(__name__))
    assert result["status"] == "ok"
    assert result["message"] == message
    assert "filtered_out" not in result
