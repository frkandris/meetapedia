"""Public forms store and announce only what the site itself offered.

On 2026-10-03 an SQL-injection scanner walked /subscribe and /varosok/kerelem
(see the 2026-10-sqli-scanner-subscribe post-mortem). Nothing was injected —
every insert is parameterised — but each probe became a row and, where the
form mails the operator, a notification. A form about a community or venue
must name one that exists, take its name and city from the record rather
than the form, carry a real address when it carries one at all, and print
only a page of this site as the link in the operator's mail.
"""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.db import (
    get_community_submissions,
    get_edit_requests,
    get_not_community_reports,
    init_db,
    upsert_venues,
)
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state

HOST = {"host": "kozossegek.com"}
PAGE = "https://kozossegek.com/budapest/sakk-kor"
PROBE = "chess AND 1=CAST('a'||(SELECT 1)::text AS NUMERIC)"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results("Budapest", "chess", [
        CommunityRecord(
            name="Sakk Kör", topic="chess", city="Budapest", locale="hu",
            source_url="https://sakk.test", extracted_at="2026-01-01T00:00:00+00:00",
            description="Heti sakkozás Budapesten.",
        ),
    ], db)
    upsert_venues(db, [{"name": "Kávézó", "city": "Budapest", "venue_id": "v1",
                        "source_urls": ["https://kave.test"]}])
    sent: list[dict] = []
    monkeypatch.setattr(web_app, "_FEEDBACK_EMAIL", "ops@example.com")
    monkeypatch.setattr(web_app, "_RESEND_API_KEY", "test")
    import resend
    monkeypatch.setattr(resend.Emails, "send", lambda payload: sent.append(payload))
    old = app_state.db_path, app_state.cities, app_state.topics
    app_state.db_path = db
    app_state.cities = [
        CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=["Budapest"]),
        CityConfig(name="Szeged", country="Hungary", locale="hu", search_variants=["Szeged"]),
    ]
    app_state.topics = [TopicConfig(name="chess", search_terms={}),
                        TopicConfig(name="running", search_terms={})]
    with sqlite3.connect(db) as conn:
        cid, rkey = conn.execute(
            "SELECT community_id, record_key FROM communities").fetchone()
        vkey = conn.execute("SELECT record_key FROM venues").fetchone()[0]
    try:
        yield {"c": TestClient(web_app.app), "db": db, "sent": sent,
               "cid": cid, "rkey": rkey, "vkey": vkey}
    finally:
        app_state.db_path, app_state.cities, app_state.topics = old


def _post(env, path, **form):
    return env["c"].post(path, data=form, headers=HOST, follow_redirects=False)


# ── /claim-community ──────────────────────────────────────────────────────────

def test_claim_names_come_from_the_record(env):
    r = _post(env, "/claim-community", community_id=env["cid"],
              community_name="<b>Spoofed</b>", city="Sydney",
              page_url=PAGE, claimant_email="leader@example.com")
    assert r.json() == {"ok": True}
    [req] = get_edit_requests(env["db"])
    assert (req["entity_name"], req["entity_city"]) == ("Sakk Kör", "Budapest")
    assert "Spoofed" not in env["sent"][0]["html"]


@pytest.mark.parametrize("form", [
    {"community_id": "000000000000", "community_name": "Sakk Kör",
     "claimant_email": "leader@example.com"},
    {"community_id": "CID", "community_name": "Sakk Kör", "claimant_email": "1"},
])
def test_claim_probe_is_neither_stored_nor_announced(env, form):
    form = {k: (env["cid"] if v == "CID" else v) for k, v in form.items()}
    r = _post(env, "/claim-community", **form)
    assert r.json()["ok"] is False
    assert get_edit_requests(env["db"]) == []
    assert env["sent"] == []


# ── /report-not-community ─────────────────────────────────────────────────────

def test_report_for_unknown_community_is_refused(env):
    r = _post(env, "/report-not-community", community_id="1", community_name=PROBE,
              city="1", topic=PROBE, source_url="1", page_url="1")
    assert r.json()["ok"] is False
    assert get_not_community_reports(env["db"]) == []
    assert env["sent"] == []


