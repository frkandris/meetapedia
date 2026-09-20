"""The acquisition funnel: that it is recorded, and that it is readable.

Two things were true before these tests. A claim — the strongest signal the
public site produces — was emailed and never stored, so with no mail key set it
vanished while the visitor was told "ok". And every other stage of the funnel
was in the database but behind the admin password, so nobody driving the
project from a terminal could see whether anything converted at all.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.db import (get_communities, get_funnel_counts, init_db, record_pageview,
                        save_subscription, upsert_persons)
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state

KOZ = {"host": "kozossegek.com"}
# Base64 of "admin:testpass"
_ADMIN_HEADERS = {
    "Authorization": "Basic YWRtaW46dGVzdHBhc3M=",
    "Host": "testserver",
    "Origin": "http://testserver",
}


@pytest.fixture()
def funnel_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results("Budapest", "music", [
        CommunityRecord(
            name="Zenei Kör", topic="music", city="Budapest", locale="hu",
            source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
            description="Aktív zenei közösség.", email="kor@example.test",
            website="https://kor.example.test",
        ),
        CommunityRecord(
            name="Néma Klub", topic="music", city="Budapest", locale="hu",
            source_url="https://b.test", extracted_at="2026-01-01T00:00:00+00:00",
        ),
    ], db)
    # Three people, two addresses, one of them the club's own — the case the row
    # counts cannot distinguish from three contacts.
    upsert_persons(db, [
        {"name": "Kovács Anna", "city": "Budapest", "topic": "music",
         "role": "leader", "person_id": "p1", "community_name": "Zenei Kör",
         "email": "anna@example.test"},
        {"name": "Nagy Béla", "city": "Budapest", "topic": "music",
         "role": "leader", "person_id": "p2", "community_name": "Néma Klub",
         "email": "KOR@example.test "},
        {"name": "Tóth Csaba", "city": "Budapest", "topic": "music",
         "role": "contact", "person_id": "p3", "community_name": "Néma Klub"},
        # Anna's address again, differently typed. Without folding this is a
        # fourth contact; with it, it is the same person reached twice.
        {"name": "Szabó Dóra", "city": "Budapest", "topic": "music",
         "role": "contact", "person_id": "p4", "community_name": "Zenei Kör",
         "email": " ANNA@Example.test"},
    ])
    old_db, old_cities = app_state.db_path, app_state.cities
    app_state.db_path = db
    app_state.cities = [
        CityConfig(name="Budapest", country="Hungary", locale="hu",
                   search_variants=["Budapest"]),
    ]
    monkeypatch.setenv("ROUTER_API_KEY", "funnel-key")
    monkeypatch.setattr(web_app, "_RESEND_API_KEY", "", raising=False)
    try:
        yield db
    finally:
        app_state.db_path, app_state.cities = old_db, old_cities


def test_an_empty_database_reports_zeroes_not_an_error(tmp_path: Path):
    """A funnel that raises on a fresh install is a funnel nobody wires up."""
    counts = get_funnel_counts(tmp_path / "missing.db")
    assert counts["subscriptions_total"] == 0
    assert counts["records"] == 0


def test_the_funnel_counts_each_stage(funnel_db):
    record_pageview(funnel_db, "2026-08-21", "kozossegek", "visitor-a")
    record_pageview(funnel_db, "2026-08-21", "kozossegek", "visitor-a")
    record_pageview(funnel_db, "2026-08-21", "kozossegek", "visitor-b")
    save_subscription(funnel_db, "reader@example.test", "Budapest", "music")
    save_subscription(funnel_db, "reader@example.test", "Budapest", "sport")
    save_subscription(funnel_db, "other@example.test", "Budapest", "music")

    counts = get_funnel_counts(funnel_db, days=365)
    assert counts["pageviews"] == 3
    assert counts["visitors"] == 2
    # Three rows, two people. A mail goes to a person, so both are reported.
    assert counts["subscriptions_total"] == 3
    assert counts["subscribers_total"] == 2
    assert counts["records"] == 2
    assert counts["records_with_email"] == 1
    assert counts["records_with_website"] == 1
    assert counts["persons"] == 4
    assert counts["persons_with_email"] == 3


def test_js_outclick_endpoint_records_only_a_real_community_link(funnel_db):
    client = TestClient(web_app.app)
    community_id = next(
        r["community_id"] for r in get_communities(funnel_db, "Budapest", "music")
        if r.get("website") == "https://kor.example.test"
    )
    payload = {"community_id": community_id, "url": "https://kor.example.test",
               "link_type": "website"}

    assert client.post("/api/outclick", json=payload, headers=KOZ).status_code == 202
    assert get_funnel_counts(funnel_db, days=365)["outclicks_total"] == 1

    # Public analytics must not be an arbitrary database-growth endpoint.
    payload["url"] = "https://spam.example.test"
    assert client.post("/api/outclick", json=payload, headers=KOZ).status_code == 202
    assert get_funnel_counts(funnel_db, days=365)["outclicks_total"] == 1


def test_a_schemeless_website_still_matches_the_url_the_browser_followed(funnel_db):
    """The defect review caught: analytics lost, silently.

    A record may store `website` without a scheme. `public_community.html`
    renders those as `https://…`, so the browser reports the URL it actually
    followed — and comparing it against the bare stored form rejected the
    event. Losing the number is worse than never collecting it, because what
    remains still looks like a real measurement.
    """
    from scraper.db import is_known_community_url
    from scraper.models import CommunityRecord
    from scraper.store import save_results

    save_results("Budapest", "dance", [CommunityRecord(
        name="Séma Nélküli Kör", topic="dance", city="Budapest", locale="hu",
        website="tancklub.example.test",          # no scheme, as stored
        source_url="https://forras.example.test",
        extracted_at="2026-01-01T00:00:00+00:00",
    )], funnel_db)
    community_id = next(
        r["community_id"] for r in get_communities(funnel_db, "Budapest", "dance")
        if r["name"] == "Séma Nélküli Kör")

    assert is_known_community_url(
        funnel_db, community_id, "https://tancklub.example.test")
    assert is_known_community_url(
        funnel_db, community_id, "https://tancklub.example.test/")
    # Still not an open endpoint.
    assert not is_known_community_url(
        funnel_db, community_id, "https://spam.example.test")


def test_the_click_tracker_ignores_the_context_menu():
    """Right-click opens a menu; it does not follow the link.

    Counting it inflates the conversion number this script exists to measure —
    every reader who inspects or copies a link would read as an outbound
    click. Middle-click opens the page in a new tab, so that one counts.
    """
    source = Path("scraper/web/static/js/outclick.js").read_text(encoding="utf-8")
    assert 'event.type === "auxclick" && event.button !== 1' in source
    assert 'event.type === "click" && event.button !== 0' in source


def test_a_claim_survives_without_a_mail_provider(funnel_db):
    """The failure this test exists for: no RESEND_API_KEY, claim silently lost."""
    client = TestClient(web_app.app)
    r = client.post("/claim-community", data={
        "community_id": "abc123",
        "community_name": "Zenei Kör",
        "city": "Budapest",
        "page_url": "https://kozossegek.com/budapest/zenei-kor",
        "claimant_email": "leader@example.test",
    }, headers=KOZ)
    assert r.json()["ok"] is True

    counts = get_funnel_counts(funnel_db, days=365)
    assert counts["claims_total"] == 1
    # A claim is not a correction; counting them together hides both.
    assert counts["edit_requests_total"] == 0


def test_a_claim_without_an_email_is_rejected_and_not_stored(funnel_db):
    client = TestClient(web_app.app)
    r = client.post("/claim-community", data={
        "community_name": "Zenei Kör", "claimant_email": "",
    }, headers=KOZ)
    assert r.json()["ok"] is False
    assert get_funnel_counts(funnel_db, days=365)["claims_total"] == 0


def test_the_funnel_endpoint_needs_a_key(funnel_db):
    client = TestClient(web_app.app)
    assert client.get("/v1/funnel").status_code == 401
    r = client.get("/v1/funnel", headers={"Authorization": "Bearer funnel-key"})
    assert r.status_code == 200
    assert r.json()["object"] == "funnel"


def test_the_window_is_bounded(funnel_db):
    """`days` reaches a date() call; an unbounded one is worth refusing early."""
    client = TestClient(web_app.app)
    r = client.get("/v1/funnel?days=99999", headers={"Authorization": "Bearer funnel-key"})
    assert r.status_code == 200
    assert r.json()["days"] == 365


def test_the_report_carries_the_funnel(funnel_db):
    """The report is the only thing read every day; a metric outside it is unread."""
    from scraper.report import build_report_html

    counts = get_funnel_counts(funnel_db, days=30)
    counts["claims_total"] = counts["claims"] = 7
    summary = {
        "hu": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                              "new_venues", "new_persons", "pages_scraped",
                              "pages_extracted", "searches")},
        "intl": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                                "new_venues", "new_persons", "pages_scraped",
                                "pages_extracted", "searches")},
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
        "runs": [], "providers": [],
    }
    _, html = build_report_html("2026-08-21", summary, {}, None, counts)
    assert "Vevőszerzés" in html
    assert "Közösség igénylés" in html
    assert ">7<" in html


def test_the_report_survives_a_missing_funnel(funnel_db):
    from scraper.report import build_report_html

    summary = {
        "hu": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                              "new_venues", "new_persons", "pages_scraped",
                              "pages_extracted", "searches")},
        "intl": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                                "new_venues", "new_persons", "pages_scraped",
                                "pages_extracted", "searches")},
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
        "runs": [], "providers": [],
    }
    _, html = build_report_html("2026-08-21", summary, {}, None, None)
    assert "Vevőszerzés" not in html
    assert "Változások" in html


def test_the_window_does_not_leak_the_cutoff_day(funnel_db):
    """Two timestamp formats share this schema and text-compare wrongly.

    Python writes `2026-07-24T00:00:01+00:00`, SQLite's datetime('now') writes
    `2026-07-24 15:34:25`, and "T" sorts above " " — so a row from fifteen
    hours outside the window compared as inside it.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=40))
    # Same calendar day as the cutoff, but hours before it: outside the window.
    edge = (datetime.now(timezone.utc) - timedelta(days=30)).replace(
        hour=0, minute=0, second=1)
    with sqlite3.connect(funnel_db) as c:
        for stamp in (old.isoformat(), edge.isoformat()):
            c.execute(
                "INSERT INTO subscriptions(email, city, topic, token, created_at)"
                " VALUES(?,?,?,?,?)",
                (f"{stamp}@example.test", "Budapest", "music", stamp, stamp))
        c.commit()

    counts = get_funnel_counts(funnel_db, days=7)
    assert counts["subscriptions"] == 0
    assert counts["subscriptions_total"] == 2


