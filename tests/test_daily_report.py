"""Daily report email: traffic counter, summary scopes, HTML builder, middleware."""
from pathlib import Path

from scraper.db import (
    create_data_guide,
    finish_run,
    get_daily_summary,
    get_data_guides_for_day,
    get_traffic_for_day,
    init_db,
    record_pageview,
    start_run,
)
from scraper.models import CommunityRecord
from scraper.report import build_report_html
from scraper.store import save_results


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def test_pageview_counter_and_uniques(tmp_path):
    db = _db(tmp_path)
    record_pageview(db, "2026-07-09", "kozossegek", "visitor-a")
    record_pageview(db, "2026-07-09", "kozossegek", "visitor-a")  # same visitor
    record_pageview(db, "2026-07-09", "kozossegek", "visitor-b")
    record_pageview(db, "2026-07-09", "meetapedia", "visitor-a")
    t = get_traffic_for_day(db, "2026-07-09")
    assert t["kozossegek"] == {"pageviews": 3, "visitors": 2}
    assert t["meetapedia"] == {"pageviews": 1, "visitors": 1}
    assert get_traffic_for_day(db, "2026-07-08") == {}


def test_daily_summary_scopes(tmp_path):
    db = _db(tmp_path)
    save_results("Budapest", "running", [CommunityRecord(
        name="Futó Kör", topic="running", city="Budapest", locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00")], db)
    save_results("Stockholm", "running", [CommunityRecord(
        name="Sthlm Runners", topic="running", city="Stockholm", locale="sv",
        source_url="https://b.test", extracted_at="2026-01-01T00:00:00+00:00")], db)

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    s = get_daily_summary(db, start, end, hu_cities={"Budapest"})
    assert s["hu"]["new_communities"] == 1
    assert s["intl"]["new_communities"] == 1
    assert s["totals"]["hu"] == 1 and s["totals"]["intl"] == 1
    # current stock (not just the diff)
    assert s["stock"]["hu"]["communities"] == 1
    assert s["stock"]["intl"]["communities"] == 1
    assert s["stock"]["hu"]["venues"] == 0 and s["stock"]["hu"]["pages_cached"] == 0


def test_report_html_contains_sections_and_numbers():
    summary = {
        "hu": {"new_communities": 5, "changed_communities": 2, "change_rows": 7,
               "new_venues": 1, "new_persons": 0, "pages_scraped": 40,
               "pages_extracted": 30, "searches": 12},
        "intl": {"new_communities": 3, "changed_communities": 0, "change_rows": 0,
                 "new_venues": 0, "new_persons": 2, "pages_scraped": 10,
                 "pages_extracted": 8, "searches": 4},
        "runs": [{"id": 1, "mode": "search_only", "started_at": "2026-07-09T01:00:00",
                  "finished_at": "2026-07-09T16:20:00", "success": True,
                  "pairs": 100, "records": 0, "search_failed": 2, "extract_failed": 0,
                  "error": "DeepSeek failed <hard>"}],
        "totals": {"hu": 11000, "intl": 2000,
                   "covered_pairs_hu": 12000, "covered_pairs_intl": 900},
    }
    traffic = {"kozossegek": {"pageviews": 120, "visitors": 45},
               "meetapedia": {"pageviews": 30, "visitors": 12}}
    subject, html = build_report_html("2026-07-09", summary, traffic)
    assert "8 új közösség" in subject and "57 látogató" in subject
    for frag in ("Napi összefoglaló — 2026-07-09", "kozossegek.com", "meetapedia.com",
                 "Új közösség", ">5<", ">3<", ">8<", "search_only", "hibák: 2 keresés",
                 "futási hiba: DeepSeek failed &lt;hard&gt;", "11000", "13000"):
        assert frag in html, f"hiányzik: {frag}"


def test_report_lists_daily_guides_and_writer_model(tmp_path):
    db = _db(tmp_path)
    create_data_guide(db, {
        "slug": "budapest-futas", "site": "kozossegek", "city": "Budapest",
        "topic": "running", "locale": "hu", "title": "Futóközösségek Budapesten",
        "summary": "Összefoglaló", "published_at": "2026-09-20T01:00:00+00:00",
        "updated_at": "2026-09-20T01:00:00+00:00",
        "data": {"writer_model": "test-model"},
    })
    guides = get_data_guides_for_day(db, "2026-09-20")
    zero = dict(new_communities=0, changed_communities=0, change_rows=0,
                new_venues=0, new_persons=0, pages_scraped=0,
                pages_extracted=0, searches=0)
    summary = {"hu": zero, "intl": dict(zero), "runs": [],
               "totals": {"hu": 0, "intl": 0,
                          "covered_pairs_hu": 0, "covered_pairs_intl": 0}}

    _, report = build_report_html("2026-09-20", summary, {}, guides=guides)

    assert "Új adatútmutatók" in report
    assert "<b>1</b> készült (1 magyar, 0 nemzetközi)" in report
    assert "https://kozossegek.com/utmutatok/budapest-futas" in report
    assert "test-model" in report


def test_report_html_shows_original_search_error():
    """Provider death must be diagnosable from the email alone (2026-07-22)."""
    empty = {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                            "new_venues", "new_persons", "pages_scraped",
                            "pages_extracted", "searches")}
    summary = {
        "hu": dict(empty), "intl": dict(empty),
        "runs": [{"id": 1, "mode": "search_only", "started_at": "2026-07-22T01:00:00",
                  "finished_at": "2026-07-22T01:05:00", "success": False,
                  "pairs": 4, "records": 0, "search_failed": 4, "extract_failed": 0,
                  "search_error": "DataForSEO: insufficient credits (40201)",
                  "error": ""}],
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
    }
    _, html = build_report_html("2026-07-22", summary, {})
    assert "ok: DataForSEO: insufficient credits (40201)" in html


def test_report_html_three_run_states():
    """2026-07-30: one transient timeout out of 1414 pairs rendered ❌, the same
    mark a dead provider gets. A finished-but-retrying run is ⚠️ now."""
    empty = {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                            "new_venues", "new_persons", "pages_scraped",
                            "pages_extracted", "searches")}

    def _summary(run):
        return {"hu": dict(empty), "intl": dict(empty), "runs": [run],
                "totals": {"hu": 0, "intl": 0,
                           "covered_pairs_hu": 0, "covered_pairs_intl": 0}}

    base = {"id": 1, "mode": "search_only", "started_at": "2026-07-30T01:00:00",
            "finished_at": "2026-07-30T16:00:00", "pairs": 1414, "records": 0,
            "search_failed": 0, "extract_failed": 0, "search_error": "", "error": ""}

    _, clean = build_report_html("2026-07-30", _summary(
        {**base, "success": True, "outcome": "ok"}), {})
    assert "✅" in clean and "⚠️" not in clean

    _, warned = build_report_html("2026-07-30", _summary(
        {**base, "success": True, "outcome": "warning", "search_failed": 1,
         "search_error": "DataForSEO standard task timed out"}), {})
    assert "⚠️" in warned and "❌" not in warned
    assert "a következő futás újrapróbálja" in warned

    _, dead = build_report_html("2026-07-30", _summary(
        {**base, "success": False, "outcome": "aborted", "search_failed": 1,
         "search_error": "search providers unavailable for this run"}), {})
    assert "❌" in dead


def test_report_html_falls_back_when_outcome_missing():
    """Rows written before the outcome column still render (legacy boolean)."""
    empty = {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                            "new_venues", "new_persons", "pages_scraped",
                            "pages_extracted", "searches")}
    summary = {
        "hu": dict(empty), "intl": dict(empty),
        "runs": [{"id": 1, "mode": "full", "started_at": "2026-07-01T01:00:00",
                  "finished_at": "2026-07-01T02:00:00", "success": False,
                  "pairs": 3, "records": 0, "search_failed": 0, "extract_failed": 0,
                  "search_error": "", "error": ""}],
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
    }
    _, html = build_report_html("2026-07-01", summary, {})
    assert "❌" in html