def test_report_keeps_only_the_records_own_values(env):
    _post(env, "/report-not-community", community_id=env["cid"], community_name="x",
          city="Sydney", topic=PROBE, source_url="https://evil.test",
          page_url="javascript:alert(1)")
    [rep] = get_not_community_reports(env["db"])
    assert (rep["community_name"], rep["city"], rep["topic"]) == ("Sakk Kör", "Budapest", "chess")
    assert rep["source_url"] == ""
    assert rep["page_url"] == ""
    assert "javascript:" not in env["sent"][0]["html"]


# ── /suggest-edit ─────────────────────────────────────────────────────────────

def _edit(env, **over):
    form = {"entity_type": "community", "entity_id": env["cid"], "entity_name": "Sakk Kör",
            "entity_city": "Budapest", "entity_topic": "chess", "record_key": env["rkey"],
            "change_type": "wrong_city", "new_value": "Szeged", "notes": "", "email": ""}
    form.update(over)
    return _post(env, "/suggest-edit", **form)


def test_valid_edit_request_is_stored(env):
    assert _edit(env, email="reader@example.com").json() == {"ok": True}
    [req] = get_edit_requests(env["db"])
    assert req["new_value"] == "Szeged"


@pytest.mark.parametrize("over, error", [
    ({"record_key": "nope"}, "unknown_entity"),
    ({"entity_type": "venue", "record_key": "nope", "change_type": "closed"}, "unknown_entity"),
    ({"email": "1"}, "invalid_email"),
    ({"new_value": "Sydney' OR 1=1--"}, "invalid_new_value"),
    ({"change_type": "wrong_topic", "new_value": PROBE}, "invalid_new_value"),
])
def test_edit_probe_is_refused(env, over, error):
    assert _edit(env, **over).json() == {"ok": False, "error": error}
    assert get_edit_requests(env["db"]) == []
    assert env["sent"] == []


def test_edit_request_names_come_from_the_record(env):
    _edit(env, entity_name=PROBE, entity_city="1", entity_topic=PROBE)
    [req] = get_edit_requests(env["db"])
    assert (req["entity_name"], req["entity_city"], req["entity_topic"]) == (
        "Sakk Kör", "Budapest", "chess")


def test_venue_edit_request_resolves_the_venue(env):
    r = _edit(env, entity_type="venue", entity_id="x", entity_name="x", entity_city="x",
              entity_topic="", record_key=env["vkey"], change_type="closed", new_value="")
    assert r.json() == {"ok": True}
    [req] = get_edit_requests(env["db"])
    assert (req["entity_id"], req["entity_name"]) == ("v1", "Kávézó")


# ── /feedback ─────────────────────────────────────────────────────────────────

def test_feedback_drops_what_the_site_did_not_offer(env):
    _post(env, "/feedback", message="Hello", user_email="1", city=PROBE, topic=PROBE,
          community_name="", page_url="javascript:alert(1)")
    [mail] = env["sent"]
    assert mail["reply_to"] is None
    assert "CAST" not in mail["html"] and "javascript:" not in mail["html"]


def test_feedback_keeps_a_real_reply_address_and_own_page(env):
    _post(env, "/feedback", message="Hello", user_email="reader@example.com",
          city="Budapest", topic="chess", community_name="Sakk Kör", page_url=PAGE)
    [mail] = env["sent"]
    assert mail["reply_to"] == "reader@example.com"
    assert PAGE in mail["html"]


# ── /varosok/kerelem and /submit-community ────────────────────────────────────

def _city_requests(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT city_name, email FROM city_requests").fetchall()


def test_city_request_needs_a_real_name(env):
    for name in ["1", "1')", "98766", "x" * 500]:
        _post(env, "/varosok/kerelem", city_name=name, email="1")
    assert _city_requests(env["db"]) == []


def test_city_request_drops_a_bad_address_but_keeps_the_town(env):
    r = _post(env, "/varosok/kerelem", city_name="Hódmezővásárhely", email="1")
    assert r.status_code == 303
    assert _city_requests(env["db"]) == [("Hódmezővásárhely", "")]


def test_submission_needs_a_known_city_and_topic(env):
    for city, topic in [("Sydney", "chess"), ("Budapest", PROBE)]:
        r = _post(env, "/submit-community", name="Új Kör", city=city, topic=topic,
                  source_url="https://uj.example.com", submitter_email="")
        assert r.status_code == 400
    assert get_community_submissions(env["db"]) == []