def test_a_claim_can_be_approved(funnel_db):
    """A claim asks for no field change, so the generic apply path refuses it.

    Approve therefore errored and Reject was the only way to clear the
    highest-intent row on the page.
    """
    from scraper.db import get_edit_requests

    client = TestClient(web_app.app)
    client.post("/claim-community", data={
        "community_id": "abc123", "community_name": "Zenei Kör",
        "city": "Budapest", "page_url": "https://kozossegek.com/budapest/zenei-kor",
        "claimant_email": "leader@example.test",
    }, headers=KOZ)
    pending = get_edit_requests(funnel_db, status="pending")
    assert len(pending) == 1

    from unittest.mock import patch
    with patch("scraper.web.app._ADMIN_PASSWORD", "testpass"):
        r = client.post(f"/admin/edit-requests/{pending[0]['id']}/approve",
                        headers=_ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert get_edit_requests(funnel_db, status="pending") == []
    # Approving a claim must not silently mutate the community it names.
    assert get_edit_requests(funnel_db, status="approved")[0]["change_type"] == "claim"


def test_contactability_counts_addresses_as_well_as_rows(funnel_db):
    """Rows and addresses answer different questions.

    "How many communities have an email" and "how many people could an opt-in
    channel reach" are not the same number: one address can sit on a club's page
    and on its leader's, and a community centre's office address serves every
    group in the building. The row counts size the corpus; the distinct counts
    size the audience. Both are reported because reading one as the other
    overstates the reach.
    """
    counts = get_funnel_counts(funnel_db, days=365)

    # Three rows carry an address and two addresses exist: ` ANNA@Example.test`
    # is Anna's `anna@example.test` in different case with a leading space, so
    # the folding is what the third row turns into rather than a fourth contact.
    # `KOR@example.test ` is the club's own address on its leader's row — the
    # same duplication across tables, which is why the two counts are reported
    # separately rather than summed.
    assert counts["persons_with_email"] == 3
    assert counts["persons_email_distinct"] == 2
    assert counts["records_with_email"] == 1
    assert counts["records_email_distinct"] == 1


def test_persons_counts_survive_a_database_without_the_table(tmp_path: Path):
    """Old production databases predate `persons`; a missing table reports zero
    rather than failing the whole funnel, like every other count here."""
    db = tmp_path / "bare.db"
    init_db(db)
    with __import__("sqlite3").connect(db) as conn:
        conn.execute("DROP TABLE persons")

    counts = get_funnel_counts(db)

    assert counts["persons"] == 0
    assert counts["persons_with_email"] == 0
    assert counts["records"] == 0