def test_daily_summary_extracts_search_error_from_pair_logs(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    db = _db(tmp_path)
    started = datetime.now(timezone.utc)
    run_id = start_run(db, started, "search_only")
    logs = [{"city": "Stockholm", "topic": "running", "search_failed": True,
             "search_error": "DataForSEO: HTTP 500", "extract_failed": 0,
             "records_extracted": 0}]
    finish_run(db, run_id, started + timedelta(seconds=1), False, json.dumps(logs))
    summary = get_daily_summary(
        db,
        (started - timedelta(seconds=1)).isoformat(),
        (started + timedelta(seconds=2)).isoformat(),
        hu_cities=set(),
    )
    assert summary["runs"][0]["search_error"] == "DataForSEO: HTTP 500"
    assert summary["runs"][0]["search_failed"] == 1


def test_run_outcome_classification():
    from scraper.pipeline import classify_run_outcome

    clean = [{"city": "Stockholm", "topic": "running"}]
    assert classify_run_outcome(clean) == "ok"
    assert classify_run_outcome(clean, "boom") == "aborted"

    # one transient pair failure out of many — retried next run, not an abort
    warned = clean + [{"city": "Malmö", "topic": "chess", "search_failed": True,
                       "search_error": "DataForSEO standard task timed out"}]
    assert classify_run_outcome(warned) == "warning"

    # the marker entry a provider-death abort leaves behind
    dead = clean + [{"city": "Malmö", "topic": "chess", "search_failed": True,
                     "search_error": "providers unavailable", "aborted": True}]
    assert classify_run_outcome(dead) == "aborted"

    assert classify_run_outcome([{"city": "A", "topic": "b", "extract_failed": 3}]) == "warning"


def test_outcome_persisted_and_warning_counts_as_finished(tmp_path):
    """A warning run must not look interrupted: get_last_run/get_last_run_mode
    drive startup recovery, which would otherwise re-run it forever."""
    import json
    from datetime import datetime, timedelta, timezone

    from scraper.db import get_last_run, get_last_run_row, get_run_detail, get_run_history

    db = _db(tmp_path)
    started = datetime.now(timezone.utc)
    run_id = start_run(db, started, "search_only")
    logs = [{"city": "Malmö", "topic": "chess", "search_failed": True,
             "search_error": "timed out"}]
    finish_run(db, run_id, started + timedelta(seconds=1), True, json.dumps(logs),
               outcome="warning")

    assert get_last_run_row(db)["outcome"] == "warning"
    assert get_run_detail(db, run_id)["outcome"] == "warning"
    assert get_run_history(db)[0]["outcome"] == "warning"
    assert get_last_run(db) is not None  # counted as a completed run


def test_legacy_run_rows_map_onto_the_three_states(tmp_path):
    from datetime import datetime, timedelta, timezone

    from scraper.db import _connect, get_run_history

    db = _db(tmp_path)
    started = datetime.now(timezone.utc)
    ok_id = start_run(db, started, "full")
    bad_id = start_run(db, started, "full")
    finish_run(db, ok_id, started + timedelta(seconds=1), True)
    finish_run(db, bad_id, started + timedelta(seconds=1), False)
    # simulate rows written before the column existed
    with _connect(db) as conn:
        conn.execute("UPDATE runs SET outcome=NULL")
        conn.commit()

    outcomes = {r["id"]: r["outcome"] for r in get_run_history(db)}
    assert outcomes[ok_id] == "ok"
    assert outcomes[bad_id] == "aborted"


def test_daily_summary_includes_persisted_run_error(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = _db(tmp_path)
    started = datetime.now(timezone.utc)
    run_id = start_run(db, started, "ai_only")
    finish_run(db, run_id, started + timedelta(seconds=1), False,
               error="malformed cache row")
    summary = get_daily_summary(
        db,
        (started - timedelta(seconds=1)).isoformat(),
        (started + timedelta(seconds=2)).isoformat(),
        hu_cities=set(),
    )
    assert summary["runs"][0]["error"] == "malformed cache row"


def test_daily_summary_does_not_claim_unfinished_run_definitely_crashed(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = _db(tmp_path)
    started = datetime.now(timezone.utc)
    start_run(db, started, "ai_only")
    summary = get_daily_summary(
        db,
        (started - timedelta(seconds=1)).isoformat(),
        (started + timedelta(seconds=1)).isoformat(),
        hu_cities=set(),
    )
    assert summary["runs"][0]["error"] == (
        "run unfinished (still running, container restart, or OOM)"
    )


def test_report_html_stock_section():
    """The Állomány table shows current totals per scope, not just the diff."""
    empty = {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                            "new_venues", "new_persons", "pages_scraped",
                            "pages_extracted", "searches")}
    summary = {
        "hu": dict(empty), "intl": dict(empty), "runs": [],
        "totals": {"hu": 700, "intl": 300, "covered_pairs_hu": 50, "covered_pairs_intl": 20},
        "stock": {
            "hu": {"communities": 700, "venues": 400, "persons": 90,
                   "pages_cached": 5000, "pages_extracted": 4800, "covered_pairs": 50},
            "intl": {"communities": 300, "venues": 100, "persons": 10,
                     "pages_cached": 2000, "pages_extracted": 1500, "covered_pairs": 20},
        },
    }
    _, html = build_report_html("2026-07-09", summary, {})
    assert "Állomány (aktuális összesen)" in html
    for frag in (">700<", ">300<", ">1000<",     # communities hu/intl/total
                 ">400<", ">500<",               # venues hu + total
                 ">5000<", ">7000<",             # pages cached hu + total
                 ">6300<", ">70<"):              # pages extracted total, covered pairs total
        assert frag in html, f"hiányzik: {frag}"


def test_middleware_counts_public_html_only(tmp_path):
    from fastapi.testclient import TestClient
    from scraper.pipeline import CityConfig, TopicConfig
    from scraper.web import app as web_app
    from scraper.web.state import app_state

    db = _db(tmp_path)
    old_db, old_cities, old_topics = app_state.db_path, app_state.cities, app_state.topics
    app_state.db_path = db
    app_state.cities = [CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])]
    app_state.topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
    try:
        c = TestClient(web_app.app)
        human = {"user-agent": "Mozilla/5.0 (Macintosh) Safari/605.1", "host": "kozossegek.com"}
        bot = {"user-agent": "Googlebot/2.1", "host": "kozossegek.com"}
        c.get("/", headers=human)
        c.get("/", headers=bot)                    # bot: skipped
        c.get("/robots.txt", headers=human)        # utility: skipped
        t = get_traffic_for_day(db, __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d"))
        assert t.get("kozossegek", {}).get("pageviews", 0) == 1
    finally:
        app_state.db_path, app_state.cities, app_state.topics = old_db, old_cities, old_topics


def test_report_html_ga4_mode():
    summary = {
        "hu": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                              "new_venues", "new_persons", "pages_scraped",
                              "pages_extracted", "searches")},
        "intl": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                                "new_venues", "new_persons", "pages_scraped",
                                "pages_extracted", "searches")},
        "runs": [], "totals": {"hu": 0, "intl": 0,
                               "covered_pairs_hu": 0, "covered_pairs_intl": 0},
    }
    traffic = {"kozossegek": {"pageviews": 3, "visitors": 2}}
    ga4 = {"kozossegek": {"visitors": 87, "sessions": 110, "pageviews": 240},
           "meetapedia": {"visitors": 13, "sessions": 15, "pageviews": 30}}
    subject, html = build_report_html("2026-07-09", summary, traffic, ga4)
    assert "100 látogató" in subject          # GA4 wins over the server counter
    assert "Látogatók (GA4)" in html and "Munkamenet" in html
    assert ">87<" in html and ">125<" in html  # per-site + total sessions
    # without GA4: falls back to the server counter
    subject2, html2 = build_report_html("2026-07-09", summary, traffic, None)
    assert "2 látogató" in subject2 and "(GA4)" not in html2


