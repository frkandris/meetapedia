import hashlib
import sqlite3
from pathlib import Path

from scraper.db import (
    bulk_upsert_communities,
    get_collected_pairs,
    get_fully_processed_pairs,
    init_db,
    save_cache_page,
    save_search_cache,
    mark_search_collection_complete,
)


URL = "https://example.com/community"


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "scraper.db"
    init_db(db)
    save_search_cache(db, "Budapest", "running", [URL], ["query"])
    return db


def _save_page(db: Path, **updates) -> None:
    entry = {
        "url": URL,
        "url_hash": hashlib.sha256(URL.encode()).hexdigest()[:16],
        "city": "Budapest",
        "topic": "running",
        "scraped_at": "2026-01-01T00:00:00+00:00",
        "extract_fingerprint": "community-v2",
        "venue_fingerprint": "venue-v2",
        "person_fingerprint": "person-v2",
        "records": [{"name": "Futók"}],
        "venues_data": [],
        "persons_data": {"Budapest/running": []},
    }
    entry.update(updates)
    save_cache_page(db, entry)


def _done(db: Path, **flags) -> set[tuple[str, str]]:
    return get_fully_processed_pairs(
        db,
        "community-v2",
        "venue-v2",
        "person-v2",
        **flags,
    )


def test_stale_community_fingerprint_keeps_green_pair_runnable(tmp_path):
    db = _db(tmp_path)
    bulk_upsert_communities(db, [{
        "name": "Budapest Futók",
        "city": "Budapest",
        "topic": "running",
        "community_id": "visible-community",
    }])
    _save_page(db, extract_fingerprint="community-v1")

    assert _done(db, run_communities=True) == set()


def test_enabled_venue_and_person_fingerprints_are_required(tmp_path):
    db = _db(tmp_path)
    _save_page(db, venue_fingerprint="venue-v1")
    assert _done(db, run_communities=True, run_venues=True) == set()
    assert _done(db, run_communities=True, run_venues=False) == {
        ("Budapest", "running")
    }

    _save_page(db, persons_data={})
    assert _done(db, run_communities=True, run_persons=True) == set()


def test_empty_community_result_skips_gated_venue_and_person_requirements(tmp_path):
    db = _db(tmp_path)
    _save_page(
        db,
        records=[],
        venue_fingerprint=None,
        person_fingerprint=None,
        persons_data={},
    )

    assert _done(
        db,
        run_communities=True,
        run_venues=True,
        run_persons=True,
    ) == {("Budapest", "running")}


def test_null_community_result_is_not_considered_processed(tmp_path):
    db = _db(tmp_path)
    _save_page(db, records=None)

    assert _done(db, run_communities=True) == set()


def test_search_collection_requires_terminal_batch_marker(tmp_path):
    db = _db(tmp_path)
    assert get_collected_pairs(db, max_pages=1) == set()

    _save_page(db)

    # Scraping a URL is not enough: the terminal marker is written only after
    # the collector has attempted the complete selected URL batch.
    assert get_collected_pairs(db, max_pages=1) == set()
    mark_search_collection_complete(db, "Budapest", "running")

    assert get_collected_pairs(db, max_pages=1) == {("Budapest", "running")}


