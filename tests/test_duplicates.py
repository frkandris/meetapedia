import sqlite3
from pathlib import Path
from scraper.db import (
    init_db,
    insert_duplicate_candidate,
    get_duplicate_candidates,
    resolve_duplicate_candidate,
    merge_community_into,
    get_all_communities,
    get_communities,
    _community_record_key,
)
from scraper.store import save_results
from scraper.models import CommunityRecord
from scraper.duplicates import detect_community_candidates, detect_all


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def test_insert_and_get_candidates(tmp_path):
    db = _db(tmp_path)
    insert_duplicate_candidate(db, "community", "id_a", "id_b", "key_a", "key_b", 0.95, "fuzzy_name")
    rows = get_duplicate_candidates(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["entity_type"] == "community"
    assert r["winner_id"] == "id_a"
    assert r["loser_id"] == "id_b"
    assert r["similarity"] == 0.95
    assert r["signal"] == "fuzzy_name"
    assert r["resolution"] is None


def test_resolve_candidate(tmp_path):
    db = _db(tmp_path)
    insert_duplicate_candidate(db, "community", "id_a", "id_b", "key_a", "key_b", 0.95, "fuzzy_name")
    cid = get_duplicate_candidates(db)[0]["id"]
    resolve_duplicate_candidate(db, cid, "dismissed")
    rows = get_duplicate_candidates(db, resolved=False)
    assert len(rows) == 0
    rows_all = get_duplicate_candidates(db, resolved=True)
    assert rows_all[0]["resolution"] == "dismissed"


def test_no_duplicate_pair_inserted_twice(tmp_path):
    db = _db(tmp_path)
    insert_duplicate_candidate(db, "community", "id_a", "id_b", "key_a", "key_b", 0.95, "fuzzy_name")
    insert_duplicate_candidate(db, "community", "id_a", "id_b", "key_a", "key_b", 0.95, "fuzzy_name")
    rows = get_duplicate_candidates(db)
    assert len(rows) == 1  # idempotent


def test_merge_community_into_hides_loser_and_merges_urls(tmp_path):
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Budapest Futók", topic="running", city="Budapest",
                         locale="hu", source_url="https://a.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Budapest Futó Kör", topic="fitness", city="Budapest",
                         locale="hu", source_url="https://b.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Budapest", "running", [r1], db)
    save_results("Budapest", "fitness", [r2], db)

    winner_key = _community_record_key(r1.name, r1.city, r1.topic)
    loser_key = _community_record_key(r2.name, r2.city, r2.topic)

    merge_community_into(db, winner_key, loser_key)

    # get_all_communities now filters hidden=0; only the winner remains visible
    all_records = get_all_communities(db)
    assert len(all_records) == 1  # loser is hidden, only winner visible

    # Only the winner is visible (hidden=0)
    visible = get_communities(db, "Budapest", "running")
    assert len(visible) == 1
    assert visible[0]["name"] == "Budapest Futók"
    assert "https://b.test" in (visible[0].get("source_urls") or [])

    # The loser is hidden
    hidden_check = get_communities(db, "Budapest", "fitness")
    assert len(hidden_check) == 0


def test_get_resolved_excludes_pending(tmp_path):
    db = _db(tmp_path)
    insert_duplicate_candidate(db, "community", "id_a", "id_b", "key_a", "key_b", 0.95, "fuzzy_name")
    insert_duplicate_candidate(db, "community", "id_c", "id_d", "key_c", "key_d", 0.90, "fuzzy_name")
    cid = get_duplicate_candidates(db)[0]["id"]
    resolve_duplicate_candidate(db, cid, "dismissed")
    resolved_rows = get_duplicate_candidates(db, resolved=True)
    assert len(resolved_rows) == 1  # only the resolved one
    assert resolved_rows[0]["resolution"] == "dismissed"
    pending_rows = get_duplicate_candidates(db, resolved=False)
    assert len(pending_rows) == 1  # only the pending one


