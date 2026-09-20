"""The community card must end with a way out to the page that can be joined.

A reader reported on 2026-09-20 that they could not work out how to join a
group. Nothing was missing: the group's own site was in the link list, and the
page we found it on was in small type under the card. Neither read as the next
step. These tests hold the button block that is now that step, and the
one-line summary that enrichment writes and the page never showed.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.db import init_db
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state

KOZ = {"host": "kozossegek.com"}


def _client(tmp_path: Path, record: CommunityRecord):
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results(record.city, record.topic, [record], db)
    app_state.db_path = db
    app_state.cities = [CityConfig(name="Budapest", country="Hungary",
                                   locale="hu", search_variants=["Budapest"])]
    app_state.topics = [TopicConfig(name="music", search_terms={})]
    return TestClient(web_app.app)


@pytest.fixture(autouse=True)
def _restore_state():
    old = (app_state.db_path, app_state.cities, app_state.topics)
    yield
    app_state.db_path, app_state.cities, app_state.topics = old


def _record(**kw) -> CommunityRecord:
    base = dict(
        name="Zenei Kör", topic="music", city="Budapest", locale="hu",
        source_url="https://forras.test/lista",
        extracted_at="2026-01-01T00:00:00+00:00",
    )
    base.update(kw)
    return CommunityRecord(**base)


def test_the_source_page_gets_a_button_even_when_there_is_a_website(tmp_path):
    """The old fallback showed source links only when no website was set.

    Both matter: the group's own page is where joining is arranged, and the
    page we found it on is what a sceptical reader wants to check.
    """
    html = _client(tmp_path, _record(
        website="https://zeneikor.test",
        source_urls=["https://forras.test/lista"],
    )).get("/budapest/zenei-kor", headers=KOZ).text

    assert "Hogyan csatlakozhatsz?" in html
    assert "Tovább a csoport saját oldalára" in html
    assert "Tovább az oldalra, ahol megtaláltuk" in html
    assert 'href="https://zeneikor.test"' in html
    assert 'data-outclick data-community-id=' in html
    assert 'data-link-type="website"' in html
    assert "/out?" not in html


def test_every_source_page_gets_its_own_button(tmp_path):
    html = _client(tmp_path, _record(
        source_urls=["https://forras.test/lista", "https://masik.test/klubok"],
    )).get("/budapest/zenei-kor", headers=KOZ).text

    assert html.count("Tovább az oldalra, ahol megtaláltuk") == 2
    assert "forras.test" in html and "masik.test" in html


def test_a_social_link_is_not_repeated_as_a_source_button(tmp_path):
    """Social links are contact, not a page that explains joining."""
    html = _client(tmp_path, _record(
        source_url="https://facebook.com/zeneikor",
        source_urls=["https://facebook.com/zeneikor"],
        social_links=["https://facebook.com/zeneikor"],
    )).get("/budapest/zenei-kor", headers=KOZ).text

    assert "Tovább az oldalra, ahol megtaláltuk" not in html


def test_the_short_description_reaches_the_page(tmp_path):
    """It was written for listing cards and the <meta> tag only."""
    html = _client(tmp_path, _record(
        short_description="Hetente próbáló vegyeskar Budapesten.",
        long_description="Hosszabb bemutatkozás a kórusról és a próbákról.",
    )).get("/budapest/zenei-kor", headers=KOZ).text

    assert "Hetente próbáló vegyeskar Budapesten." in html
    assert "Hosszabb bemutatkozás a kórusról és a próbákról." in html


def test_the_short_description_is_not_printed_twice(tmp_path):
    """On a record with no enrichment the two can be the same string.

    Counted in the body only: the same sentence is also the <meta> description,
    which is where it was already being used and should stay.
    """
    html = _client(tmp_path, _record(
        description="Ugyanaz a szöveg.",
        short_description="Ugyanaz a szöveg.",
    )).get("/budapest/zenei-kor", headers=KOZ).text
    body = html.split("</head>", 1)[-1]

    assert body.count("Ugyanaz a szöveg.") == 1