def test_init_db_backfills_legacy_search_rows_as_collected(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE search_cache (
                city TEXT NOT NULL, topic TEXT NOT NULL, urls TEXT NOT NULL,
                queries TEXT NOT NULL, cached_at TEXT NOT NULL,
                PRIMARY KEY (city, topic)
            )
        """)
        conn.execute(
            "INSERT INTO search_cache VALUES (?,?,?,?,?)",
            ("Budapest", "running", "[]", "[]", "2026-07-13T01:00:00+00:00"),
        )
    init_db(db)
    assert get_collected_pairs(db, max_pages=5) == {("Budapest", "running")}


def test_extraction_done_check_ignores_urls_beyond_fetch_cap(tmp_path):
    db = _db(tmp_path)
    extra_url = "https://example.com/beyond-cap"
    save_search_cache(db, "Budapest", "running", [URL, extra_url], ["query"])
    _save_page(db)
    save_cache_page(db, {
        "url": extra_url,
        "url_hash": hashlib.sha256(extra_url.encode()).hexdigest()[:16],
        "scraped_at": "2026-01-01T00:00:00+00:00",
        "extract_fingerprint": "community-v1",
        "records": [{"name": "Stale"}],
    })

    assert get_fully_processed_pairs(
        db,
        "community-v2",
        run_communities=True,
        max_pages=1,
    ) == {("Budapest", "running")}


def test_the_filter_reads_only_the_columns_the_run_needs(tmp_path):
    """/v1/backlog timed out past 200 seconds on ~207K cached pages.

    The query pulled `persons_data` out of every `data` blob and re-parsed it
    in Python for callers that never look at it — the endpoint and every
    `search_only` run take the defaults, where run_venues and run_persons are
    both false.
    """
    import contextlib
    from unittest.mock import patch

    import scraper.db as dbmod

    db = _db(tmp_path)
    _save_page(db)

    def _statements(**flags) -> str:
        seen: list[str] = []
        real_connect = dbmod._connect

        @contextlib.contextmanager
        def _traced(*a, **kw):
            with real_connect(*a, **kw) as conn:
                conn.set_trace_callback(seen.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        with patch.object(dbmod, "_connect", _traced):
            get_fully_processed_pairs(db, "community-v2", "venue-v2", "person-v2", **flags)
        return [sql for sql in seen if "FROM cache_pages" in sql]

    lean = _statements()
    bulk = [sql for sql in lean if "records_count IS NULL" not in sql]
    assert len(bulk) == 1, bulk
    assert "persons_data" not in bulk[0]
    assert "venue_fingerprint" not in bulk[0]
    # The scan that reads every page must not open the blob: `data` is ~30 KB a
    # row in production, and that is the whole reason for the column. The only
    # statement allowed to touch it is the one scoped to un-backfilled rows.
    assert "data" not in bulk[0]
    lean = " ".join(lean)

    full = " ".join(_statements(run_venues=True, run_persons=True))
    assert "persons_data" in full
    assert "venue_fingerprint" in full


def test_a_page_extracted_with_no_communities_is_still_finished(tmp_path):
    """0 records is a done page; NULL is a page that never ran.

    The filter used to ask `json_type(data,'$.records') = 'array'`, which says
    "the extraction stored an array" whether or not it found anything. The
    replacement column has to keep that distinction or every empty page is
    re-extracted forever.
    """
    db = _db(tmp_path)
    _save_page(db, records=[])
    assert ("Budapest", "running") in _done(db)


def test_a_row_the_backfill_has_not_reached_is_read_from_the_blob(tmp_path):
    """Correctness must not wait for a migration.

    Calling a NULL "never extracted" would send the whole corpus back for
    re-extraction the moment this shipped — at the free fleet's ~650 pages a
    day, a year of work.
    """
    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE cache_pages SET records_count = NULL")
        con.commit()
    assert ("Budapest", "running") in _done(db)


def test_a_page_that_never_ran_is_still_not_done(tmp_path):
    """The other half: no records key at all means the work is outstanding."""
    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE cache_pages SET records_count = NULL,"
                    " data = json_remove(data, '$.records')")
        con.commit()
    assert ("Budapest", "running") not in _done(db)


def test_the_backfill_fills_the_column_from_the_blob(tmp_path):
    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE cache_pages SET records_count = NULL")
        con.commit()
    from scraper.db import backfill_records_count

    assert backfill_records_count(db) == 1
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 1


def test_the_filter_scans_an_index_not_the_table(tmp_path):
    """The whole point: `data` is ~30 KB a row, and a table scan reads it all.

    Naming fewer columns did not help — SQLite still walked 6 GB of rows.
    Measured on a synthetic copy of production, a covering index took the
    filter from 11.03 s to 0.31 s.
    """
    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        plan = " ".join(str(step) for step in con.execute(
            "EXPLAIN QUERY PLAN SELECT url_hash, extract_fingerprint, records_count"
            " FROM cache_pages WHERE scraped_at IS NOT NULL").fetchall())
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index'"
            " AND name='idx_cache_pages_done'").fetchone()
    # An index is used at all…
    assert "SCAN cache_pages USING INDEX" in plan, plan
    # …and it is the one that covers the query. EXPLAIN QUERY PLAN cannot tell
    # a covering partial index from a thin one — both read
    # "SCAN cache_pages USING INDEX <name>" — so the plan alone passes happily
    # with an index on one column, which would put the 6 GB table back in the
    # scan. Assert on what makes it covering instead: every column the bulk
    # query reads, and the predicate it filters on.
    assert ddl, "idx_cache_pages_done is missing"
    for column in ("url_hash", "extract_fingerprint", "records_count"):
        assert column in ddl[0], (column, ddl[0])
    assert "scraped_at IS NOT NULL" in ddl[0], ddl[0]


def test_the_plain_writer_keeps_the_count_current(tmp_path):
    """`save_cache_page` must carry the new count through ON CONFLICT.

    Checking the source for `records_count` passes even with the ON CONFLICT
    clause deleted, which is precisely the case that leaves a stale count after
    a re-extraction.
    """
    db = _db(tmp_path)
    _save_page(db, records=[{"name": "A"}, {"name": "B"}])
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 2

    _save_page(db, records=[])          # re-extraction found nothing
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 0

    _save_page(db, records=[{"name": "C"}])
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 1


def test_the_pipeline_writer_sets_the_count(tmp_path):
    """`_write_cache_page` is the other full-row writer and missed it once.

    A row left NULL reads as never-extracted, so its pair is re-extracted on
    every run — forever, and silently.
    """
    from scraper.db import update_cache_page

    db = _db(tmp_path)
    uh = hashlib.sha256(URL.encode()).hexdigest()[:16]
    update_cache_page(db, uh, create={
        "url": URL, "city": "Budapest", "topic": "running",
        "scraped_at": "2026-01-01T00:00:00+00:00",
        "extract_fingerprint": "community-v2",
        "records": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
    })
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 3


def test_the_merging_writer_keeps_the_count_current(tmp_path):
    """Re-extraction changes the count; ON CONFLICT must carry the new one over.

    `update_cache_page` is the read-merge-write path used by the admin queue
    and the router's upgrade sweep. Leaving a stale count here would mark a
    page as holding communities it no longer has.
    """
    from scraper.db import update_cache_page

    db = _db(tmp_path)
    _save_page(db, records=[{"name": "A"}, {"name": "B"}])
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 2

    uh = hashlib.sha256(URL.encode()).hexdigest()[:16]
    update_cache_page(db, uh, {"records": []})
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 0


def test_the_backfill_releases_the_write_lock_between_chunks(tmp_path):
    """One UPDATE over the whole table rewrites every ~30 KB row.

    On the production corpus that is a multi-minute write holding SQLite's
    single writer slot, with the crawler and every request queued behind it.
    """
    from scraper.db import _backfill_records_count

    db = _db(tmp_path)
    rows = 4500  # more than two chunks
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT OR REPLACE INTO cache_pages"
            " (url, url_hash, city, topic, scraped_at, extract_fingerprint, data)"
            " VALUES (?,?,?,?,?,?,?)",
            # Half of them scraped and never extracted — the shape the backlog
            # is mostly made of, and the one an earlier version left NULL
            # forever, so the fallback opened every one of their blobs on every
            # scan. Seeding only extracted rows made this test prove nothing.
            [(f"https://e.test/{i}", f"h{i:012d}", "Budapest", "running",
              "2026-01-01T00:00:00+00:00", "community-v2",
              '{"records": [{"name": "X"}]}' if i % 2 else '{"text": "raw page"}')
             for i in range(rows)])
        con.commit()

    commits: list[int] = []
    with sqlite3.connect(db) as con:
        real_commit = con.commit
        con.set_trace_callback(lambda sql: commits.append(1) if "COMMIT" in sql.upper() else None)
        _backfill_records_count(con)
        real_commit()
        remaining = con.execute(
            "SELECT COUNT(*) FROM cache_pages WHERE records_count IS NULL").fetchone()[0]
    assert remaining == 0, "un-extracted pages must be filled too, not left NULL"
    # 4,500 rows at 2,000 a chunk cannot be one transaction.
    assert len(commits) >= 3, commits


def test_init_db_does_not_run_the_backfill(tmp_path):
    """~97 s for the production corpus, and init_db runs from a dozen routes.

    Correctness does not need it — the filter falls back to the blob — so it
    belongs after boot, not in the startup path or inside a request.
    """
    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE cache_pages SET records_count = NULL")
        con.commit()

    init_db(db, force=True)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] is None
    # …but the index it needs is created there, because that is cheap and once.
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
            " AND name='idx_cache_pages_done'").fetchone()[0] == 1


def test_invalidating_extraction_returns_the_page_to_unextracted(tmp_path):
    """The column mirrors `$.records`, so the two leave together — as -1.

    Not NULL: NULL means "the backfill has not reached this row", and writing
    it here would manufacture rows whose blob the fallback opens on every scan.
    The done-pair verdict is already right without any of this (the fingerprint
    goes NULL and fails the currency check first), but a column that disagrees
    with the blob it mirrors is a trap for the next reader.
    """
    from scraper.db import invalidate_extraction_cache

    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT records_count FROM cache_pages").fetchone()[0] == 1

    invalidate_extraction_cache(db)
    with sqlite3.connect(db) as con:
        count, has_records = con.execute(
            "SELECT records_count, json_extract(data, '$.records') FROM cache_pages"
        ).fetchone()
    assert count == -1, "NULL here would make the fallback open this blob forever"
    assert has_records is None
    assert ("Budapest", "running") not in _done(db)


def test_a_concurrent_backfill_cannot_unfinish_a_page(tmp_path):
    """The bulk scan and the NULL fallback must see one snapshot.

    They are separate statements and the backfill runs in the background: a row
    that flips NULL -> count between them is called unextracted by the first and
    no longer matches `records_count IS NULL` for the second to correct. The
    pair then goes back for extraction — the outcome the fallback exists to
    prevent, during exactly the window it exists for.
    """
    import scraper.db as dbmod

    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE cache_pages SET records_count = NULL")
        con.commit()

    real_connect = dbmod._connect
    flipped: list[int] = []

    class _Racing:
        """Fills the column from another connection after the first read."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

        def execute(self, sql, *a, **kw):
            cur = self._conn.execute(sql, *a, **kw)
            if "FROM cache_pages" in sql and "records_count IS NULL" not in sql \
                    and not flipped:
                flipped.append(1)
                with sqlite3.connect(db) as other:
                    other.execute("UPDATE cache_pages SET records_count = 1")
                    other.commit()
            return cur

    def _wrapped(*a, **kw):
        return _Racing(real_connect(*a, **kw))

    from unittest.mock import patch
    with patch.object(dbmod, "_connect", _wrapped):
        done = get_fully_processed_pairs(db, "community-v2")
    assert flipped, "the race was never triggered — the test proves nothing"
    assert ("Budapest", "running") in done


def test_something_actually_starts_the_backfill(tmp_path):
    """`init_db` not doing it is only half the requirement.

    Deleting the `create_task` launch left the "not in init_db" assertion
    passing, which would ship a column nothing ever fills — and the fallback
    that keeps that correct reads the blob, so the query stays slow forever.
    """
    import ast
    from pathlib import Path as _P

    tree = ast.parse(_P("scraper/main.py").read_text(encoding="utf-8"))
    launched = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "create_task":
            continue
        arg = node.args[0] if node.args else None
        called = arg.func if isinstance(arg, ast.Call) else None
        if called is not None and getattr(called, "id", "") == "_backfill_once":
            launched = True
    assert launched, "no create_task(_backfill_once()) in main.py"

    # …and that it does the work, rather than being a no-op the launch check
    # would happily accept.
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_backfill_once":
            # It is handed to asyncio.to_thread as an argument, not called
            # directly, so look for the name anywhere in the body.
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert "backfill_records_count" in names, names
            break
    else:
        raise AssertionError("_backfill_once not found")


def test_the_read_snapshot_is_closed_before_the_filter_returns(tmp_path):
    """An open read transaction in WAL stops checkpointing and stalls writers.

    The filter takes an explicit `BEGIN` so its two scans agree; it has to give
    it back, and the connection is not closed on the way out.
    """
    import scraper.db as dbmod

    db = _db(tmp_path)
    _save_page(db)

    seen: dict = {}
    real_connect = dbmod._connect

    class _Probe:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            seen["open"] = self._conn.in_transaction
            return self._conn.__exit__(*exc)

    from unittest.mock import patch
    with patch.object(dbmod, "_connect", lambda *a, **kw: _Probe(real_connect(*a, **kw))):
        get_fully_processed_pairs(db, "community-v2")
    assert seen.get("open") is False

    # And a writer is not left waiting on it.
    with sqlite3.connect(db, timeout=2) as con:
        con.execute("UPDATE cache_pages SET city='Budapest'")
        con.commit()


def test_a_scraped_but_unextracted_page_gets_the_sentinel(tmp_path):
    """The case that defeated the whole optimisation.

    Those pages have no `records` key, so a backfill keyed on
    `json_type(data,'$.records') = 'array'` skipped them and left them NULL
    forever — and the blob fallback then opened every one of them on every
    scan. They are also most of what a backlog *is*.
    """
    from scraper.db import backfill_records_count

    db = _db(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT OR REPLACE INTO cache_pages"
            " (url, url_hash, city, topic, scraped_at, data) VALUES (?,?,?,?,?,?)",
            ("https://raw.test/1", "rawhash000001", "Budapest", "running",
             "2026-01-01T00:00:00+00:00", '{"text": "page text, never extracted"}'))
        con.commit()

    assert backfill_records_count(db) == 1
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT records_count FROM cache_pages WHERE url_hash='rawhash000001'"
        ).fetchone()[0] == -1
        # Nothing left for the fallback to open.
        assert con.execute(
            "SELECT COUNT(*) FROM cache_pages"
            " WHERE scraped_at IS NOT NULL AND records_count IS NULL").fetchone()[0] == 0

    # And it is still not a finished page.
    assert ("Budapest", "running") not in _done(db)


