"""What an answer engine's crawler gets: robots signals, llms.txt, Markdown.

AI crawlers run no JavaScript and read every byte of markup they are handed;
these are the three things that make the site cheap and unambiguous to read.
"""
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from scraper.db import init_db
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state

KOZ = {"host": "kozossegek.com"}
MEET = {"host": "meetapedia.com"}
AGENT = {"accept": "text/markdown, text/html;q=0.5"}
BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "agents.db"
    init_db(db)
    cities = [
        CityConfig(name="Pécs", country="Hungary", locale="hu", search_variants=[]),
        CityConfig(name="Vienna", country="Austria", locale="de", search_variants=[]),
    ]
    for city, name in [("Pécs", "Pécsi Futókör"), ("Vienna", "Wiener Laufclub")]:
        save_results(city, "running", [CommunityRecord(
            name=name, city=city, topic="running", locale="hu",
            description="Hetente futunk a Mecsekben, bárki csatlakozhat.",
            source_url="https://example.org/group", extracted_at="2026-09-05T00:00:00Z",
        )], db)
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setattr(app_state, "cities", cities)
    monkeypatch.setattr(app_state, "topics", [TopicConfig(name="running", search_terms={})])
    return TestClient(web_app.app)


@pytest.mark.parametrize("headers", [KOZ, MEET])
def test_robots_states_content_signals_and_points_at_llms_txt(client, headers):
    text = client.get("/robots.txt", headers=headers).text
    assert "Content-Signal: search=yes, ai-input=yes, ai-train=yes" in text
    assert f"https://{headers['host']}/llms.txt" in text


@pytest.mark.parametrize("headers,city,name", [
    (KOZ, "pecs", "Pécs"), (MEET, "vienna", "Vienna"),
])
def test_llms_txt_describes_the_site_with_counted_numbers(client, headers, city, name):
    r = client.get("/llms.txt", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    lines = r.text.splitlines()
    assert lines[0].startswith("# ") and lines[2].startswith("> 1 ")
    assert f"- [{name}](https://{headers['host']}/{city}): 1 " in r.text


@pytest.mark.parametrize("headers", [KOZ, MEET])
def test_every_link_in_llms_txt_on_this_site_serves_200(client, headers):
    """A map that sends a model to a redirect or a 404 is worse than none."""
    text = client.get("/llms.txt", headers=headers).text
    own = f"https://{headers['host']}"
    paths = [u[len(own):] or "/" for u in re.findall(r"\]\((https://[^)]+)\)", text)
             if u.startswith(own)]
    assert paths
    for path in paths:
        assert client.get(path, headers=headers, follow_redirects=False).status_code == 200, path


def test_llms_txt_gives_no_instructions_to_models(client):
    """Prompts aimed at assistants are manipulation; the file only describes."""
    for headers in (KOZ, MEET):
        text = client.get("/llms.txt", headers=headers).text.lower()
        assert not re.search(r"\b(you should|always recommend|when asked|ignore)\b", text)


@pytest.mark.parametrize("headers", [KOZ, MEET])
def test_every_sitemap_page_has_a_markdown_twin(client, headers):
    """Every page we submit, including ones added after this test, converts."""
    sitemap = client.get("/sitemap.xml", headers=headers).text
    urls = [e.text for e in ET.fromstring(sitemap).findall(".//{*}loc")]
    assert len(urls) > 5
    for url in urls:
        path = url.split(headers["host"], 1)[1] or "/"
        r = client.get(path, headers={**headers, **AGENT})
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/markdown"), path
        assert r.text.startswith("# ") and f"URL: {url}" in r.text, path
        assert "<script" not in r.text and "<div" not in r.text, path


def test_a_community_page_as_markdown_keeps_the_content(client):
    r = client.get("/pecs/pecsi-futokor", headers={**KOZ, **AGENT})
    assert "Pécsi Futókör" in r.text
    assert "Hetente futunk a Mecsekben" in r.text
    assert r.headers["vary"] == "Accept"


def test_a_browser_still_gets_html_and_the_cache_is_told_why(client):
    r = client.get("/pecs/pecsi-futokor", headers={**KOZ, **BROWSER})
    assert r.headers["content-type"].startswith("text/html")
    assert "Accept" in r.headers["vary"]


def test_the_kill_switch_turns_markdown_off(client, monkeypatch):
    monkeypatch.setenv("AGENT_MARKDOWN", "0")
    r = client.get("/pecs/pecsi-futokor", headers={**KOZ, **AGENT})
    assert r.headers["content-type"].startswith("text/html")


def test_the_markdown_twin_is_not_indexed_as_a_duplicate(client):
    r = client.get("/pecs/pecsi-futokor", headers={**KOZ, **AGENT})
    assert r.headers["x-robots-tag"] == "noindex"
