"""The extraction prompt names the topic in words, not by its slug.

Until 2026-10-23 the model read "Extract all nok community groups located in
Pécs" and, from a page listing every NGO in the city, filed all of them as
women's groups — 526 of 1,328 visible `nok` records had nothing female in their
name or tags.
"""
import yaml
from pathlib import Path

from scraper import extract

ROOT = Path(__file__).resolve().parent.parent


def _prompt(topic: str) -> str:
    return extract.get_prompt("extraction_user").format(
        topic=extract.topic_for_prompt(topic), topic_rule=extract.topic_rule(topic),
        city="Pécs", source_url="https://pecs.hu/civil-szervezetek/", page_text="…")


def test_no_hungarian_slug_reaches_the_model():
    for slug in ("nok", "kisallat", "vallalkozas", "hagyomanyorzes", "fogyatekossag"):
        assert f" {slug} " not in _prompt(slug)
    assert "women's community groups" in _prompt("nok")


def test_every_shipped_topic_gets_a_readable_phrase():
    topics = yaml.safe_load((ROOT / "config/topics.yaml").read_text(encoding="utf-8"))
    topics = topics["topics"] if isinstance(topics, dict) else topics
    for t in topics:
        phrase = extract.topic_for_prompt(t["name"])
        assert phrase and "_" not in phrase


def test_a_general_ngo_list_is_not_a_topic_list_except_for_other():
    assert "not a list of women's groups" in _prompt("nok")
    assert "not a list of" not in _prompt("other")


def test_the_user_prompt_is_outside_the_extraction_fingerprint():
    """Changing it must not re-extract the corpus (~74K cached extractions)."""
    before = extract.get_extract_fingerprint()
    old = extract.USER_PROMPT_TEMPLATE
    try:
        extract.USER_PROMPT_TEMPLATE = old + "\nchanged"
        assert extract.get_extract_fingerprint() == before
    finally:
        extract.USER_PROMPT_TEMPLATE = old


def test_the_public_source_page_renders_the_prompt_it_sent(tmp_path):
    """/source/{hash} formatted the template itself and missed {topic_rule}:
    every request answered 500 from 2026-10-23 20:56 until fixed."""
    import hashlib

    from fastapi.testclient import TestClient

    from scraper.cache import CacheManager
    from scraper.db import init_db
    from scraper.web import app as web_app
    from scraper.web.state import app_state

    db = tmp_path / "scraper.db"
    init_db(db)
    url = "https://pecs.hu/civil-szervezetek/"
    cm = CacheManager(db)
    cm.save_scraped(url, "Egyesületek listája Pécsen …", "Pécs", "nok")
    old_db, old_cm = app_state.db_path, app_state.cache_manager
    app_state.db_path, app_state.cache_manager = db, cm
    try:
        h = hashlib.sha256(url.encode()).hexdigest()[:16]
        r = TestClient(web_app.app).get(f"/source/{h}", headers={"host": "kozossegek.com"})
        assert r.status_code == 200
        assert "women&#39;s community groups" in r.text or "women's community groups" in r.text
    finally:
        app_state.db_path, app_state.cache_manager = old_db, old_cm