def test_a_freshly_scraped_page_is_written_with_the_sentinel(tmp_path):
    """The most common write in the system, and the one that must not be NULL.

    Every scrape stores a page with no extraction yet. Writing NULL there would
    manufacture the defeated case on the write side: the blob fallback opening
    a ~30 KB row for every page waiting to be extracted, forever, no matter how
    complete the backfill is.
    """
    from scraper.cache import CacheManager

    db = _db(tmp_path)
    CacheManager(db).save_scraped(URL, "page text long enough to keep", "Budapest", "running")
    with sqlite3.connect(db) as con:
        count, has_records = con.execute(
            "SELECT records_count, json_extract(data, '$.records') FROM cache_pages"
        ).fetchone()
    assert has_records is None, "nothing extracted yet"
    assert count == -1, "a scraped page is 'not extracted', not 'not backfilled'"

    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM cache_pages"
            " WHERE scraped_at IS NOT NULL AND records_count IS NULL").fetchone()[0] == 0


def test_the_answer_does_not_change_across_the_migration(tmp_path):
    """The property a deploy depends on.

    The container restarts often, and the backfill is chunked, so it will be
    interrupted. What the pipeline believes is finished must be identical
    before it starts, part-way through, and after it completes — otherwise a
    restart silently re-opens work that was already done, or closes work that
    was not.
    """
    from scraper.db import backfill_records_count

    db = _db(tmp_path)
    _save_page(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE cache_pages SET records_count = NULL")
        con.commit()

    before = get_fully_processed_pairs(db, "community-v2")
    # Interrupted part-way: some rows filled, the rest still NULL.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE cache_pages SET records_count = 1 WHERE rowid <= 1")
        con.commit()
    midway = get_fully_processed_pairs(db, "community-v2")
    backfill_records_count(db)
    after = get_fully_processed_pairs(db, "community-v2")

    assert before == midway == after == {("Budapest", "running")}


def test_corpus_wide_invalidation_commits_in_chunks_and_misses_nothing(tmp_path):
    """One UPDATE over ~200K blobs held the write lock for minutes; chunks
    must still cover every row, including the last partial range.
    """
    import sqlite3

    from scraper.cache import CacheManager
    from scraper.db import _connect, _update_cache_pages_in_chunks, init_db

    db = tmp_path / "chunks.db"
    init_db(db)
    cache = CacheManager(db)
    for i in range(10):
        cache.save_scraped(f"https://x.test/{i}", "text", "Pécs", "running")
        cache.save_extracted(f"https://x.test/{i}", [], fingerprint="fp")
    with _connect(db) as conn:
        changed = _update_cache_pages_in_chunks(
            conn, "extract_fingerprint=NULL, data=json_remove(data, '$.records')",
            "extract_fingerprint IS NOT NULL", chunk=3)
    assert changed == 10
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_pages"
                            " WHERE extract_fingerprint IS NOT NULL").fetchone()[0] == 0