def test_report_shows_free_ai_usage_per_provider(tmp_path):
    """The daily email carries a free-tier AI block.

    The router's whole premise is that extraction stays inside allowances
    nobody pays for. A provider quietly hitting its ceiling every day costs
    coverage without costing money — the failure mode that hides unless it is
    on the daily report.
    """
    from scraper.db import init_db, record_provider_call
    from scraper.report import build_report_html

    db = tmp_path / "scraper.db"
    init_db(db)
    record_provider_call(db, "2026-08-16", "groq")

    summary = {
        "hu": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                              "new_venues", "new_persons", "pages_scraped",
                              "pages_extracted", "searches")},
        "intl": {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                                "new_venues", "new_persons", "pages_scraped",
                                "pages_extracted", "searches")},
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
        "runs": [],
        "providers": [
            {"name": "groq", "used": 120, "budget": 13680, "observed_limit": None,
             "rate_limits": 0, "failures": 0},
            {"name": "openrouter", "used": 47, "budget": 47, "observed_limit": 50,
             "rate_limits": 3, "failures": 1},
        ],
    }
    _, html = build_report_html("2026-08-16", summary, {}, None)

    assert "AI-keret" in html
    assert "groq" in html and "openrouter" in html
    assert "13680" in html and "120" in html
    # A spent provider must read as spent, not as a quiet row.
    assert "100%" in html
    assert "észlelt plafon 50" in html and "3× 429" in html