def test_merge_resolves_candidate_atomically(tmp_path):
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Budapest Futók", topic="running", city="Budapest",
                         locale="hu", source_url="https://a.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Budapest Futók Kör", topic="fitness", city="Budapest",
                         locale="hu", source_url="https://b.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Budapest", "running", [r1], db)
    save_results("Budapest", "fitness", [r2], db)
    # save_results auto-detects the pair; use the auto-inserted candidate id
    cid = get_duplicate_candidates(db, resolved=False)[0]["id"]
    winner_key = _community_record_key(r1.name, r1.city, r1.topic)
    loser_key = _community_record_key(r2.name, r2.city, r2.topic)
    merge_community_into(db, winner_key, loser_key, candidate_id=cid)
    # Candidate should be resolved atomically
    pending = get_duplicate_candidates(db, resolved=False)
    assert len(pending) == 0




def test_detect_cross_topic_duplicates(tmp_path):
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Budapest Futók", topic="running", city="Budapest",
                         locale="hu", source_url="https://a.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Budapest Futók Kör", topic="fitness", city="Budapest",
                         locale="hu", source_url="https://b.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Budapest", "running", [r1], db)
    save_results("Budapest", "fitness", [r2], db)

    # save_results already auto-detected; manual call returns 0 (idempotent)
    count = detect_community_candidates(db)
    assert count >= 0
    candidates = get_duplicate_candidates(db)
    assert len(candidates) >= 1
    assert candidates[0]["signal"] in ("fuzzy_name", "url_match")


def test_detect_url_match(tmp_path):
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Runners Club", topic="running", city="Budapest",
                         locale="hu", source_url="https://a.test",
                         website="https://runners.hu",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Futók Egyesület", topic="fitness", city="Budapest",
                         locale="hu", source_url="https://b.test",
                         website="https://runners.hu/",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Budapest", "running", [r1], db)
    save_results("Budapest", "fitness", [r2], db)

    # save_results already auto-detected; manual call returns 0 (idempotent)
    count = detect_community_candidates(db)
    assert count >= 0
    candidates = get_duplicate_candidates(db)
    assert any(c["signal"] == "url_match" for c in candidates)


def test_no_cross_city_false_positive(tmp_path):
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Futók Klub", topic="running", city="Budapest",
                         locale="hu", source_url="https://a.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Futók Klub", topic="running", city="Debrecen",
                         locale="hu", source_url="https://b.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Budapest", "running", [r1], db)
    save_results("Debrecen", "running", [r2], db)

    count = detect_community_candidates(db)
    assert count == 0  # different cities → not duplicates


def test_detect_all_runs_without_error(tmp_path):
    db = _db(tmp_path)
    detect_all(db)  # should not raise


def test_detect_idempotent(tmp_path):
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Budapest Futók", topic="running", city="Budapest",
                         locale="hu", source_url="https://a.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Budapest Futók Kör", topic="fitness", city="Budapest",
                         locale="hu", source_url="https://b.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Budapest", "running", [r1], db)
    save_results("Budapest", "fitness", [r2], db)
    detect_community_candidates(db)
    detect_community_candidates(db)  # second run
    candidates = get_duplicate_candidates(db)
    assert len(candidates) == 1  # only one, not two


def test_dismissed_pair_not_reinserted_on_rescan(tmp_path):
    """Dismissed duplicate pair must not reappear after a new scan."""
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Budapest Futók", topic="running", city="Budapest",
                         locale="hu", source_url="https://a.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Budapest Futók Kör", topic="fitness", city="Budapest",
                         locale="hu", source_url="https://b.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Budapest", "running", [r1], db)
    save_results("Budapest", "fitness", [r2], db)
    cid = get_duplicate_candidates(db, resolved=False)[0]["id"]
    resolve_duplicate_candidate(db, cid, "dismissed")
    # Re-scan should not bring it back
    detect_community_candidates(db)
    pending = get_duplicate_candidates(db, resolved=False)
    assert len(pending) == 0


def test_save_results_detects_cross_topic_duplicates(tmp_path):
    """After save_results, duplicates across topics in same city are auto-detected."""
    db = _db(tmp_path)
    r1 = CommunityRecord(name="Pécsi Futó Klub", topic="running", city="Pécs",
                         locale="hu", source_url="https://a.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    r2 = CommunityRecord(name="Pécsi Futók Klubja", topic="fitness", city="Pécs",
                         locale="hu", source_url="https://b.test",
                         extracted_at="2026-01-01T00:00:00+00:00")
    save_results("Pécs", "running", [r1], db)
    save_results("Pécs", "fitness", [r2], db)

    candidates = get_duplicate_candidates(db)
    assert len(candidates) >= 1
    assert candidates[0]["entity_type"] == "community"
    assert candidates[0]["signal"] == "fuzzy_name"
    assert candidates[0]["winner_key"].startswith("c2:")
    assert candidates[0]["loser_key"].startswith("c2:")


