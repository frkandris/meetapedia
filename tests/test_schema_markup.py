"""Structured data: which pages carry it, and whether it says enough.

Audited 2026-09-20. Before that audit only two page types emitted JSON-LD —
the community page and explore — and the community object carried eight
properties out of the two dozen the record actually holds. Venue and person
pages, which are the ones whose shape search engines understand best (a named
place in a named town; a named person), carried none at all.

The tag itself now lives in `public_base.html`, so a page type cannot ship
without it by forgetting a `{% block head %}`.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.db import init_db, upsert_persons, upsert_venues
from scraper.models import CommunityRecord, PersonRecord, VenueRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.schema import community_to_schema, site_jsonld
from scraper.web.state import app_state

KOZ = {"host": "kozossegek.com"}


def _blocks(html: str) -> list[dict]:
    raw = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    return [json.loads(b) for b in raw]


def _typed(html: str, type_name: str) -> dict | None:
    for block in _blocks(html):
        for obj in block.get("@graph", [block]):
            if obj.get("@type") == type_name:
                return obj
    return None


@pytest.fixture()
def client(tmp_path: Path):
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results("Budapest", "choir", [CommunityRecord(
        name="Zenei Kör", topic="choir", city="Budapest", locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
        description="Rövid leírás.", long_description="Hosszabb bemutatkozás.",
        website="https://zeneikor.test", email="info@zeneikor.test",
        phone="+36 1 234 5678", founding_year=1987, language="magyar",
        tags=["vegyeskar", "klasszikus"], location="Fő tér 1.",
        meeting_schedule="Szerdánként 18:00", leader="Kovács Anna, karnagy",
        social_links=["https://facebook.com/zeneikor"],
    )], db)
    upsert_venues(db, [VenueRecord(
        name="Kultúrház", city="Budapest", locale="hu",
        address="Fő tér 2.", venue_type="cultural_center",
        description="Régi kultúrház a főtéren.", website="https://kult.test",
        email="info@kult.test", phone="+36 1 111 2222",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
    ).model_dump()])
    upsert_persons(db, [PersonRecord(
        name="Kovács Anna", role="leader", city="Budapest", topic="choir",
        community_name="Zenei Kör", bio="Karnagy és tanár.",
        website="https://kovacsanna.test", source_url="https://a.test",
        extracted_at="2026-01-01T00:00:00+00:00",
    ).model_dump()])
    old = (app_state.db_path, app_state.cities, app_state.topics)
    app_state.db_path = db
    app_state.cities = [CityConfig(name="Budapest", country="Hungary",
                                   locale="hu", search_variants=["Budapest"])]
    app_state.topics = [TopicConfig(name="choir", search_terms={})]
    try:
        yield TestClient(web_app.app)
    finally:
        app_state.db_path, app_state.cities, app_state.topics = old


def test_the_venue_page_describes_a_place(client):
    html = client.get("/budapest/helyszin/kulturhaz", headers=KOZ).text
    obj = _typed(html, "CivicStructure")
    assert obj, "venue pages carried no structured data at all before 2026-09-20"
    assert obj["name"] == "Kultúrház"
    assert obj["address"]["streetAddress"] == "Fő tér 2."
    assert obj["address"]["addressLocality"] == "Budapest"
    assert obj["telephone"] == "+36 1 111 2222"
    assert obj["@id"].startswith("https://kozossegek.com/budapest/helyszin/")


def test_the_person_page_describes_a_person_and_their_groups(client):
    html = client.get("/budapest/ember/kovacs-anna", headers=KOZ).text
    obj = _typed(html, "Person")
    assert obj, "person pages carried no structured data at all before 2026-09-20"
    assert obj["name"] == "Kovács Anna"
    assert obj["description"] == "Karnagy és tanár."
    assert obj["memberOf"][0]["name"] == "Zenei Kör"


def test_the_home_page_declares_the_site_and_its_search(client):
    html = client.get("/", headers=KOZ).text
    site = _typed(html, "WebSite")
    assert site, "the home page had no WebSite markup before 2026-09-20"
    assert site["potentialAction"]["@type"] == "SearchAction"
    assert "/kereses?q=" in site["potentialAction"]["target"]["urlTemplate"]
    assert _typed(html, "Organization")


def test_the_community_object_carries_what_the_record_knows(client):
    html = client.get("/budapest/zenei-kor", headers=KOZ).text
    obj = _typed(html, "MusicGroup")
    assert obj
    # Eight of these are new as of the 2026-09-20 audit.
    assert obj["description"] == "Hosszabb bemutatkozás."
    assert obj["url"] == "https://zeneikor.test"
    assert obj["email"] == "info@zeneikor.test"
    assert obj["telephone"] == "+36 1 234 5678"
    assert obj["foundingDate"] == "1987"
    assert obj["knowsLanguage"] == "magyar"
    assert "vegyeskar" in obj["keywords"]
    assert obj["member"]["name"] == "Kovács Anna, karnagy"
    assert obj["location"]["address"]["addressLocality"] == "Budapest"
    assert obj["mainEntityOfPage"].endswith("/budapest/zenei-kor")


def test_nothing_is_invented_for_a_bare_record():
    """A record with nothing but its identity must produce no claims."""
    obj = community_to_schema(CommunityRecord(
        name="Névtelen Kör", topic="other", city="Szeged", locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
    ))
    for absent in ("foundingDate", "email", "telephone", "keywords",
                   "knowsLanguage", "member", "url", "description"):
        assert absent not in obj, f"{absent} was invented"
    assert obj["@type"] == "Organization"
    assert obj["location"]["addressLocality"] == "Szeged"


def test_site_markup_needs_a_name_and_a_url():
    assert site_jsonld("", "https://x.test", "/kereses") == ""
    assert site_jsonld("x", "", "/kereses") == ""


def test_a_listing_page_does_not_claim_every_record_is_its_main_entity(client):
    """`mainEntityOfPage` on a list would say all of them are the page."""
    html = client.get("/budapest", headers=KOZ).text
    for block in _blocks(html):
        for obj in block.get("@graph", [block]):
            if obj.get("@type") in {"BreadcrumbList", "WebSite", "Organization"}:
                continue
            assert "mainEntityOfPage" not in obj