def test_report_omits_the_ai_block_when_nothing_was_called(tmp_path):
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
    _, html = build_report_html("2026-08-16", summary, {}, None)
    assert "Ingyenes AI-keret" not in html


def test_the_spend_line_names_each_workload():
    """Enrichment, extraction and "other" spend the same allowance.

    Undifferentiated, the line read "8.9 calls/page, ~239 pages/day" on
    2026-08-23 while 384 of the 936 calls had written descriptions — and that
    figure was used to size a paid-model decision. Both parts are measured at
    their source now; what is left over is preflight and the /v1 gateway, which
    is other people's software and is named rather than blamed on extraction.
    """
    from scraper.report import build_report_html

    def _blank():
        return {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                               "new_venues", "new_persons", "pages_scraped",
                               "pages_extracted", "searches")}

    hu = _blank()
    hu["pages_extracted"] = 105
    summary = {
        "hu": hu, "intl": _blank(),
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
        "runs": [],
        "enrich_attempts": 384, "extract_attempts": 1200, "guide_attempts": 10,
        # The 2026-08-23 fleet, verbatim: 1,794 attempts, 858 failures.
        "providers": [
            {"name": "mistral", "configured": True, "used": 485, "budget": 475,
             "failures": 9, "rate_limits": 0, "tokens": 816995},
            {"name": "groq", "configured": True, "used": 200, "budget": 186,
             "failures": 116, "rate_limits": 45, "tokens": 164113},
            {"name": "gemini", "configured": True, "used": 1062, "budget": 1425,
             "failures": 721, "rate_limits": 704, "tokens": 439651},
            {"name": "openrouter", "configured": True, "used": 47, "budget": 47,
             "failures": 12, "rate_limits": 6, "tokens": 82160},
        ],
    }
    _, html = build_report_html("2026-08-23", summary, {}, None, None)

    assert "1794 hívás" in html
    assert "1200 kinyerés" in html
    assert "384 leírás" in html
    assert "10 útmutató" in html
    assert "200 egyéb" in html          # 1794 - 1200 - 384 - 10
    assert "105 feldolgozott oldal" in html
    # No derived capacity: the allowance is not one scalar (Groq is token-bound).
    assert "kapacitás" not in html
    assert "oldal/nap" not in html