def test_reorienting_survives_a_reverse_pending_row(tmp_path):
    """A pair stored both ways round must collapse, not crash the scan.

    idx_dup_pair is partial (resolution IS NULL), so two pending rows for the
    same pair in opposite orientations are allowed to coexist — rows written
    before the lookup checked both orders. Reorienting one onto the other used
    to raise "UNIQUE constraint failed" and abort the whole post-run scan
    (production, 2026-08-17).
    """
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        for wk, lk in (("key_a", "key_b"), ("key_b", "key_a")):
            conn.execute(
                "INSERT INTO duplicate_candidates (entity_type, winner_id, loser_id,"
                " winner_key, loser_key, similarity, signal, detected_at)"
                " VALUES ('community','id_x','id_y',?,?,0.9,'fuzzy_name','2026-01-01')",
                (wk, lk),
            )

    insert_duplicate_candidate(db, "community", "id_b", "id_a",
                               "key_b", "key_a", 0.95, "manual")

    pending = get_duplicate_candidates(db, resolved=False)
    assert len(pending) == 1
    assert (pending[0]["winner_key"], pending[0]["loser_key"]) == ("key_b", "key_a")


def test_repeated_insert_of_the_same_pair_is_a_no_op(tmp_path):
    db = _db(tmp_path)
    args = ("community", "id_a", "id_b", "key_a", "key_b", 0.9, "fuzzy_name")
    assert insert_duplicate_candidate(db, *args) is True
    assert insert_duplicate_candidate(db, *args) is False
    assert len(get_duplicate_candidates(db, resolved=False)) == 1


def test_reorienting_never_deletes_an_admin_flag(tmp_path):
    """An auto re-scan must not demote a pair an admin flagged manually."""
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        for wk, lk, sig in (("key_a", "key_b", "fuzzy_name"),
                            ("key_b", "key_a", "manual")):
            conn.execute(
                "INSERT INTO duplicate_candidates (entity_type, winner_id, loser_id,"
                " winner_key, loser_key, similarity, signal, detected_at)"
                " VALUES ('community','id_x','id_y',?,?,0.9,?,'2026-01-01')",
                (wk, lk, sig),
            )

    insert_duplicate_candidate(db, "community", "id_b", "id_a",
                               "key_b", "key_a", 0.95, "fuzzy_name")

    pending = get_duplicate_candidates(db, resolved=False)
    manual = [p for p in pending if p["signal"] == "manual"]
    assert len(manual) == 1
    assert (manual[0]["winner_key"], manual[0]["loser_key"]) == ("key_b", "key_a")


def test_city_filter_happens_in_sql_not_in_the_caller(tmp_path):
    """One city's duplicate scan must not read the whole table.

    Every row carries the record's entire JSON blob, so filtering in Python
    parses 45K blobs in production to answer a question about a few dozen —
    once per processed pair, because `store.save_results` calls this.
    """
    db = _db(tmp_path)
    for city in ("Budapest", "Szentendre"):
        save_results(city, "running", [CommunityRecord(
            name=f"{city} Futók", topic="running", city=city, locale="hu",
            source_url="https://a.test",
            extracted_at="2026-01-01T00:00:00+00:00")], db)

    assert [r["city"] for r in get_all_communities(db, city="Szentendre")] == ["Szentendre"]
    assert len(get_all_communities(db)) == 2

    seen: dict = {}
    import scraper.duplicates as dup
    original = dup.get_all_communities

    def _spy(db_path, city=None):
        seen["city"] = city
        return original(db_path, city=city)

    dup.get_all_communities = _spy
    try:
        dup.detect_community_candidates(db, city="Szentendre")
    finally:
        dup.get_all_communities = original
    assert seen["city"] == "Szentendre"


def test_city_filtered_scan_uses_the_city_index(tmp_path):
    """The filtered query must SEARCH, not SCAN.

    Asserting on the plan rather than on a timing keeps this honest if someone
    drops `idx_comm_city_topic` — a scan would still return the right rows and
    every other test would stay green.
    """
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT data FROM communities "
            "WHERE hidden=0 AND city=? ORDER BY city, topic, id", ("Budapest",)
        ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "SEARCH" in detail and "idx_comm_city_topic" in detail, detail
