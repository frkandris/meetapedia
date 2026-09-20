"""GSC found sitemap URLs that redirect, while their detail records still exist."""
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from scraper.db import init_db, upsert_persons, upsert_venues
from scraper.models import CommunityRecord, PersonRecord, VenueRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state


@pytest.fixture
def route_client(tmp_path, monkeypatch):
    db = tmp_path / "routes.db"
    init_db(db)
    cities = [
        CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[]),
        CityConfig(name="Vienna", country="Austria", locale="de", search_variants=[]),
    ]
    for city in cities:
        for topic, name in [("running", "Running Club"), ("community_general", "Legacy Club")]:
            save_results(city.name, topic, [CommunityRecord(
                name=name, city=city.name, topic=topic, locale=city.locale,
                description="Weekly group with open membership.",
                source_url="https://example.org/group", extracted_at="2026-09-05T00:00:00Z",
            )], db)
    # Venue and person pages are in the sitemap on both editions since
    # 2026-09-20. They are listed here so the URL contract below — every
    # submitted URL serves 200 and self-canonicalizes — covers them too.
    for city in cities:
        upsert_venues(db, [VenueRecord(
            name=f"{city.name} Hall", city=city.name, locale=city.locale,
            address="Main square 1", venue_type="cultural_center",
            source_url="https://example.org/venue",
            extracted_at="2026-09-05T00:00:00Z",
        ).model_dump()])
        upsert_persons(db, [PersonRecord(
            name=f"Anna {city.name}", role="leader", city=city.name,
            topic="running", community_name="Running Club",
            source_url="https://example.org/person",
            extracted_at="2026-09-05T00:00:00Z",
        ).model_dump()])
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setattr(app_state, "cities", cities)
    monkeypatch.setattr(app_state, "topics", [TopicConfig(name="running", search_terms={})])
    return TestClient(web_app.app)


@pytest.mark.parametrize("domain,city", [
    ("kozossegek.com", "budapest"), ("meetapedia.com", "vienna"),
])
def test_sitemap_urls_serve_directly_and_keep_legacy_details(route_client, domain, city):
    headers = {"host": domain}
    response = route_client.get("/sitemap.xml", headers=headers)
    urls = [e.text for e in ET.fromstring(response.text).findall(".//{*}loc")]
    assert f"https://{domain}/{city}/legacy-club" in urls
    assert f"https://{domain}/{city}/community-general" not in urls
    topic_slug = web_app._topic_url_slug("running", "hu" if city == "budapest" else "de")
    assert f"https://{domain}/{city}/{topic_slug}" in urls
    # Test the actual HTTP contract, including static aliases: no hidden 302
    # followed by a 200, and no canonical pointing to a different page.
    for url in urls:
        page = route_client.get(url, headers=headers, follow_redirects=False)
        assert page.status_code == 200, (url, page.status_code, page.headers.get("location"))
        assert f'<link rel="canonical" href="{url}">' in page.text, url


@pytest.mark.parametrize("domain,city,label", [
    ("kozossegek.com", "budapest", "Egyéb"), ("meetapedia.com", "vienna", "Other"),
])
def test_legacy_detail_has_no_broken_topic_links_and_keeps_report_identity(
    route_client, domain, city, label,
):
    page = route_client.get(f"/{city}/legacy-club", headers={"host": domain})
    assert page.status_code == 200
    title = re.search(r"<title>(.*?)</title>", page.text, re.S).group(1)
    assert label in title
    assert "community_general" not in title
    assert f'/{city}/community-general' not in page.text
    assert 'id="rf-topic"           value="community_general"' in page.text
    assert 'content="noindex"' not in page.text
    assert f'href="/{city}/running-club"' in page.text


@pytest.mark.parametrize("domain,city", [
    ("kozossegek.com", "budapest"), ("meetapedia.com", "vienna"),
])
def test_venue_and_person_pages_are_submitted_on_both_editions(route_client, domain, city):
    """meetapedia.com submitted 38,108 URLs and none of its venues or people.

    The cause was a pair of prefixes written for an English URL scheme that
    was never built: /vienna/venue/x is a 404, /vienna/helyszin/x is the page.
    So the venue and person branch was skipped entirely on that edition rather
    than producing broken URLs — which is the better of the two bugs, and
    still meant the international corpus was invisible to a crawler that reads
    sitemaps.
    """
    urls = [e.text for e in ET.fromstring(
        route_client.get("/sitemap.xml", headers={"host": domain}).text
    ).findall(".//{*}loc")]

    assert f"https://{domain}/{city}/helyszin/{city}-hall" in urls
    assert f"https://{domain}/{city}/ember/anna-{city}" in urls
    assert not [u for u in urls if "/venue/" in u or "/person/" in u]