def test_a_run_that_aborts_at_preflight_still_records_its_attempts():
    """A counter written only on the happy path is the bug it was added to fix.

    `preflight()` probes every provider, so an aborted run has already spent
    attempts. Losing them makes the "other" bucket absorb the difference.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("scraper/pipeline.py").read_text(encoding="utf-8"))
    persisted = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_persist_attempts"}
    assert "_persist_attempts" in persisted

    # Both exits: the preflight abort and the normal end of the run.
    src = Path("scraper/pipeline.py").read_text(encoding="utf-8")
    assert src.count("_persist_attempts(") >= 3      # def + abort + throughput
    i = src.index("extractor_preflight_failed")
    assert "_persist_attempts(" in src[i:i + 400]


def test_more_calls_than_the_ledger_saw_is_reported_not_clamped():
    """The two counters measure the same universe; a gap is a finding.

    On 2026-09-10 the line read "2400 hívás (11 kinyerés 2641 leírás)" — its own
    numbers exceeding its own total by 252, with nothing said about it, because
    `max(0, ...)` swallowed the difference. The ledger is what governs routing,
    so calls it never saw are calls no budget was charged for, and undercounting
    a budget is how a router walks into a hard block. The excess is the one
    direction that must not be rounded away.
    """
    from scraper.report import build_report_html

    def _blank():
        return {k: 0 for k in ("new_communities", "changed_communities", "change_rows",
                               "new_venues", "new_persons", "pages_scraped",
                               "pages_extracted", "searches")}

    hu = _blank()
    hu["pages_extracted"] = 7
    summary = {
        "hu": hu, "intl": _blank(),
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
        "runs": [],
        # The 2026-09-10 figures, verbatim.
        "enrich_attempts": 2641, "extract_attempts": 11,
        "providers": [
            {"name": "mistral", "configured": True, "used": 433, "budget": 475,
             "failures": 409, "rate_limits": 407, "tokens": 21799},
            {"name": "openrouter", "configured": True, "used": 950, "budget": 950,
             "failures": 28, "rate_limits": 24, "tokens": 2261314},
            {"name": "gemini", "configured": True, "used": 510, "budget": 475,
             "failures": 459, "rate_limits": 454, "tokens": 82784},
            {"name": "groq", "configured": True, "used": 257, "budget": 950,
             "failures": 164, "rate_limits": 23, "tokens": 188422},
            {"name": "cloudflare", "configured": True, "used": 95, "budget": 95,
             "failures": 4, "rate_limits": 0, "tokens": 240004},
            {"name": "localgpu", "configured": True, "used": 136, "budget": 95000,
             "failures": 136, "rate_limits": 0, "tokens": 0},
            {"name": "gemini_flash", "configured": True, "used": 19, "budget": 19,
             "failures": 19, "rate_limits": 18, "tokens": 0},
        ],
    }
    _, html = build_report_html("2026-09-10", summary, {}, None, None)

    assert "2400 hívás" in html
    assert "252" in html                 # 2652 - 2400, named rather than hidden
    assert "kvótakönyvelés" in html
    assert "egyéb" not in html           # there is no surplus to attribute
