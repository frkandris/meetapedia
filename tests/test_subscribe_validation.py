"""/subscribe stores and announces only a real address, city and topic.

On 2026-10-03 an SQL-injection scanner posted nine forms in twenty seconds —
email "1", city "Sydney", the topic field carrying quotes and MySQL/PostgreSQL
payloads. The insert is parameterised, so nothing ran, but every probe became
a `subscriptions` row and a notification e-mail. A value the site never
offered is not a subscription.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.db import get_subscriptions, init_db
from scraper.pipeline import CityConfig, TopicConfig
from scraper.web import app as web_app
from scraper.web.state import app_state


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    db = tmp_path / "scraper.db"
    init_db(db)
    sent: list[dict] = []
    monkeypatch.setattr(web_app, "_FEEDBACK_EMAIL", "ops@example.com")
    monkeypatch.setattr(web_app, "_RESEND_API_KEY", "test")
    import resend
    monkeypatch.setattr(resend.Emails, "send", lambda payload: sent.append(payload))
    old = app_state.db_path, app_state.cities, app_state.topics
    app_state.db_path = db
    app_state.cities = [
        CityConfig(name="Sydney", country="Australia", locale="en", search_variants=["Sydney"]),
    ]
    app_state.topics = [
        TopicConfig(name="chess", search_terms={}),
        TopicConfig(name="running", search_terms={}),
    ]
    try:
        yield TestClient(web_app.app), db, sent
    finally:
        app_state.db_path, app_state.cities, app_state.topics = old


def _post(c, **form):
    return c.post("/subscribe", data=form, headers={"host": "meetapedia.com"},
                  follow_redirects=False)


def test_valid_subscription_is_saved_and_announced(client):
    c, db, sent = client
    r = _post(c, email="reader@example.com", city="Sydney", topics=["chess"])
    assert r.status_code == 302
    assert "subscribed=1" in r.headers["location"]
    assert [s["topic"] for s in get_subscriptions(db)] == ["chess"]
    assert len(sent) == 1


@pytest.mark.parametrize("form", [
    {"email": "1", "city": "Sydney", "topics": ["chess"]},
    {"email": "reader@example.com", "city": "Sydney",
     "topics": ["chess AND 1=CAST('a'||(SELECT 1)::text AS NUMERIC)"]},
    {"email": "reader@example.com", "city": "Sydney", "topics": ["chess'"]},
    {"email": "reader@example.com", "city": "Sydney' OR 1=1--", "topics": ["chess"]},
])
def test_probe_is_neither_stored_nor_announced(client, form):
    c, db, sent = client
    r = _post(c, **form)
    assert r.status_code == 302
    assert "subscribed=1" not in r.headers["location"]
    assert get_subscriptions(db) == []
    assert sent == []


def test_unknown_topics_are_dropped_known_ones_kept(client):
    c, db, sent = client
    _post(c, email="reader@example.com", city="Sydney", topics=["chess", "chess)", "running"])
    assert sorted(s["topic"] for s in get_subscriptions(db)) == ["chess", "running"]
    assert "chess)" not in sent[0]["html"]


def test_probe_is_not_reflected_into_the_redirect(client):
    c, _, _ = client
    r = _post(c, email="reader@example.com", city="Sydney' OR 1=1--", topics=["chess'"])
    assert "OR" not in r.headers["location"]
    assert "'" not in r.headers["location"]
