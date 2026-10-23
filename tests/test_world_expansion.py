"""The world expansion (2026-10-23): every searchable country, by population.

Three things changed with it and each can fail silently: where a search runs
(the city's country, not its language), what it may cost per day, and which
order the worker walks ~200 countries in.
"""
from pathlib import Path

import pytest
import yaml

from scraper import countries as countries_mod
from scraper.search import DataForSEOClient, SearchBudget, SearchQuotaError, SearchUnavailableError

ROOT = Path(__file__).resolve().parent.parent


class _Capture:
    """Stands in for httpx.AsyncClient: records the task_post body, then fails
    the call so nothing is polled."""
    is_closed = False

    def __init__(self, sink, cost=0.0006):
        self.sink, self.cost = sink, cost

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json, headers):
        self.sink.append(json)

        class _R:
            status_code = 200

            def json(_self):
                return {"status_code": 20000, "cost": self.cost,
                        "tasks": [{"status_code": 40501, "status_message": "stop",
                                   "id": "t"}]}
        return _R()


@pytest.fixture
def capture(monkeypatch):
    sink: list = []
    monkeypatch.setattr("scraper.search.httpx.AsyncClient",
                        lambda **kw: _Capture(sink))
    monkeypatch.setattr("scraper.search._PENDING_TASKS", {})
    return sink


@pytest.mark.asyncio
async def test_the_search_runs_from_the_citys_country_not_its_language(capture):
    """By language, every Spanish-speaking city searched from Spain."""
    client = DataForSEOClient("l", "p", mode="standard", rate_limit_seconds=0)
    with pytest.raises(SearchUnavailableError):
        await client.search("club de baile Monterrey", locale="es", country="Mexico")
    task = capture[0][0]
    assert task["location_code"] == countries_mod.dataforseo_location("Mexico") == 2484
    assert task["language_code"] == "es"


@pytest.mark.asyncio
async def test_chinese_is_sent_as_a_language_dataforseo_has(capture):
    client = DataForSEOClient("l", "p", mode="standard", rate_limit_seconds=0)
    with pytest.raises(SearchUnavailableError):
        await client.search("合唱团 成都", locale="zh", country="China")
    assert capture[0][0]["language_code"] == "zh-CN"


@pytest.mark.asyncio
async def test_the_daily_budget_stops_paid_searches(tmp_path, capture):
    from scraper.db import init_db

    db = tmp_path / "t.db"
    init_db(db)
    budget = SearchBudget(db, daily_usd=0.001)
    client = DataForSEOClient("l", "p", mode="standard", rate_limit_seconds=0,
                              budget=budget)
    with pytest.raises(SearchUnavailableError):
        await client.search("choir Lagos", locale="en", country="Nigeria")
    assert budget.spent_today() == pytest.approx(0.0006)
    with pytest.raises(SearchUnavailableError):
        await client.search("choir Kano", locale="en", country="Nigeria")
    # Two tasks charged → over the ceiling: the third is never posted.
    with pytest.raises(SearchQuotaError, match="daily budget"):
        await client.search("choir Abuja", locale="en", country="Nigeria")
    assert len(capture) == 2


def test_no_ceiling_means_none_and_zero_means_no_searches(tmp_path):
    from scraper.db import init_db

    db = tmp_path / "t.db"
    init_db(db)
    SearchBudget(db, daily_usd=None).check()
    with pytest.raises(SearchQuotaError):
        SearchBudget(db, daily_usd=0).check()


def test_shipped_settings_set_a_search_ceiling():
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
    assert settings["search"]["daily_budget_usd"] == 1.0
    assert settings["search"]["core_queries_per_pair"] == 1


def test_core_cities_search_once_per_pair():
    from scraper.config import load_config
    from scraper.pipeline import _queries_for
    from scraper.search import build_queries

    cities, _, cfg = load_config(ROOT / "nonexistent.db")
    core = next(c for c in cities if c.topic_tier == "core")
    full = next(c for c in cities if c.topic_tier != "core")
    assert _queries_for(core, cfg) == 1 and _queries_for(full, cfg) is None
    terms = ["choir", "chorus"]
    assert len(build_queries("Lagos", ["Lagos", "Lagos Nigeria"], terms, 1)) == 1
    assert len(build_queries("Lagos", ["Lagos", "Lagos Nigeria"], terms, None)) == 3


def test_countries_are_largest_first_and_all_searchable():
    cs = countries_mod.load_countries()
    assert len(cs) > 150
    pops = [c.population for c in cs]
    assert pops == sorted(pops, reverse=True)
    assert all(c.dataforseo_location for c in cs)
    # DataForSEO cannot search these; a search from elsewhere buys the wrong SERP.
    assert not {"RU", "IR", "TW", "HK"} & {c.iso2 for c in cs}


def test_every_search_language_has_terms_for_every_core_topic():
    """A locale without terms falls back to English without a word in the log."""
    from scraper.config import yaml_locale

    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
    core = set(settings["pipeline"]["core_topics"])
    topics = yaml.safe_load((ROOT / "config/topics.yaml").read_text(encoding="utf-8"))
    topics = topics["topics"] if isinstance(topics, dict) else topics
    terms = {t["name"]: {yaml_locale(k) for k in t["search_terms"]} for t in topics}
    cities = yaml.safe_load((ROOT / "config/cities.yaml").read_text(encoding="utf-8"))["cities"]
    core_locales = {yaml_locale(c["locale"]) for c in cities if c.get("topic_tier") == "core"}
    missing = {(topic, loc) for topic in core for loc in core_locales
               if loc not in terms[topic]}
    assert not missing, sorted(missing)


def test_every_city_can_be_placed_by_country_or_language():
    from scraper.search import LOCALE_TO_DATAFORSEO_LOCATION

    cities = yaml.safe_load((ROOT / "config/cities.yaml").read_text(encoding="utf-8"))["cities"]
    unplaced = sorted({(c["country"], str(c["locale"])) for c in cities
                       if not countries_mod.dataforseo_location(c["country"])
                       and str(c["locale"]) not in LOCALE_TO_DATAFORSEO_LOCATION
                       and c["locale"] is not False})
    assert not unplaced


def test_norwegian_is_not_the_boolean_false():
    from scraper.config import yaml_locale

    assert yaml.safe_load("locale: no")["locale"] is False
    assert yaml_locale(False) == "no"
    assert yaml_locale("hu") == "hu"


def test_priority_names_the_settings_countries_then_everyone_by_population():
    from scraper.main import _settings_country_priority

    order = _settings_country_priority()
    assert order[:4] == ["Hungary", "Germany", "Indonesia", "Sweden"]
    tail = [c for c in countries_mod.population_order() if c not in order[:4]]
    assert order[4:] == tail
    assert len(order) == len(set(order))


def test_groups_follow_the_order_in_one_pass():
    from types import SimpleNamespace

    from scraper.main import _saver_city_groups

    cities = [SimpleNamespace(name=n, country=c) for n, c in
              [("Lagos", "Nigeria"), ("Pécs", "Hungary"), ("Atlantis", "Nowhere"),
               ("Kano", "Nigeria")]]
    groups = _saver_city_groups(cities, ["Hungary", "Nigeria"])
    assert [[c.name for c in g] for g in groups] == [["Pécs"], ["Lagos", "Kano"], ["Atlantis"]]
