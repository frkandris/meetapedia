from datetime import datetime, timezone
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from scraper.db import get_daily_counter, get_data_guides, init_db
from scraper.guides import _decode_article, publish_daily_guides
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state


class _Writer:
    last_model = "test-writer"

    def __init__(self):
        self.calls_made = 0

    async def completion(self, messages, **params):
        self.calls_made += 1
        packet = json.loads(messages[-1]["content"].split("\n", 1)[1])
        language = packet["language"]
        filler = ("Ez az útmutató kizárólag a katalógusban rögzített adatokat értelmezi. "
                  if language == "Hungarian" else
                  "This guide interprets only the details recorded in the directory. ")
        body = filler * 9
        article = {
            "introduction": body, "comparison": body,
            "choosing_advice": body, "conclusion": body,
            "used_dimensions": ["location", "fee", "skill_level"],
        }
        return {"choices": [{"message": {"content": json.dumps(article)}}]}


def _seed(db, city: CityConfig, topic: str = "running", count: int = 8) -> None:
    names = ("Aurora", "Borealis", "Canyon", "Delta", "Evergreen", "Falcon",
             "Galaxy", "Harbour", "Indigo", "Juniper")
    records = [CommunityRecord(
        name=f"{city.name} {names[i]}", city=city.name, topic=topic, locale=city.locale,
        description=("A recurring open community with public joining details and "
                     "a sufficiently informative description for prospective members."),
        meeting_schedule=f"Tuesday {i}:00", location=f"Hall {i}", fee="free",
        skill_level="all levels", language=city.locale,
        source_url=f"https://example.org/{city.name}/{i}",
        extracted_at="2026-09-20T00:00:00Z",
    ) for i in range(count)]
    save_results(city.name, topic, records, db)


def test_daily_limit_is_shared_and_country_priority_falls_through(tmp_path):
    db = tmp_path / "guides.db"
    init_db(db)
    cities = [
        CityConfig("Berlin", "de", [], "Germany"),
        CityConfig("Budapest", "hu", [], "Hungary"),
        CityConfig("Stockholm", "sv", [], "Sweden"),
    ]
    for city in cities:
        _seed(db, city)
    now = datetime(2026, 9, 20, 1, tzinfo=timezone.utc)

    created = asyncio.run(publish_daily_guides(
        db, cities, _Writer(), limit=2,
        country_priority=["Hungary", "Germany", "Sweden"], now=now))

    assert [(g["city"], g["site"]) for g in created] == [
        ("Budapest", "kozossegek"), ("Berlin", "meetapedia")]
    assert asyncio.run(publish_daily_guides(db, cities, _Writer(), limit=2, now=now)) == []
    assert len(get_data_guides(db, "kozossegek")) == 1
    assert len(get_data_guides(db, "meetapedia")) == 1
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert get_daily_counter(db, day, "guide_attempts") == 2


def test_failed_guide_call_still_records_provider_attempts(tmp_path):
    class _FailingWriter:
        calls_made = 0

        async def completion(self, messages, **params):
            self.calls_made += 2  # one routed call tried two providers
            raise RuntimeError("fleet unavailable")

    db = tmp_path / "failed-guide.db"
    city = CityConfig("Budapest", "hu", [], "Hungary")
    init_db(db)
    _seed(db, city)
    with pytest.raises(RuntimeError, match="fleet unavailable"):
        asyncio.run(publish_daily_guides(db, [city], _FailingWriter(), limit=1))

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert get_daily_counter(db, day, "guide_attempts") == 2


def test_quality_gate_does_not_force_daily_quota(tmp_path):
    db = tmp_path / "quality.db"
    init_db(db)
    city = CityConfig("Budapest", "hu", [], "Hungary")
    _seed(db, city, count=7)
    assert asyncio.run(publish_daily_guides(db, [city], _Writer(), limit=10)) == []


def test_article_validator_rejects_untraceable_numbers_and_dimensions():
    body = "Grounded editorial sentence without unsupported claims. " * 20
    base = {"introduction": body, "comparison": body, "choosing_advice": body,
            "conclusion": body, "used_dimensions": ["fee"]}
    assert _decode_article(json.dumps(base), {"fee"}, set())
    assert _decode_article(json.dumps({**base, "conclusion": body + " 999 members"}),
                           {"fee"}, {"8"}) is None
    assert _decode_article(json.dumps({**base, "used_dimensions": ["popularity"]}),
                           {"fee"}, set()) is None


def test_guide_routes_and_sitemaps_are_site_scoped(tmp_path, monkeypatch):
    db = tmp_path / "routes.db"
    init_db(db)
    cities = [CityConfig("Budapest", "hu", [], "Hungary"),
              CityConfig("Berlin", "de", [], "Germany")]
    for city in cities:
        _seed(db, city)
    asyncio.run(publish_daily_guides(
        db, cities, _Writer(), limit=10,
        now=datetime(2026, 9, 20, tzinfo=timezone.utc)))
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setattr(app_state, "cities", cities)
    monkeypatch.setattr(app_state, "topics", [TopicConfig("running", {})])
    client = TestClient(web_app.app)

    hu = client.get("/utmutatok/budapest-futas", headers={"host": "kozossegek.com"})
    assert hu.status_code == 200
    assert "Budapest Aurora" in hu.text
    assert '"@type": "Article"' in hu.text
    international = client.get("/guides/berlin-running", headers={"host": "meetapedia.com"})
    assert international.status_code == 200
    assert "What can you compare?" in international.text

    hu_map = client.get("/sitemap.xml", headers={"host": "kozossegek.com"}).text
    en_map = client.get("/sitemap.xml", headers={"host": "meetapedia.com"}).text
    assert "https://kozossegek.com/utmutatok/budapest-futas" in hu_map
    assert "berlin-running" not in hu_map
    assert "https://meetapedia.com/guides/berlin-running" in en_map
    assert "budapest-futas" not in en_map
