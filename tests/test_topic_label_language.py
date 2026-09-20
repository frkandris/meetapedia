"""Topic labels on a page must be in that page's language.

Reported 2026-09-20 with a screenshot: on kozossegek.com the report form's
"Helyes érdeklődési kör" picker listed "Religion & Faith", "Book Club",
"Photography" — and, mixed in, "Hagyományőrzés", "Baba & Szülő", "Kisállat".

The mix is the tell. The list was built from `app.py:TOPIC_LABELS`, the English
fallback dictionary, in which three Hungarian-only topics were simply never
translated to English. So it was not a partially translated list: it was the
wrong dictionary, showing through wherever it had no English word of its own.

Every other label on the page was correct because `topic_labels` arrives via
`**lang_context(request)` and overrides the explicit kwarg. These two keys —
`all_topic_names` on the community page and `topic_label` on the person page —
are not named `topic_labels`, so nothing overrode them.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.db import init_db, upsert_persons
from scraper.models import CommunityRecord, PersonRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state

KOZ = {"host": "kozossegek.com"}
MEET = {"host": "meetapedia.com"}


@pytest.fixture()
def client(tmp_path: Path):
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results("Budapest", "religion", [
        CommunityRecord(
            name="Ima és Igeliturgia", topic="religion", city="Budapest",
            locale="hu", source_url="https://a.test",
            extracted_at="2026-01-01T00:00:00+00:00",
            description="Heti imaalkalom.",
        ),
    ], db)
    # A non-Hungarian city for the English side: a HU city on meetapedia.com
    # redirects to kozossegek.com (see _hu_redirect), so it cannot answer a
    # question about English labels.
    save_results("Stockholm", "hagyomanyorzes", [
        CommunityRecord(
            name="Folk Circle", topic="hagyomanyorzes", city="Stockholm",
            locale="sv", source_url="https://b.test",
            extracted_at="2026-01-01T00:00:00+00:00",
            description="Weekly folk dance evenings.",
        ),
    ], db)
    upsert_persons(db, [PersonRecord(
        name="Kovács Anna", role="leader", city="Budapest", topic="religion",
        community_name="Ima és Igeliturgia", source_url="https://a.test",
        extracted_at="2026-01-01T00:00:00+00:00",
    ).model_dump()])
    old = (app_state.db_path, app_state.cities, app_state.topics)
    app_state.db_path = db
    app_state.cities = [
        CityConfig(name="Budapest", country="Hungary",
                   locale="hu", search_variants=["Budapest"]),
        CityConfig(name="Stockholm", country="Sweden",
                   locale="sv", search_variants=["Stockholm"]),
    ]
    app_state.topics = [TopicConfig(name="religion", search_terms={}),
                        TopicConfig(name="hagyomanyorzes", search_terms={}),
                        TopicConfig(name="book_club", search_terms={})]
    try:
        yield TestClient(web_app.app)
    finally:
        app_state.db_path, app_state.cities, app_state.topics = old


def test_the_topic_picker_is_hungarian_on_the_hungarian_site(client):
    html = client.get("/budapest/ima-es-igeliturgia", headers=KOZ).text
    assert ">Vallás<" in html
    assert ">Könyvklub<" in html
    assert ">Religion &amp; Faith<" not in html and ">Religion & Faith<" not in html
    assert ">Book Club<" not in html


def test_the_topic_picker_is_english_on_the_english_site(client):
    html = client.get("/stockholm/folk-circle", headers=MEET).text
    assert ">Religion &amp; Faith<" in html or ">Religion & Faith<" in html
    # The three Hungarian-only topics are what exposed the wrong dictionary:
    # app.py's fallback left `hagyomanyorzes` as "Hagyományőrzés", so it showed
    # a Hungarian word on both sites. i18n has a real English label for it.
    assert "Folk Traditions" in html
    assert ">Hagyományőrzés<" not in html


def test_the_person_page_labels_its_communities_in_the_page_language(client):
    html = client.get("/budapest/ember/kovacs-anna", headers=KOZ).text
    assert "Vallás" in html
    assert "Religion &amp; Faith" not in html and "Religion & Faith" not in html
