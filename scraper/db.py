import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .identity import (
    community_record_key as _community_record_key,
    normalized_match_key,
    person_record_key as _person_record_key,
    venue_record_key as _venue_record_key,
)

_UNICODE_RECORD_KEYS_MIGRATION = "unicode_record_keys_v2"
log = structlog.get_logger()


#: WAL is enabled once per process, not per connection — the mode is a property
#: of the database file and the PRAGMA is a write, so doing it on every connect
#: adds a lock acquisition to every query.
_wal_enabled: set[str] = set()


def _connect(db_path: Path) -> sqlite3.Connection:
    # One lock wait, set once. `timeout=30` used to be followed by
    # `busy_timeout = 5000`, which silently overrode it: any write held for
    # more than 5 s surfaced as "database is locked" in the worker.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    key = str(db_path)
    if key not in _wal_enabled:
        # Write-Ahead Logging lets readers proceed while a writer holds the
        # database. Without it the pipeline's writes block every HTTP request:
        # 2026-08-16 produced "database is locked" and 30-second page loads once
        # the Hungarian import pushed the write volume up. Readers and one
        # writer can now run concurrently.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL is the standard companion to WAL: durable across process
            # crashes, only at risk in an OS-level crash, and far fewer fsyncs.
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:  # e.g. a read-only mount
            log.warning("wal_enable_failed", error=str(exc))
        _wal_enabled.add(key)
    return conn


_FINGERPRINT_BACKFILL_MIGRATION = "cache_pages_fingerprint_columns_v1"


def _migrate_unicode_record_keys(conn: sqlite3.Connection) -> None:
    """Rewrite legacy ASCII-only keys and every persisted reference once."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    if conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name=?",
        (_UNICODE_RECORD_KEYS_MIGRATION,),
    ).fetchone():
        return

    mappings: dict[str, dict[str, str]] = {
        "community": {},
        "venue": {},
        "person": {},
    }
    for old_key, data_json in conn.execute("SELECT record_key, data FROM communities"):
        data = json.loads(data_json)
        mappings["community"][old_key] = _community_record_key(
            data.get("name", ""), data.get("city", ""), data.get("topic", "")
        )
    for old_key, data_json in conn.execute("SELECT record_key, data FROM venues"):
        data = json.loads(data_json)
        mappings["venue"][old_key] = _venue_record_key(
            data.get("name", ""), data.get("city", "")
        )
    for old_key, data_json in conn.execute("SELECT record_key, data FROM persons"):
        data = json.loads(data_json)
        mappings["person"][old_key] = _person_record_key(
            data.get("name", ""),
            data.get("city", ""),
            data.get("role", "leader"),
            data.get("community_name", ""),
        )

    table_for_type = {
        "community": "communities",
        "venue": "venues",
        "person": "persons",
    }
    for entity_type, key_map in mappings.items():
        table = table_for_type[entity_type]
        for old_key, new_key in key_map.items():
            if old_key == new_key:
                continue
            conn.execute(
                f"UPDATE {table} SET record_key=? WHERE record_key=?",
                (new_key, old_key),
            )
            conn.execute(
                "UPDATE duplicate_candidates SET winner_key=?"
                " WHERE entity_type=? AND winner_key=?",
                (new_key, entity_type, old_key),
            )
            conn.execute(
                "UPDATE duplicate_candidates SET loser_key=?"
                " WHERE entity_type=? AND loser_key=?",
                (new_key, entity_type, old_key),
            )
            conn.execute(
                "UPDATE edit_requests SET record_key=?"
                " WHERE entity_type=? AND record_key=?",
                (new_key, entity_type, old_key),
            )

    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (_UNICODE_RECORD_KEYS_MIGRATION, datetime.now(timezone.utc).isoformat()),
    )


#: Databases already initialised in this process. init_db is documented as safe
#: to call on every request, and a dozen routes do — but it issues CREATE TABLE
#: and ALTER TABLE, i.e. it takes a *write* lock. Under a writing pipeline that
#: turned every admin page load into a lock wait. The schema cannot change
#: mid-process, so once is enough.
_initialised: set[str] = set()


def backfill_records_count(db_path: Path) -> int:
    """Fill `records_count` from the blob. Returns how many rows it wrote.

    Deliberately NOT part of `init_db`. Measured on a 6.15 GB synthetic copy of
    the corpus it takes ~97 seconds for 207K rows, and `init_db` runs on the
    startup path and from a dozen routes — a two-minute migration there is a
    two-minute deploy stall or a two-minute request. It is safe to run late,
    and safe to interrupt, because the filter reads the blob for whatever rows
    it has not reached yet.
    """
    if not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        return _backfill_records_count(conn)


def _backfill_records_count(conn: sqlite3.Connection) -> int:
    """Fill `records_count` from the blob, in chunks that release the lock.

    One `UPDATE cache_pages SET …` over the whole table rewrites every row, and
    the rows carry a ~30 KB blob each: on the production corpus that is a
    multi-minute write holding SQLite's single writer slot, with the crawler
    and every request behind it. Chunked with a commit between, other writers
    interleave and the worst case is a slow migration rather than a stalled app.

    Correctness does not depend on this finishing — `get_fully_processed_pairs`
    reads the blob for whatever rows are still NULL. That matters more than it
    sounds: treating an un-backfilled row as unextracted would send the whole
    corpus back for re-extraction, which at the free fleet's ~650 pages a day
    is a year of work.
    """
    _CHUNK = 2000
    filled = 0
    while True:
        cur = conn.execute("""
            UPDATE cache_pages
            SET records_count = CASE
                WHEN json_type(data, '$.records') = 'array'
                THEN json_array_length(json_extract(data, '$.records'))
                ELSE -1
            END
            WHERE rowid IN (
                SELECT rowid FROM cache_pages
                WHERE records_count IS NULL AND scraped_at IS NOT NULL
                LIMIT ?
            )
        """, (_CHUNK,))
        conn.commit()
        if not cur.rowcount:
            break
        filled += cur.rowcount
    if filled:
        log.info("records_count_backfilled", rows=filled)
    return filled


def init_db(db_path: Path, force: bool = False) -> None:
    """Create/migrate every table. Idempotent, and now also *cheap to call*.

    Routes are documented as free to call this per request, and a dozen do —
    but the body is CREATE TABLE / ALTER TABLE, which takes a write lock. With
    the pipeline writing concurrently that produced "database is locked" and
    multi-second page loads (2026-08-16). The schema cannot change within a
    process, so the work runs once per path; pass force=True in tests that
    rebuild a database at the same location.
    """
    key = str(db_path)
    if not force and key in _initialised:
        return
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                run_mode    TEXT NOT NULL DEFAULT 'full',
                success     INTEGER NOT NULL DEFAULT 1,
                search_log  TEXT,
                error       TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE runs ADD COLUMN search_log TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE runs ADD COLUMN new_records INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE runs ADD COLUMN error TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            # 'ok' | 'warning' | 'aborted'. NULL on rows written before
            # 2026-07-31; readers fall back to the boolean via _OUTCOME_SQL.
            conn.execute("ALTER TABLE runs ADD COLUMN outcome TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS city_requests (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                city_name  TEXT NOT NULL,
                email      TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL,
                city       TEXT NOT NULL,
                topic      TEXT NOT NULL,
                token      TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_uniq
            ON subscriptions(email, city, topic)
        """)

        # Communities — one row per unique (name, city, topic), full record as JSON
        conn.execute("""
            CREATE TABLE IF NOT EXISTS communities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                record_key   TEXT NOT NULL UNIQUE,
                community_id TEXT NOT NULL,
                city         TEXT NOT NULL,
                topic        TEXT NOT NULL,
                data         TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comm_city_topic ON communities(city, topic)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comm_community_id ON communities(community_id)"
        )
        try:
            conn.execute("ALTER TABLE communities ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Materialized, data-derived editorial pages.  A row is a publication
        # event, not a live query: its bounded record snapshot keeps the page
        # reproducible even while the crawler continues changing the corpus.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS data_guides (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT NOT NULL UNIQUE,
                site         TEXT NOT NULL DEFAULT 'kozossegek',
                city         TEXT NOT NULL,
                topic        TEXT NOT NULL,
                locale       TEXT NOT NULL DEFAULT 'hu',
                title        TEXT NOT NULL,
                summary      TEXT NOT NULL,
                data         TEXT NOT NULL,
                published_at TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                UNIQUE(site, city, topic)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_data_guides_site_published
            ON data_guides(site, published_at DESC)
        """)
        # Cache pages — full JSON entry per scraped URL
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_pages (
                url_hash            TEXT PRIMARY KEY,
                url                 TEXT NOT NULL,
                city                TEXT NOT NULL DEFAULT '',
                topic               TEXT NOT NULL DEFAULT '',
                domain              TEXT NOT NULL DEFAULT '',
                scraped_at          TEXT,
                extracted_at        TEXT,
                extract_fingerprint TEXT,
                data                TEXT NOT NULL
            )
        """)
        try:
            conn.execute("ALTER TABLE cache_pages ADD COLUMN extract_fingerprint TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE cache_pages ADD COLUMN venue_fingerprint TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE cache_pages ADD COLUMN person_fingerprint TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            # Quality score (0-100) of the model that produced the cached
            # extraction. NULL = pre-router row, treated as the lowest tier so
            # an upgrade sweep can reconsider it. Read by the router's
            # re-extraction policy; never part of any cache key.
            conn.execute("ALTER TABLE cache_pages ADD COLUMN extract_quality INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE cache_pages ADD COLUMN extract_model TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            # Number of communities the extraction produced. NULL = no records
            # key in the blob, i.e. never extracted. It exists so the done-pair
            # filter never has to open `data`: that is a ~30 KB blob per row
            # across ~207K rows, and `json_type(data,'$.records')` makes SQLite
            # read every one of them. Measured on a 6.15 GB synthetic copy the
            # filter went from 0.7 s with small blobs to 13.2 s with real ones,
            # and /v1/backlog was answering 524 after 125 s in production.
            conn.execute("ALTER TABLE cache_pages ADD COLUMN records_count INTEGER")
        except sqlite3.OperationalError:
            pass
        # Backfill fingerprint columns from the JSON blob — once. It was meant
        # to run once but had no marker, and most pages keep both columns NULL
        # forever (no communities, so no venues or persons), so every process
        # start opened nearly every ~30 KB blob under the write lock before the
        # server came up. Every write since keeps the columns in sync.
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations"
                     " (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE name=?",
                            (_FINGERPRINT_BACKFILL_MIGRATION,)).fetchone():
            conn.execute("""
                UPDATE cache_pages
                SET venue_fingerprint  = json_extract(data, '$.venue_fingerprint'),
                    person_fingerprint = json_extract(data, '$.person_fingerprint')
                WHERE venue_fingerprint IS NULL AND person_fingerprint IS NULL
                  AND (json_extract(data, '$.venue_fingerprint') IS NOT NULL
                    OR json_extract(data, '$.person_fingerprint') IS NOT NULL)
            """)
            conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                         (_FINGERPRINT_BACKFILL_MIGRATION,
                          datetime.now(timezone.utc).isoformat()))
        # The done-pair filter reads three small columns from every scraped
        # page and nothing else, so give it an index that carries all three.
        # Without it SQLite scans the table, and the table is ~30 KB a row over
        # ~207K rows: a covering scan of a few megabytes instead of six
        # gigabytes. Measured on a 6.15 GB synthetic copy, the filter went from
        # **11.03 s to 0.31 s** and EXPLAIN QUERY PLAN changed from
        # "SCAN cache_pages" to "SCAN cache_pages USING INDEX". The partial
        # clause must match the query's WHERE or the index is not eligible.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cache_pages_done
            ON cache_pages(url_hash, extract_fingerprint, records_count)
            WHERE scraped_at IS NOT NULL
        """)
        conn.commit()

        # False positives
        conn.execute("""
            CREATE TABLE IF NOT EXISTS false_positives (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                city       TEXT NOT NULL,
                topic      TEXT NOT NULL,
                reason     TEXT,
                source_url TEXT,
                fp_type    TEXT NOT NULL DEFAULT 'extraction',
                marked_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fp_uniq
            ON false_positives(name, city, topic, fp_type)
        """)

        # Prompt version history
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                version   INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                content   TEXT NOT NULL,
                fp_type   TEXT NOT NULL,
                fp_count  INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Editable prompt overrides (key = prompt identifier)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_overrides (
                key        TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Search result cache — URL lists per city+topic
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                city       TEXT NOT NULL,
                topic      TEXT NOT NULL,
                urls       TEXT NOT NULL,
                queries    TEXT NOT NULL,
                cached_at  TEXT NOT NULL,
                collected_at TEXT,
                PRIMARY KEY (city, topic)
            )
        """)
        try:
            conn.execute("ALTER TABLE search_cache ADD COLUMN collected_at TEXT")
        except sqlite3.OperationalError:
            pass
        else:
            # Legacy rows were produced by runs that already attempted their fetch
            # batch. Treat them as terminally collected so permanently unreadable
            # URLs do not force a full historical retry after this migration.
            conn.execute(
                "UPDATE search_cache SET collected_at=cached_at WHERE collected_at IS NULL"
            )

        # Venues — physical locations that host communities
        conn.execute("""
            CREATE TABLE IF NOT EXISTS venues (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                record_key TEXT NOT NULL UNIQUE,
                venue_id   TEXT NOT NULL,
                city       TEXT NOT NULL,
                data       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_venues_city ON venues(city)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_venues_venue_id ON venues(venue_id)")

        # Persons — leaders, instructors, speakers linked to communities
        conn.execute("""
            CREATE TABLE IF NOT EXISTS persons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                record_key  TEXT NOT NULL UNIQUE,
                person_id   TEXT NOT NULL,
                city        TEXT NOT NULL,
                topic       TEXT NOT NULL,
                role        TEXT NOT NULL,
                data        TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_persons_city_topic ON persons(city, topic)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_persons_person_id ON persons(person_id)")

        # Field-level change history for communities
        conn.execute("""
            CREATE TABLE IF NOT EXISTS community_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                community_id TEXT NOT NULL,
                changed_at   TEXT NOT NULL,
                changed_by   TEXT NOT NULL DEFAULT 'scraper',
                field        TEXT NOT NULL,
                old_value    TEXT,
                new_value    TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_community_id ON community_history(community_id)"
        )

        # Field-level change history for venues
        conn.execute("""
            CREATE TABLE IF NOT EXISTS venue_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                venue_id   TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL DEFAULT 'scraper',
                field      TEXT NOT NULL,
                old_value  TEXT,
                new_value  TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_venue_history_venue_id ON venue_history(venue_id)"
        )

        # Field-level change history for persons
        conn.execute("""
            CREATE TABLE IF NOT EXISTS person_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id  TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL DEFAULT 'scraper',
                field      TEXT NOT NULL,
                old_value  TEXT,
                new_value  TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_person_history_person_id ON person_history(person_id)"
        )

        # User-submitted "not a community" flags — pending admin review
        conn.execute("""
            CREATE TABLE IF NOT EXISTS not_community_reports (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                community_id   TEXT,
                community_name TEXT NOT NULL,
                city           TEXT,
                topic          TEXT,
                source_url     TEXT,
                page_url       TEXT,
                reported_at    TEXT NOT NULL
            )
        """)

        # Duplicate candidate pairs detected by fuzzy matching
        conn.execute("""
            CREATE TABLE IF NOT EXISTS duplicate_candidates (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type  TEXT NOT NULL,
                winner_id    TEXT NOT NULL,
                loser_id     TEXT NOT NULL,
                winner_key   TEXT NOT NULL,
                loser_key    TEXT NOT NULL,
                similarity   REAL NOT NULL,
                signal       TEXT NOT NULL,
                detected_at  TEXT NOT NULL,
                resolved_at  TEXT,
                resolution   TEXT
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dup_pair
            ON duplicate_candidates(entity_type, winner_key, loser_key)
            WHERE resolution IS NULL
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS wrong_city_candidates (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                record_key     TEXT NOT NULL,
                community_id   TEXT NOT NULL DEFAULT '',
                mentioned_city TEXT NOT NULL,
                field          TEXT NOT NULL,
                snippet        TEXT NOT NULL DEFAULT '',
                matched_text   TEXT NOT NULL DEFAULT '',
                detected_at    TEXT NOT NULL,
                resolved_at    TEXT,
                resolution     TEXT
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wrong_city_pair
            ON wrong_city_candidates(record_key, mentioned_city)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS edit_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type  TEXT NOT NULL,
                entity_id    TEXT NOT NULL,
                entity_name  TEXT NOT NULL,
                entity_city  TEXT NOT NULL,
                entity_topic TEXT,
                record_key   TEXT NOT NULL,
                change_type  TEXT NOT NULL,
                new_value    TEXT,
                notes        TEXT NOT NULL,
                email        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT NOT NULL,
                reviewed_at  TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS community_submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                city            TEXT NOT NULL,
                topic           TEXT NOT NULL,
                source_url      TEXT NOT NULL,
                submitter_email TEXT,
                submitted_at    TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outclick_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                community_id TEXT NOT NULL,
                url          TEXT NOT NULL,
                link_type    TEXT NOT NULL DEFAULT 'website',
                clicked_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outclick_community ON outclick_events(community_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outclick_clicked_at ON outclick_events(clicked_at)"
        )
        # ── The joinability gate's own memory ────────────────────────────────
        # Deliberately NOT in cache_pages. A gate decision is a classifier's
        # opinion, not an extraction result, and the two must never be
        # confusable: writing "no communities" into the extraction cache from a
        # classifier would record it permanently under the current fingerprint,
        # which is the one thing CLAUDE.md forbids outright.
        #
        # `fingerprint` is half the key, so changing the model, the question or
        # the threshold releases every held page automatically — the same
        # mechanism the extraction quarantine uses. `score` is kept so a
        # decision can be re-read at a different threshold without paying for
        # the call again, and audited by eye.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gate_decisions (
                url_hash    TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                score       REAL NOT NULL,
                model       TEXT NOT NULL DEFAULT '',
                url         TEXT NOT NULL DEFAULT '',
                decided_at  TEXT NOT NULL,
                released_at TEXT,
                PRIMARY KEY (url_hash, fingerprint)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gate_fingerprint"
            " ON gate_decisions(fingerprint, score)"
        )
        # Per-day counters that belong to no run and no provider. The first is
        # enrichment: it spends the same free budget as extraction, and without
        # a number for it the report divided *every* successful call by the
        # pages extracted and called the result a per-page cost. Modified
        # records cannot stand in for it — a re-extraction modifies records too.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_counters (
                day   TEXT NOT NULL,
                name  TEXT NOT NULL,
                value INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traffic_daily (
                day       TEXT NOT NULL,
                site      TEXT NOT NULL,
                pageviews INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, site)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traffic_visitors (
                day          TEXT NOT NULL,
                site         TEXT NOT NULL,
                visitor_hash TEXT NOT NULL,
                PRIMARY KEY (day, site, visitor_hash)
            )
        """)

        # Per-provider, per-UTC-day quota ledger for the free-tier model router.
        # Persisted rather than in-memory because free daily allowances are
        # calendar-day budgets: a container restart must not hand a provider a
        # fresh 14,400 requests it does not actually have.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_usage (
                day             TEXT NOT NULL,
                provider        TEXT NOT NULL,
                calls           INTEGER NOT NULL DEFAULT 0,
                failures        INTEGER NOT NULL DEFAULT 0,
                rate_limits     INTEGER NOT NULL DEFAULT 0,
                -- Requests served on the day we first hit a 429. The published
                -- limits are stale or unpublished for most providers, so the
                -- observed ceiling is what actually governs routing.
                observed_limit  INTEGER,
                -- When `observed_limit` was last confirmed, as an epoch. The
                -- ceiling is a *hypothesis*, and a stale one is expensive: a
                -- per-minute refusal misread as a daily cap used to end a
                -- provider's day with most of its allowance unspent, with no
                -- way back until midnight. Past a TTL the router stops
                -- believing it and plans against the configured number again;
                -- if the provider really is finished it refuses once more and
                -- the pin returns, costing one call per cooldown.
                observed_at     REAL NOT NULL DEFAULT 0,
                blocked_until   REAL NOT NULL DEFAULT 0,
                last_error      TEXT,
                -- Tokens, not just calls. Groq's free tier is bounded by
                -- *tokens per day* (200,000), not by requests: on 2026-08-20 it
                -- refused with "TPD: Limit 200000, Used 199087" after ~390
                -- calls, while our catalogue planned for 14,400. A request
                -- count cannot express that ceiling at all.
                tokens          INTEGER NOT NULL DEFAULT 0,
                -- What the day's calls cost, in USD, for providers that charge.
                -- A request count cannot express a budget denominated in money:
                -- the same 10,000 calls cost $0.40 on one paid model and $20 on
                -- another, and on 2026-08-24..27 the fleet spent ~$60 through
                -- the expensive one because nothing in the ledger was counting
                -- dollars. Accumulated from the provider's own usage numbers
                -- and the catalogue's per-model price.
                cost_usd        REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (day, provider)
            )
        """)
        for _col, _type in (("tokens", "INTEGER NOT NULL DEFAULT 0"),
                            ("cost_usd", "REAL NOT NULL DEFAULT 0"),
                            ("observed_at", "REAL NOT NULL DEFAULT 0")):
            try:
                conn.execute(f"ALTER TABLE provider_usage ADD COLUMN {_col} {_type}")
            except sqlite3.OperationalError:
                pass

        # Pages whose extraction keeps failing at one fingerprint.
        #
        # A failed extraction is deliberately never cached: caching it would
        # record "0 communities" permanently and the page would never be retried.
        # The cost of that rule is that a page which fails *deterministically*
        # — the model's answer does not fit in max_output_tokens, so the JSON is
        # cut off — is re-attempted by every run, forever, against every provider
        # in the fleet. On 2026-08-26 that was ~21 pages retried by 30 runs, and
        # with paid providers on it was most of the day's bill.
        #
        # This table is the missing memory: not "the page is empty" (which would
        # be data loss) but "the page failed N times at this fingerprint, stop
        # paying to find out again". The fingerprint is part of the key, so any
        # prompt or model change clears the quarantine automatically — exactly
        # the change that could produce a different answer.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extract_failures (
                url_hash    TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                url         TEXT,
                fail_count  INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT,
                first_at    TEXT,
                last_at     TEXT,
                PRIMARY KEY (url_hash, fingerprint)
            )
        """)
        # The quarantine is read once per run as a whole-fingerprint sweep, and
        # written one row at a time.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_extract_failures_fp
            ON extract_failures(fingerprint, fail_count)
        """)

        _migrate_unicode_record_keys(conn)
        conn.commit()
    _initialised.add(key)


# ── Runs ──────────────────────────────────────────────────────────────────────

#: Reads the three-state outcome, falling back to the legacy boolean for rows
#: written before the column existed (2026-07-31). Those rows only ever
#: distinguished clean from not-clean, so a failed one maps to 'aborted'.
_OUTCOME_SQL = (
    "COALESCE(outcome, CASE WHEN success=1 THEN 'ok' ELSE 'aborted' END)"
)


def start_run(db_path: Path, started_at: datetime, run_mode: str) -> int:
    """Insert a run row immediately (finished_at=NULL). Call finish_run when done."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, run_mode, success) VALUES (?, ?, 0)",
            (started_at.isoformat(), run_mode),
        )
        conn.commit()
        return cur.lastrowid


def finish_run(
    db_path: Path,
    run_id: int,
    finished_at: datetime,
    success: bool,
    search_log: str | None = None,
    new_records: int = 0,
    error: str | None = None,
    outcome: str | None = None,
) -> None:
    """outcome: 'ok' | 'warning' | 'aborted' (see pipeline.classify_run_outcome).

    `success` stays for every existing reader and means "the run completed" —
    a warning run is successful. Callers that pass `outcome` should derive
    `success` from it the same way; callers that don't get the legacy mapping.
    """
    if outcome is None:
        outcome = "ok" if success else "aborted"
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, success=?, search_log=?, new_records=?,"
            " error=?, outcome=? WHERE id=?",
            (finished_at.isoformat(), int(success), search_log, new_records,
             error, outcome, run_id),
        )
        conn.commit()


def get_last_run_row(db_path: Path) -> dict | None:
    """Return the most recent run row regardless of success/completion."""
    if not db_path.exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                f"SELECT id, run_mode, finished_at, success, {_OUTCOME_SQL}"
                " FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                return {"id": row[0], "run_mode": row[1], "finished_at": row[2],
                        "success": bool(row[3]), "outcome": row[4]}
    except Exception:
        return None
    return None


def get_last_run(db_path: Path) -> datetime | None:
    if not db_path.exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT finished_at FROM runs WHERE success=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return datetime.fromisoformat(row[0])
    except Exception:
        return None
    return None


def get_run_history(db_path: Path, limit: int = 20) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, run_mode, success, COALESCE(new_records, 0), "
                f"{_OUTCOME_SQL} FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "started_at": r[1],
                "finished_at": r[2],
                "run_mode": r[3],
                "success": bool(r[4]),
                "new_records": r[5],
                "outcome": r[6],
            }
            for r in rows
        ]
    except Exception:
        return []


def get_run_detail(db_path: Path, run_id: int) -> dict | None:
    if not db_path.exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, started_at, finished_at, run_mode, success, search_log, "
                f"{_OUTCOME_SQL} FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "started_at": row[1],
                "finished_at": row[2],
                "run_mode": row[3],
                "success": bool(row[4]),
                "search_log": row[5],
                "outcome": row[6],
            }
    except Exception:
        return None


# ── Subscriptions ─────────────────────────────────────────────────────────────

def save_subscription(db_path: Path, email: str, city: str, topic: str) -> str:
    token = str(uuid.uuid4())
    with _connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO subscriptions (email, city, topic, token, created_at) VALUES (?,?,?,?,?)",
                (email.strip().lower(), city, topic, token, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT token FROM subscriptions WHERE email=? AND city=? AND topic=?",
                (email.strip().lower(), city, topic),
            ).fetchone()
            token = row[0] if row else token
    return token


def delete_subscription(db_path: Path, token: str) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE token=?", (token,))
        conn.commit()
        return cur.rowcount > 0


def get_subscriptions(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, email, city, topic, created_at FROM subscriptions ORDER BY id DESC"
        ).fetchall()
    return [{"id": r[0], "email": r[1], "city": r[2], "topic": r[3], "created_at": r[4]}
            for r in rows]


# ── Shared history helpers ────────────────────────────────────────────────────

def _log_changes(
    conn: sqlite3.Connection,
    table: str,
    id_col: str,
    record_id: str,
    fields: list[str],
    old_data: dict | None,
    new_data: dict,
    changed_by: str = "scraper",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if old_data is None:
        conn.execute(
            f"INSERT INTO {table} ({id_col}, changed_at, changed_by, field, old_value, new_value)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (record_id, now, changed_by, "__created__", None, new_data.get("name", "")),
        )
        return
    for field in fields:
        old_v = _val_str(old_data.get(field))
        new_v = _val_str(new_data.get(field))
        if old_v != new_v:
            conn.execute(
                f"INSERT INTO {table} ({id_col}, changed_at, changed_by, field, old_value, new_value)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (record_id, now, changed_by, field, old_v, new_v),
            )


def _get_history(db_path: Path, table: str, id_col: str, record_id: str, limit: int = 100) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT changed_at, changed_by, field, old_value, new_value"
            f" FROM {table} WHERE {id_col}=?"
            f" ORDER BY changed_at DESC, id DESC LIMIT ?",
            (record_id, limit),
        ).fetchall()
    return [{"changed_at": r[0], "changed_by": r[1], "field": r[2],
             "old_value": r[3], "new_value": r[4]} for r in rows]


# ── Communities ───────────────────────────────────────────────────────────────

_HISTORY_FIELDS = [
    "name", "description", "short_description", "long_description", "history",
    "website", "tags", "social_links",
    "meeting_schedule", "location", "contact", "fee", "age_range", "skill_level",
    "join_process", "leader", "language", "frequency", "founding_year", "member_count",
    "email", "phone", "confidence", "joinable",
]


def _val_str(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
        return None if s in ("[]", "{}") else s
    return str(v)


def _log_community_changes(
    conn: sqlite3.Connection,
    community_id: str,
    old_data: dict | None,
    new_data: dict,
    changed_by: str = "scraper",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if old_data is None:
        conn.execute(
            "INSERT INTO community_history"
            " (community_id, changed_at, changed_by, field, old_value, new_value)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (community_id, now, changed_by, "__created__", None, new_data.get("name", "")),
        )
        return
    for field in _HISTORY_FIELDS:
        old_v = _val_str(old_data.get(field))
        new_v = _val_str(new_data.get(field))
        if old_v != new_v:
            conn.execute(
                "INSERT INTO community_history"
                " (community_id, changed_at, changed_by, field, old_value, new_value)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (community_id, now, changed_by, field, old_v, new_v),
            )


# SEO-enrichment fields written by scraper/enrich.py, never by extraction. They
# must survive a re-extraction that rebuilds the record from cache, so they are
# carried forward from the existing row whenever the incoming record lacks them.
_PRESERVED_ENRICHMENT_FIELDS = ("short_description", "long_description")


def _merge_source_urls(old_data: dict | None, record: dict) -> dict:
    if not old_data:
        return record
    prev_urls: list[str] = old_data.get("source_urls") or []
    if old_data.get("source_url") and old_data["source_url"] not in prev_urls:
        prev_urls = [old_data["source_url"]] + prev_urls
    new_urls: list[str] = record.get("source_urls") or []
    if record.get("source_url") and record["source_url"] not in new_urls:
        new_urls = [record["source_url"]] + new_urls
    merged = {**record, "source_urls": list(dict.fromkeys(new_urls + prev_urls))}
    for field in _PRESERVED_ENRICHMENT_FIELDS:
        if not merged.get(field) and old_data.get(field):
            merged[field] = old_data[field]
    return merged


# Fields that change on every extraction without any user-visible content change;
# excluded from the change-detection fingerprint so `updated_at`/`<lastmod>` stay stable.
# `enrich_attempted_at` is a retry marker (dropped on re-extraction) — must be volatile
# or its removal reads as a change and falsely bumps <lastmod>.
_VOLATILE_COMMUNITY_FIELDS = {"extracted_at", "enrich_attempted_at"}


def _community_content_fingerprint(data: dict) -> str:
    # Drop volatile fields AND null/empty values, so newly-added optional fields
    # (e.g. short_description/long_description serialized as None on the first save
    # of a pre-existing record) don't register as a content change and churn every
    # page's <lastmod>.
    return json.dumps(
        {k: v for k, v in data.items()
         if k not in _VOLATILE_COMMUNITY_FIELDS and v not in (None, "", [], {})},
        ensure_ascii=False, sort_keys=True,
    )


def _bulk_upsert_communities(
    conn: sqlite3.Connection,
    records: list[dict],
    previous: dict[str, dict] | None = None,
    prev_updated: dict[str, str] | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for record in records:
        key = _community_record_key(record["name"], record["city"], record["topic"])

        # Prefer pre-delete snapshot; fall back to live row (used by bulk_upsert_communities)
        old_data = (previous or {}).get(key)
        old_updated = (prev_updated or {}).get(key)
        if old_data is None:
            existing_row = conn.execute(
                "SELECT data, updated_at FROM communities WHERE record_key=?", (key,)
            ).fetchone()
            if existing_row:
                old_data = json.loads(existing_row[0])
                old_updated = existing_row[1]

        record = _merge_source_urls(old_data, record)
        data_str = json.dumps(record, ensure_ascii=False)
        # Only advance updated_at on a real content change, so it is a trustworthy
        # sitemap <lastmod> and a re-extraction that reproduces identical data does
        # not churn the whole corpus's freshness dates. (replace_communities_for_topic
        # DELETEs first, so the ON CONFLICT branch never fires there — the preserved
        # timestamp must be computed here and passed as the inserted value.)
        row_updated = now
        if old_data is not None and old_updated and \
                _community_content_fingerprint(record) == _community_content_fingerprint(old_data):
            row_updated = old_updated

        conn.execute("""
            INSERT INTO communities (record_key, community_id, city, topic, data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_key) DO UPDATE SET
                data=excluded.data,
                community_id=excluded.community_id,
                updated_at=excluded.updated_at
        """, (key, record.get("community_id", ""), record["city"], record["topic"],
              data_str, row_updated))

        _log_community_changes(conn, record.get("community_id", ""), old_data, record)


def bulk_upsert_communities(db_path: Path, records: list[dict]) -> None:
    with _connect(db_path) as conn:
        _bulk_upsert_communities(conn, records)
        conn.commit()


def replace_communities_for_topic(
    db_path: Path,
    city: str,
    topic: str,
    records: list[dict],
) -> None:
    with _connect(db_path) as conn:
        # One transaction from the snapshot to the write: read outside it, a
        # concurrent enrichment write between the two was silently erased.
        conn.execute("BEGIN IMMEDIATE")
        # Snapshot existing records before delete so history can diff against them.
        rows = conn.execute(
            "SELECT data, updated_at FROM communities WHERE city=? AND topic=?",
            (city, topic)
        ).fetchall()
        previous: dict[str, dict] = {}
        prev_updated: dict[str, str] = {}
        for data_str, updated_at in rows:
            d = json.loads(data_str)
            key = _community_record_key(d["name"], d["city"], d["topic"])
            previous[key] = d
            prev_updated[key] = updated_at

        # Visible rows only. `records` is built from the *visible* set plus the
        # new extraction, so deleting hidden rows too erased every moderated
        # (merged / reported) community not in this batch — and one that came
        # back later came back visible. Kept hidden rows are updated in place by
        # the upsert's ON CONFLICT branch, which never touches `hidden`.
        conn.execute("DELETE FROM communities WHERE city=? AND topic=? AND hidden=0",
                     (city, topic))
        _bulk_upsert_communities(conn, records, previous, prev_updated)
        conn.commit()


def get_enrichment_candidates(
    db_path: Path, city_names: set[str], limit: int, retry_after_days: int = 7,
) -> list[dict]:
    """Communities in `city_names` still missing a `long_description` (the durable
    enrichment marker — set fields survive re-extraction via `_merge_source_urls`).
    Candidates attempted-but-not-enriched within the last `retry_after_days` are
    skipped (see `mark_enrichment_attempted`) so blocked/dead sources or repeated
    invalid output don't starve later communities while still allowing eventual
    retry. Returns dicts with record_key/name/city/topic/locale/description + all
    source_urls and any cached raw_text. raw_text is looked up per candidate
    (bounded to ~limit lookups), never the whole cache at once (OOM)."""
    import hashlib
    from datetime import timedelta

    def _uh(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    if not db_path.exists() or not city_names or limit <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retry_after_days)).isoformat()
    out: list[dict] = []
    with _connect(db_path) as conn:
        # Filtered in SQL and read lazily: this used to fetchall() every visible
        # community (~110 MB of JSON) and filter in Python, every round.
        rows = conn.execute(
            "SELECT record_key, data FROM communities WHERE hidden=0"
            " AND city IN (SELECT value FROM json_each(?))"
            # already enriched (durable marker)
            " AND TRIM(COALESCE(json_extract(data, '$.long_description'), '')) = ''"
            # attempted recently and failed — retry later, not now
            " AND COALESCE(json_extract(data, '$.enrich_attempted_at'), '') <= ?"
            " ORDER BY id",
            (json.dumps(sorted(city_names)), cutoff))
        for record_key, data_str in rows:
            if len(out) >= limit:
                break
            try:
                d = json.loads(data_str)
            except (TypeError, json.JSONDecodeError):
                continue
            urls = d.get("source_urls") or ([d["source_url"]] if d.get("source_url") else [])
            if not urls:
                continue
            raw_text = None
            for url in urls:
                row = conn.execute(
                    "SELECT json_extract(data, '$.raw_text') FROM cache_pages WHERE url_hash=?",
                    (_uh(url),),
                ).fetchone()
                if row and row[0] and len(row[0]) >= 300:
                    raw_text = row[0]
                    break
            out.append({
                "record_key": record_key, "name": d.get("name", ""),
                "city": d.get("city", ""), "topic": d.get("topic", ""),
                "locale": d.get("locale", "hu"),
                "description": d.get("description") or "",
                "source_urls": urls, "raw_text": raw_text,
            })
    return out


def update_community_enrichment(
    db_path: Path, record_key: str, short_description: str, long_description: str,
) -> bool:
    """Set a community's enrichment fields (`short_description` + `long_description`)
    and bump `updated_at` (genuine content change → correct <lastmod>). Returns
    False if the row is gone. No cache write needed: `_merge_source_urls` carries
    these fields forward across every re-extraction, so they are durable."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM communities WHERE record_key=?", (record_key,)
        ).fetchone()
        if not row:
            return False
        old = json.loads(row[0])
        now = datetime.now(timezone.utc).isoformat()
        new = {**old, "short_description": short_description,
               "long_description": long_description}
        conn.execute(
            "UPDATE communities SET data=?, updated_at=? WHERE record_key=?",
            (json.dumps(new, ensure_ascii=False), now, record_key),
        )
        _log_community_changes(conn, new.get("community_id", ""), old, new)
        conn.commit()
    return True


def mark_enrichment_attempted(db_path: Path, record_key: str) -> None:
    """Stamp `enrich_attempted_at` so a candidate that failed/produced junk is not
    re-selected every batch (see `get_enrichment_candidates`). Does NOT bump
    `updated_at` (no user-visible change) or log history. Not a CommunityRecord
    field, so a genuine re-extraction clears it and allows a fresh retry."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM communities WHERE record_key=?", (record_key,)
        ).fetchone()
        if not row:
            return
        d = json.loads(row[0])
        d["enrich_attempted_at"] = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE communities SET data=? WHERE record_key=?",
                     (json.dumps(d, ensure_ascii=False), record_key))
        conn.commit()


def get_communities(db_path: Path, city: str, topic: str) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data FROM communities WHERE city=? AND topic=? AND hidden=0 ORDER BY id",
            (city, topic)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_community_lastmods(db_path: Path) -> dict[tuple[str, str], str]:
    """(city, name_slug) → updated_at date (YYYY-MM-DD) for visible communities.

    Keyed by public slug and ordered by (topic, id) with first-wins, so the date
    is the one for the exact record the public URL resolves to (see
    the community page, first match in `get_communities_for_city`) — even when a name
    exists under multiple topics or two names share a slug. `updated_at` only
    advances on real content changes (see `_bulk_upsert_communities`), so it is a
    stable <lastmod>. One query for the whole sitemap.
    """
    if not db_path.exists():
        return {}
    from .identity import public_slug
    out: dict[tuple[str, str], str] = {}
    with _connect(db_path) as conn:
        for city, name, updated_at in conn.execute(
            "SELECT city, json_extract(data,'$.name'), updated_at "
            "FROM communities WHERE hidden=0 ORDER BY topic, id"
        ):
            if name and updated_at:
                out.setdefault((city, public_slug(name)), updated_at[:10])
    return out


def search_communities_by_tag(db_path: Path, tag: str, city: str = "") -> list[dict]:
    """Return communities whose tags array contains `tag` (exact match, case-sensitive)."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        if city:
            rows = conn.execute(
                "SELECT data FROM communities WHERE city=? AND hidden=0 AND EXISTS ("
                "  SELECT 1 FROM json_each(json_extract(data,'$.tags')) WHERE value=?"
                ") ORDER BY id",
                (city, tag)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT data FROM communities WHERE hidden=0 AND EXISTS ("
                "  SELECT 1 FROM json_each(json_extract(data,'$.tags')) WHERE value=?"
                ") ORDER BY city, id",
                (tag,)
            ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_all_communities(db_path: Path, city: str | None = None) -> list[dict]:
    """Every visible community, or only one city's when `city` is given.

    The filter is in SQL rather than in the caller for a reason that only shows
    at production size: every row carries the record's whole JSON blob, so
    reading them all to keep one city's costs the full table — 45,785 blobs
    parsed to answer a question about a few dozen. `detect_community_candidates`
    runs on every `save_results`, i.e. once per processed pair, which is where
    that bill was being paid. `idx_comm_city_topic` makes the filtered form a
    SEARCH instead of a SCAN.
    """
    if not db_path.exists():
        return []
    sql = "SELECT data FROM communities WHERE hidden=0"
    params: tuple = ()
    if city is not None:
        sql += " AND city=?"
        params = (city,)
    sql += " ORDER BY city, topic, id"
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_community_by_record_key(db_path: Path, record_key: str) -> dict | None:
    """Get a single community by record_key, including hidden records."""
    if not db_path or not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM communities WHERE record_key=?", (record_key,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def get_community_history(db_path: Path, community_id: str, limit: int = 100) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT changed_at, changed_by, field, old_value, new_value"
            " FROM community_history WHERE community_id=?"
            " ORDER BY changed_at DESC, id DESC LIMIT ?",
            (community_id, limit),
        ).fetchall()
    return [
        {"changed_at": r[0], "changed_by": r[1], "field": r[2],
         "old_value": r[3], "new_value": r[4]}
        for r in rows
    ]


_VENUE_HISTORY_FIELDS = [
    "name", "description", "venue_type", "address", "website",
    "social_links", "email", "phone", "contact", "welcomed_topics",
]

_PERSON_HISTORY_FIELDS = [
    "name", "role", "bio", "email", "website", "social_links", "community_name",
]


def get_venue_history(db_path: Path, venue_id: str, limit: int = 100) -> list[dict]:
    return _get_history(db_path, "venue_history", "venue_id", venue_id, limit)


def get_person_history(db_path: Path, person_id: str, limit: int = 100) -> list[dict]:
    return _get_history(db_path, "person_history", "person_id", person_id, limit)


def set_community_hidden(db_path: Path, record_key: str, hidden: bool) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE communities SET hidden=? WHERE record_key=?",
            (1 if hidden else 0, record_key),
        )
        conn.commit()


def get_communities_for_city(db_path: Path, city: str) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data FROM communities WHERE city=? AND hidden=0 ORDER BY topic, id",
            (city,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def find_community_by_id(db_path: Path, community_id: str) -> dict | None:
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM communities WHERE community_id=? AND hidden=0 LIMIT 1",
            (community_id,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def get_communities_by_ids(db_path: Path, community_ids: list[str]) -> list[dict]:
    """Bulk fetch communities by a list of community_id values."""
    if not db_path.exists() or not community_ids:
        return []
    placeholders = ",".join("?" * len(community_ids))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT data FROM communities WHERE community_id IN ({placeholders}) AND hidden=0",
            community_ids,
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_guide_candidate_groups(db_path: Path) -> list[dict]:
    """Return unpublished city/topic groups, richest first.

    SQL only selects the bounded candidate inventory; editorial quality gates
    stay in guides.py where they can be tested without knowing SQLite details.
    """
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT c.city, c.topic, COUNT(*) AS community_count,
                   SUM(CASE WHEN LENGTH(TRIM(COALESCE(
                       json_extract(c.data, '$.long_description'),
                       json_extract(c.data, '$.description'),
                       json_extract(c.data, '$.short_description'), ''))) >= 50
                       THEN 1 ELSE 0 END) AS described_count
            FROM communities c
            WHERE c.hidden=0
              AND NOT EXISTS (
                  SELECT 1 FROM data_guides g
                  WHERE g.city=c.city AND g.topic=c.topic
              )
            GROUP BY c.city, c.topic
            ORDER BY community_count DESC, described_count DESC, c.city, c.topic
            """,
        ).fetchall()
    return [
        {"city": r[0], "topic": r[1], "community_count": r[2],
         "described_count": r[3]}
        for r in rows
    ]


def create_data_guide(db_path: Path, guide: dict) -> bool:
    """Insert one publication idempotently; return whether a row was added."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO data_guides
                (slug, site, city, topic, locale, title, summary, data,
                 published_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guide["slug"], guide["site"], guide["city"], guide["topic"],
             guide["locale"], guide["title"], guide["summary"],
             json.dumps(guide["data"], ensure_ascii=False),
             guide["published_at"], guide["updated_at"]),
        )
        conn.commit()
        return cur.rowcount == 1


def count_data_guides_published_on(db_path: Path, day: str,
                                   site: str | None = None) -> int:
    with _connect(db_path) as conn:
        if site:
            row = conn.execute(
                "SELECT COUNT(*) FROM data_guides WHERE site=? AND substr(published_at,1,10)=?",
                (site, day),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM data_guides WHERE substr(published_at,1,10)=?", (day,)
            ).fetchone()
    return int(row[0])


_GUIDE_COLUMNS = ("id", "slug", "site", "city", "topic", "locale", "title",
                  "summary", "data", "published_at", "updated_at")


def _decode_guide_row(row) -> dict:
    result = dict(zip(_GUIDE_COLUMNS, row))
    result["data"] = json.loads(result["data"])
    return result


def get_data_guide(db_path: Path, slug: str,
                   site: str = "kozossegek") -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM data_guides WHERE site=? AND slug=?", (site, slug)
        ).fetchone()
    if not row:
        return None
    return _decode_guide_row(row)


def get_data_guides(db_path: Path, site: str = "kozossegek", *,
                    limit: int = 24, offset: int = 0) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM data_guides WHERE site=? ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
            (site, limit, offset),
        ).fetchall()
    return [_decode_guide_row(r) for r in rows]


def get_stale_data_guides(db_path: Path, prompt_version: str,
                          limit: int = 10) -> list[dict]:
    """Published guides whose prose predates the current writer.

    The prompt version is the whole staleness rule. An article is the output of
    one prompt against one fact packet, so improving the prompt is exactly what
    makes an already-published article out of date — and leaving it up means the
    site keeps serving prose the current gate would refuse. Oldest first: those
    have had the longest to be indexed and read.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM data_guides
            WHERE COALESCE(json_extract(data, '$.prompt_version'), '') != ?
            ORDER BY published_at ASC, id ASC LIMIT ?
            """,
            (prompt_version, limit),
        ).fetchall()
    return [_decode_guide_row(r) for r in rows]


def replace_data_guide(db_path: Path, slug: str, guide: dict) -> bool:
    """Rewrite one guide in place, keeping its slug, URL and publication date.

    `published_at` deliberately survives: the page is the same page about the
    same groups, and back-dating or advancing it would misreport when this
    directory first covered them. `updated_at` is what moves.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE data_guides SET title=?, summary=?, data=?, updated_at=?
            WHERE slug=? AND site=?
            """,
            (guide["title"], guide["summary"],
             json.dumps(guide["data"], ensure_ascii=False),
             guide["updated_at"], slug, guide["site"]),
        )
        conn.commit()
        return cur.rowcount == 1


def get_data_guides_for_day(db_path: Path, day: str) -> list[dict]:
    """All guides published on one UTC date, in publication order."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM data_guides WHERE substr(published_at,1,10)=? ORDER BY id",
            (day,),
        ).fetchall()
    return [_decode_guide_row(r) for r in rows]


def count_data_guides(db_path: Path, site: str = "kozossegek") -> int:
    with _connect(db_path) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM data_guides WHERE site=?", (site,)
        ).fetchone()[0])


def get_data_guide_sitemap_rows(db_path: Path,
                                site: str = "kozossegek") -> list[tuple[str, str]]:
    with _connect(db_path) as conn:
        return [(r[0], r[1][:10]) for r in conn.execute(
            "SELECT slug, updated_at FROM data_guides WHERE site=? ORDER BY id",
            (site,),
        )]


def get_communities_for_venue(
    db_path: Path,
    community_ids: list[str],
    venue_name: str,
    city: str,
) -> list[dict]:
    """Return communities associated with a venue.
    Tries community_ids first; falls back to location-text match."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        if community_ids:
            placeholders = ",".join("?" * len(community_ids))
            rows = conn.execute(
                f"SELECT data FROM communities WHERE community_id IN ({placeholders}) AND hidden=0",
                community_ids,
            ).fetchall()
            if rows:
                return [json.loads(r[0]) for r in rows]
        safe_name = venue_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT data FROM communities WHERE city=? AND hidden=0"
            " AND json_extract(data,'$.location') LIKE ? ESCAPE '\\'",
            (city, f"%{safe_name}%"),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_venue_for_community(db_path: Path, community_id: str, city: str) -> dict | None:
    """Return the first venue in city whose community_ids list contains community_id."""
    if not db_path.exists() or not community_id:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM venues WHERE city=? AND EXISTS ("
            "  SELECT 1 FROM json_each(json_extract(data,'$.community_ids')) WHERE value=?"
            ") LIMIT 1",
            (city, community_id),
        ).fetchone()
    return json.loads(row[0]) if row else None


def get_topic_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT topic, COUNT(*) FROM communities WHERE hidden=0 GROUP BY topic"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_topic_counts_for_cities(db_path: Path, city_names: set[str]) -> dict[str, int]:
    """Topic counts restricted to a specific set of city names (single query)."""
    if not db_path.exists() or not city_names:
        return {}
    placeholders = ",".join("?" * len(city_names))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT topic, COUNT(*) FROM communities WHERE city IN ({placeholders}) AND hidden=0 GROUP BY topic",
            tuple(city_names),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_city_topic_counts(db_path: Path) -> dict[str, dict[str, int]]:
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT city, topic, COUNT(*) FROM communities WHERE hidden=0 GROUP BY city, topic"
        ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for city, topic, count in rows:
        result.setdefault(city, {})[topic] = count
    return result


def get_topic_counts_for_city(db_path: Path, city: str) -> dict[str, int]:
    """{topic: visible community count} for one city — an index lookup, where
    `get_city_topic_counts(...)[city]` aggregates the whole table to read one row.
    """
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT topic, COUNT(*) FROM communities WHERE city=? AND hidden=0 GROUP BY topic",
            (city,)).fetchall()
    return {topic: count for topic, count in rows}


def get_city_topic_states(db_path: Path, current_fp: str) -> dict[str, dict[str, dict]]:
    """Return per-(city, topic) state dict for the coverage page.

    State keys per cell:
      community_count: int
      page_count: int   (search_cache URLs that have been scraped)
      current_fp_count: int  (scraped URLs extracted with current_fp)

    Uses url_hash lookup (same as get_fully_processed_pairs) so that
    page_count/current_fp_count are consistent with the done-pairs check.
    The old city/topic JOIN was unreliable because cache_pages.city/topic
    is last-write-wins and gets overwritten when a URL appears in multiple
    search results.
    """
    import hashlib as _hashlib

    def _url_hash(url: str) -> str:
        return _hashlib.sha256(url.encode()).hexdigest()[:16]

    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        # url_hash → extract_fingerprint for all successfully scraped pages
        fp_by_hash: dict[str, str | None] = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT url_hash, extract_fingerprint FROM cache_pages WHERE scraped_at IS NOT NULL"
            )
        }
        comm_counts: dict[tuple[str, str], int] = {
            (r[0], r[1]): r[2]
            for r in conn.execute(
                "SELECT city, topic, COUNT(*) FROM communities WHERE hidden=0 GROUP BY city, topic"
            )
        }
        search_rows = conn.execute("SELECT city, topic, urls FROM search_cache").fetchall()

    result: dict[str, dict[str, dict]] = {}
    for city, topic, urls_json in search_rows:
        urls: list[str] = json.loads(urls_json) if urls_json else []
        hashes = [_url_hash(u) for u in urls]
        page_count = sum(1 for h in hashes if h in fp_by_hash)
        current_fp_count = sum(1 for h in hashes if fp_by_hash.get(h) == current_fp)
        result.setdefault(city, {})[topic] = {
            "page_count": page_count,
            "current_fp_count": current_fp_count,
            "community_count": comm_counts.get((city, topic), 0),
        }
    return result


def get_searched_pairs(db_path: Path) -> set[tuple[str, str]]:
    """Pairs with a saved search — the only ones that can have cached pages."""
    if not db_path.exists():
        return set()
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT city, topic FROM search_cache").fetchall()
    return {(row[0], row[1]) for row in rows}


def get_collected_pairs(db_path: Path, max_pages: int) -> set[tuple[str, str]]:
    """Pairs whose selected search results have all had a fetch attempt."""
    del max_pages  # retained in the public signature for compatibility
    if not db_path.exists():
        return set()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT city, topic FROM search_cache WHERE collected_at IS NOT NULL"
        ).fetchall()
    return {(row[0], row[1]) for row in rows}


def get_fully_processed_pairs(
    db_path: Path,
    current_fp: str,
    venue_fp: str | None = None,
    person_fp: str | None = None,
    *,
    run_communities: bool = True,
    run_venues: bool = False,
    run_persons: bool = False,
    max_pages: int | None = None,
    quarantine_threshold: int = 0,
) -> set[tuple[str, str]]:
    """Return (city, topic) pairs that need no pipeline work this run.

    cache_pages is keyed by url_hash and city/topic columns are overwritten on
    every save (last-write-wins), so city/topic-based joins are unreliable.
    Instead we check by url_hash, exactly as cache.py does at fetch time.

    A pair is done if it has a search_cache entry and every successfully
    scraped page is current for every extraction family enabled for this run.
    Venue/person extraction is only expected when the cached community result
    is non-empty, matching the pipeline's cost gates.

    A page in extraction quarantine (`quarantine_threshold` content failures at
    `current_fp`) counts as done. It is not extracted and never will be at this
    fingerprint, so leaving its pair outstanding would put the pair back in the
    loop on every run of the day to skip the same page again — which is the
    noise the done-pair filter exists to remove.
    """
    import hashlib

    # The same URL ranks for several topics in one city, so this is called more
    # than once per page across ~54K search-cache rows.
    _hash_memo: dict[str, str] = {}

    def _url_hash(url: str) -> str:
        h = _hash_memo.get(url)
        if h is None:
            h = _hash_memo[url] = hashlib.sha256(url.encode()).hexdigest()[:16]
        return h

    def _json_object_keys(raw: str | None) -> set[str]:
        if not raw:
            return set()
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return set()
        return set(value) if isinstance(value, dict) else set()

    if not db_path.exists():
        return set()

    # Ask only for what this run reads. The venue and person columns cost three
    # more JSON traversals of every `data` blob, and `persons_data` is a whole
    # sub-object pulled out as text and re-parsed in Python — over ~207K cached
    # pages, for a caller that never looks at it. /v1/backlog and every
    # `search_only` run take the defaults, where run_venues and run_persons are
    # both false, and the endpoint was timing out past 200 seconds.
    want_extras = run_venues or run_persons
    # `records_count` replaces two JSON traversals of `data`. That blob is
    # ~30 KB per row in production and `json_type(data,'$.records')` makes
    # SQLite read every one of ~207K of them: measured on a 6.15 GB copy the
    # filter took 13.2 s against 0.7 s with small blobs, which is why
    # /v1/backlog answered 524 after 125 seconds. Only the person path still
    # opens the blob, and only when persons are actually being extracted.
    cols = ["url_hash", "extract_fingerprint", "records_count"]
    if want_extras:
        cols += ["venue_fingerprint", "person_fingerprint",
                 "json_extract(data, '$.persons_data')"]

    with _connect(db_path) as conn:
        # One snapshot for all three reads. The bulk scan and the NULL fallback
        # are separate statements and the backfill runs concurrently in the
        # background: without a transaction a row can flip NULL -> count between
        # them, so the first read calls it unextracted and the second no longer
        # matches `records_count IS NULL` to correct it. The page then reads as
        # outstanding and its pair is re-extracted — the outcome the fallback
        # exists to prevent, during exactly the window it exists for. WAL gives
        # a reader a consistent snapshot for the life of its transaction.
        conn.execute("BEGIN")
        pages_by_hash: dict[str, dict] = {}
        for row in conn.execute(
                f"SELECT {', '.join(cols)} FROM cache_pages"
                " WHERE scraped_at IS NOT NULL"):
            # NULL is either "never extracted" or "the backfill has not
            # reached this row yet". The loop below tells those apart from the
            # blob, for the shrinking set of rows where it still matters.
            count = row[2]
            entry = {
                "extract_fingerprint": row[1],
                "records_present": count is not None and count >= 0,
                "has_communities": count is not None and count > 0,
                "venue_fingerprint": None,
                "person_fingerprint": None,
                "person_keys": frozenset(),
                "quarantined": False,
            }
            if want_extras:
                entry["venue_fingerprint"] = row[3]
                entry["person_fingerprint"] = row[4]
                entry["person_keys"] = _json_object_keys(row[5])
            pages_by_hash[row[0]] = entry
        # Whatever the backfill has not reached yet, read from the blob. The
        # alternative — treating a NULL as "never extracted" — would send the
        # whole corpus back for re-extraction the moment this shipped, and at
        # the free fleet's ~650 pages a day that is a year of work. The index
        # answers this in one probe once the backfill is done, and the rows it
        # returns shrink to none.
        for uh, is_array, length in conn.execute(
                "SELECT url_hash,"
                " json_type(data, '$.records') = 'array',"
                " json_array_length(json_extract(data, '$.records'))"
                " FROM cache_pages"
                " WHERE scraped_at IS NOT NULL AND records_count IS NULL"):
            entry = pages_by_hash.get(uh)
            if entry is None:
                continue
            entry["records_present"] = bool(is_array)
            entry["has_communities"] = bool(is_array) and bool(length)
        quarantined: set[str] = set()
        if quarantine_threshold > 0:
            try:
                quarantined = {
                    r[0] for r in conn.execute(
                        "SELECT url_hash FROM extract_failures"
                        " WHERE fingerprint=? AND fail_count>=?",
                        (current_fp, int(quarantine_threshold)))
                }
            except sqlite3.OperationalError:
                quarantined = set()  # older database: nothing is quarantined
        for _uh in quarantined:
            _entry = pages_by_hash.get(_uh)
            if _entry is not None:
                _entry["quarantined"] = True
        search_rows = conn.execute("SELECT city, topic, urls FROM search_cache").fetchall()
        conn.rollback()  # read-only: end the snapshot without pretending to write

    result: set[tuple[str, str]] = set()
    for city, topic, urls_json in search_rows:
        try:
            urls: list[str] = json.loads(urls_json) if urls_json else []
        except (TypeError, json.JSONDecodeError):
            log.warning("invalid_search_cache_urls", city=city, topic=topic)
            continue
        if max_pages is not None:
            urls = urls[:max_pages]
        if not urls:
            result.add((city, topic))
            continue

        processable = [
            pages_by_hash[_url_hash(url)]
            for url in urls
            if _url_hash(url) in pages_by_hash
        ]
        if not processable:
            continue

        pair_person_key = f"{city}/{topic}"
        all_current = True
        for page in processable:
            community_current = (
                page["extract_fingerprint"] == current_fp
                and page["records_present"]
            ) or page["quarantined"]
            if (run_communities or run_persons) and not community_current:
                all_current = False
                break

            has_communities = community_current and page["has_communities"]
            venue_expected = run_venues and (has_communities or not run_communities)
            if venue_expected and (
                not venue_fp or page["venue_fingerprint"] != venue_fp
            ):
                all_current = False
                break

            person_expected = run_persons and has_communities
            if person_expected and (
                not person_fp
                or page["person_fingerprint"] != person_fp
                or pair_person_key not in page["person_keys"]
            ):
                all_current = False
                break

        if all_current:
            result.add((city, topic))
    return result


def get_recently_added_communities(db_path: Path, limit: int = 30) -> list[dict]:
    """Latest first-seen visible communities (first __created__ history row per id)."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT c.data, h.first_seen FROM communities c
            JOIN (SELECT community_id, MIN(changed_at) AS first_seen
                  FROM community_history WHERE field='__created__'
                  GROUP BY community_id) h ON h.community_id = c.community_id
            WHERE c.hidden = 0
              AND c.id = (SELECT MIN(c2.id) FROM communities c2
                          WHERE c2.community_id = c.community_id AND c2.hidden = 0)
            ORDER BY h.first_seen DESC LIMIT ?
        """, (limit,)).fetchall()
    out = []
    for data_str, first_seen in rows:
        try:
            d = json.loads(data_str)
        except Exception:
            continue
        d["first_seen"] = first_seen
        out.append(d)
    return out


def get_city_totals(db_path: Path) -> list[tuple[str, int]]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT city, COUNT(*) as cnt FROM communities WHERE hidden=0 GROUP BY city ORDER BY cnt DESC"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_total_community_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM communities WHERE hidden=0").fetchone()
    return row[0] if row else 0


# ── Cache pages ───────────────────────────────────────────────────────────────

#: `records_count` for a page that has been scraped but never extracted.
#: A sentinel rather than NULL, because NULL has to mean exactly one thing —
#: "the backfill has not reached this row" — for the blob fallback to empty out.
#: Using NULL for both left every un-extracted page NULL forever, so the
#: fallback opened its ~30 KB blob on every scan: the optimisation defeated
#: precisely where the backlog is largest.
_NOT_EXTRACTED = -1


def _records_count(entry: dict) -> int:
    """How many communities this page's extraction produced.

    -1 is "scraped, never extracted"; 0 is "extracted, found nothing" — a
    finished page. The done-pair filter needs to tell those apart, and reads
    this column instead of `json_type(data,'$.records')` so a covering index
    can answer it without touching the blob. Every writer must set it.
    """
    records = entry.get("records")
    return len(records) if isinstance(records, list) else _NOT_EXTRACTED


def _write_cache_page(conn: sqlite3.Connection, entry: dict) -> None:
    # extract_quality/extract_model mirror the blob into real columns so the
    # router's upgrade sweep can filter and order in SQL rather than by decoding
    # every cached page. Neither is part of any cache key.
    quality = entry.get("extract_quality")
    conn.execute("""
        INSERT INTO cache_pages
            (url_hash, url, city, topic, domain, scraped_at, extracted_at,
             extract_fingerprint, venue_fingerprint, person_fingerprint,
             extract_quality, extract_model, records_count, data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url_hash) DO UPDATE SET
            city=excluded.city,
            topic=excluded.topic,
            domain=excluded.domain,
            scraped_at=excluded.scraped_at,
            extracted_at=excluded.extracted_at,
            extract_fingerprint=excluded.extract_fingerprint,
            venue_fingerprint=excluded.venue_fingerprint,
            person_fingerprint=excluded.person_fingerprint,
            extract_quality=excluded.extract_quality,
            extract_model=excluded.extract_model,
            records_count=excluded.records_count,
            data=excluded.data
    """, (
        entry["url_hash"],
        entry.get("url", ""),
        entry.get("city", ""),
        entry.get("topic", ""),
        entry.get("domain", ""),
        entry.get("scraped_at"),
        entry.get("extracted_at"),
        entry.get("extract_fingerprint"),
        entry.get("venue_fingerprint"),
        entry.get("person_fingerprint"),
        int(quality) if isinstance(quality, (int, float)) else None,
        entry.get("extract_model"),
        _records_count(entry),
        json.dumps(entry, ensure_ascii=False),
    ))


def update_cache_page(db_path: Path, url_hash: str, updates: dict | None = None,
                      *, create: dict | None = None, drop: list[str] | None = None,
                      mutate=None) -> dict | None:
    """Atomic read-merge-write of ONE cache page inside a single transaction.

    The old load→mutate→save pattern used two separate connections, so two
    concurrent writers to the same URL (pipeline vs. admin queue ops) could
    have the later full-blob write erase the other's fields. BEGIN IMMEDIATE
    holds the write lock across the read.

    create: entry skeleton used when the row does not exist (None = no-op on
    missing rows). drop: keys removed from the entry. mutate: callable applied
    to the entry inside the transaction for nested structures."""
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT data FROM cache_pages WHERE url_hash=?", (url_hash,)
        ).fetchone()
        if row:
            entry = json.loads(row[0])
        elif create is not None:
            entry = {**create, "url_hash": url_hash}
        else:
            conn.commit()
            return None
        if updates:
            entry.update(updates)
        for key in drop or ():
            entry.pop(key, None)
        if mutate is not None:
            entry = mutate(entry) or entry
        _write_cache_page(conn, entry)
        conn.commit()
    return entry


def save_cache_page(db_path: Path, entry: dict) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO cache_pages
                (url_hash, url, city, topic, domain, scraped_at, extracted_at,
                 extract_fingerprint, venue_fingerprint, person_fingerprint,
                 records_count, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                city=excluded.city,
                topic=excluded.topic,
                domain=excluded.domain,
                scraped_at=excluded.scraped_at,
                extracted_at=excluded.extracted_at,
                extract_fingerprint=excluded.extract_fingerprint,
                venue_fingerprint=excluded.venue_fingerprint,
                person_fingerprint=excluded.person_fingerprint,
                records_count=excluded.records_count,
                data=excluded.data
        """, (
            entry["url_hash"],
            entry.get("url", ""),
            entry.get("city", ""),
            entry.get("topic", ""),
            entry.get("domain", ""),
            entry.get("scraped_at"),
            entry.get("extracted_at"),
            entry.get("extract_fingerprint"),
            entry.get("venue_fingerprint"),
            entry.get("person_fingerprint"),
            _records_count(entry),
            json.dumps(entry, ensure_ascii=False),
        ))
        conn.commit()


def load_cache_page(db_path: Path, url_hash: str) -> dict | None:
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM cache_pages WHERE url_hash=?", (url_hash,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def delete_cache_page(db_path: Path, url_hash: str) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM cache_pages WHERE url_hash=?", (url_hash,))
        conn.commit()
        return cur.rowcount > 0


def _update_cache_pages_in_chunks(conn: sqlite3.Connection, set_sql: str,
                                  where_sql: str, chunk: int = 2000) -> int:
    """Apply one UPDATE to `cache_pages` a rowid range at a time, committing
    between ranges.

    As one statement a corpus-wide `json_remove` rewrote ~6 GB of blobs in a
    single transaction: the write lock was held for minutes, every other writer
    (ledger, counters, cache saves) failed with "database is locked", and the
    WAL grew by roughly the size of the corpus.
    """
    total, last = 0, 0
    while True:
        upper = conn.execute(
            "SELECT MAX(rowid) FROM (SELECT rowid FROM cache_pages WHERE rowid > ?"
            " ORDER BY rowid LIMIT ?)", (last, chunk)).fetchone()[0]
        if upper is None:
            return total
        cur = conn.execute(
            f"UPDATE cache_pages SET {set_sql} WHERE rowid > ? AND rowid <= ? AND ({where_sql})",
            (last, upper))
        total += max(0, cur.rowcount)
        conn.commit()
        last = upper


def invalidate_extraction_cache(
    db_path: Path,
    city: str | None = None,
    topic: str | None = None,
    urls: list[str] | None = None,
) -> int:
    """Remove community extraction results while preserving scraped page text.

    With no city/topic the invalidation is global. For a specific pair, URL
    hashes come from search_cache as well as cache_pages' denormalized pair
    columns; explicit URLs cover manually submitted or otherwise uncatalogued
    source pages.
    """
    import hashlib

    if not db_path.exists():
        return 0
    if (city is None) != (topic is None):
        raise ValueError("city and topic must be provided together")

    json_paths = (
        "'$.records', '$.extracted_at', '$.extract_duration_s',"
        " '$.extract_fingerprint', '$.extract_model',"
        " '$.enrich_scraped_at', '$.enrich_scrape_duration_s',"
        " '$.enrich_extracted_at', '$.enrich_extract_duration_s',"
        " '$.enrich_count', '$.enrich_model', '$.enrich_log'"
    )
    stale_predicate = (
        "(extracted_at IS NOT NULL OR extract_fingerprint IS NOT NULL"
        " OR json_extract(data, '$.records') IS NOT NULL)"
    )

    with _connect(db_path) as conn:
        if city is None:
            # records_count follows $.records out of the blob. The done-pair
            # verdict is already correct without this — extract_fingerprint
            # is NULL, which fails the currency check first — but a column
            # that disagrees with the blob it mirrors is a trap for the next
            # reader, and the backfill will not repair it (it only fills NULLs).
            return _update_cache_pages_in_chunks(
                conn,
                f"extracted_at=NULL, extract_fingerprint=NULL, "
                f"records_count={_NOT_EXTRACTED}, data=json_remove(data, {json_paths})",
                stale_predicate)

        target_hashes = {
            row[0]
            for row in conn.execute(
                "SELECT url_hash FROM cache_pages WHERE city=? AND topic=?",
                (city, topic),
            )
        }
        search_row = conn.execute(
            "SELECT urls FROM search_cache WHERE city=? AND topic=?",
            (city, topic),
        ).fetchone()
        if search_row and search_row[0]:
            target_hashes.update(
                hashlib.sha256(url.encode()).hexdigest()[:16]
                for url in json.loads(search_row[0])
            )
        target_hashes.update(
            hashlib.sha256(url.encode()).hexdigest()[:16]
            for url in (urls or [])
            if url
        )

        updated = 0
        hashes = sorted(target_hashes)
        for start in range(0, len(hashes), 500):
            chunk = hashes[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.execute(
                # records_count follows $.records out of the blob. The done-pair
                # verdict is already correct without this — extract_fingerprint
                # is NULL, which fails the currency check first — but a column
                # that disagrees with the blob it mirrors is a trap for the next
                # reader, and the backfill will not repair it (it only fills NULLs).
                f"UPDATE cache_pages SET extracted_at=NULL, extract_fingerprint=NULL, "
                f"records_count={_NOT_EXTRACTED}, "
                f"data=json_remove(data, {json_paths}) "
                f"WHERE url_hash IN ({placeholders}) AND {stale_predicate}",
                chunk,
            )
            updated += cur.rowcount
        conn.commit()
        return updated


def clear_person_cache(db_path: Path) -> int:
    """Strip person extraction fields from all cache entries, forcing re-extraction."""
    with _connect(db_path) as conn:
        return _update_cache_pages_in_chunks(
            conn,
            "person_fingerprint=NULL, data=json_remove(data, '$.person_extracted_at',"
            " '$.person_fingerprint', '$.person_model', '$.persons_data')",
            "person_fingerprint IS NOT NULL"
            " OR json_extract(data, '$.person_extracted_at') IS NOT NULL")


def count_cache_pages(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM cache_pages").fetchone()[0]


def get_cache_index(db_path: Path) -> list[dict]:
    """Cache listing without loading page texts: json_extract pulls only the
    small metadata keys (the old SELECT data + json.loads deserialized every
    full page text just to render a table)."""
    if not db_path.exists():
        return []
    small_keys = [
        "url_hash", "url", "domain", "city", "topic",
        "scraped_at", "scrape_duration_s", "extracted_at", "extract_duration_s",
        "enrich_scraped_at", "enrich_scrape_duration_s",
        "enrich_extracted_at", "enrich_extract_duration_s", "enrich_count",
        "extract_fingerprint", "extract_model", "enrich_model",
    ]
    select = ", ".join(f"json_extract(data, '$.{k}')" for k in small_keys)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {select},"
            " json_array_length(COALESCE(json_extract(data, '$.records'), '[]')),"
            " json_extract(data, '$.raw_text') IS NOT NULL"
            " FROM cache_pages ORDER BY url_hash"
        ).fetchall()
    entries = []
    for row in rows:
        entry = dict(zip(small_keys, row[:len(small_keys)]))
        entry["record_count"] = row[len(small_keys)] or 0
        entry["has_text"] = bool(row[len(small_keys) + 1])
        for k in ("url_hash", "url", "domain", "city", "topic"):
            entry[k] = entry[k] or ""
        entries.append(entry)
    return entries


def get_scraped_cache_for_search_pair(
    db_path: Path, city: str, topic: str
) -> list[tuple[str, str]]:
    """Return only one pair's ``(url, raw_text)`` rows.

    The old bulk helper materializes every raw page in memory before ai_only can
    process its first pair. At production scale that can trigger an OOM restart.
    Search-cache URLs remain authoritative; denormalized cache metadata is used
    only for pairs without a search row (for example manual legacy pages).
    """
    import hashlib

    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        search_row = conn.execute(
            "SELECT urls FROM search_cache WHERE city=? AND topic=?",
            (city, topic),
        ).fetchone()
        if search_row is None:
            rows = conn.execute(
                "SELECT data FROM cache_pages"
                " WHERE city=? AND topic=? AND scraped_at IS NOT NULL",
                (city, topic),
            ).fetchall()
            ordered_hashes = None
        else:
            try:
                urls = json.loads(search_row[0]) if search_row[0] else []
            except (TypeError, json.JSONDecodeError):
                log.warning("invalid_search_cache_urls", city=city, topic=topic)
                return []
            ordered_hashes = [hashlib.sha256(url.encode()).hexdigest()[:16] for url in urls]
            if not ordered_hashes:
                return []
            placeholders = ",".join("?" for _ in ordered_hashes)
            rows = conn.execute(
                f"SELECT url_hash, data FROM cache_pages"
                f" WHERE url_hash IN ({placeholders}) AND scraped_at IS NOT NULL",
                ordered_hashes,
            ).fetchall()

    if ordered_hashes is None:
        entries = []
        for (data_json,) in rows:
            try:
                entry = json.loads(data_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict) and entry.get("url") and entry.get("raw_text"):
                entries.append((entry["url"], entry["raw_text"]))
        return entries

    by_hash: dict[str, tuple[str, str]] = {}
    for url_hash, data_json in rows:
        try:
            entry = json.loads(data_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict) and entry.get("url") and entry.get("raw_text"):
            by_hash[url_hash] = (entry["url"], entry["raw_text"])
    return [by_hash[url_hash] for url_hash in ordered_hashes if url_hash in by_hash]


# ── False positives ───────────────────────────────────────────────────────────

def get_false_positives(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name, city, topic, reason, source_url, fp_type, marked_at "
            "FROM false_positives ORDER BY id"
        ).fetchall()
    return [
        {"name": r[0], "city": r[1], "topic": r[2], "reason": r[3] or "",
         "source_url": r[4] or "", "fp_type": r[5], "marked_at": r[6]}
        for r in rows
    ]


def upsert_false_positive(db_path: Path, name: str, city: str, topic: str,
                          reason: str, source_url: str, fp_type: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO false_positives (name, city, topic, reason, source_url, fp_type, marked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, city, topic, fp_type) DO UPDATE SET
                reason=excluded.reason,
                source_url=excluded.source_url,
                marked_at=excluded.marked_at
        """, (name, city, topic, reason, source_url, fp_type, now))
        conn.commit()


def delete_false_positive(db_path: Path, name: str, city: str, topic: str, fp_type: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM false_positives WHERE name=? AND city=? AND topic=? AND fp_type=?",
            (name, city, topic, fp_type)
        )
        conn.commit()


# ── Prompt history ────────────────────────────────────────────────────────────

def get_prompt_history(db_path: Path, fp_type: str) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT version, timestamp, content, fp_type, fp_count "
            "FROM prompt_history WHERE fp_type=? ORDER BY version",
            (fp_type,)
        ).fetchall()
    return [
        {"version": r[0], "timestamp": r[1], "content": r[2], "fp_type": r[3], "fp_count": r[4]}
        for r in rows
    ]


def append_prompt_history(db_path: Path, version: int, timestamp: str,
                          content: str, fp_type: str, fp_count: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO prompt_history (version, timestamp, content, fp_type, fp_count)
            VALUES (?, ?, ?, ?, ?)
        """, (version, timestamp, content, fp_type, fp_count))
        conn.commit()


# ── Prompt overrides ──────────────────────────────────────────────────────────

def get_prompt_overrides(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT key, content FROM prompt_overrides").fetchall()
    return {r[0]: r[1] for r in rows}


def upsert_prompt_override(db_path: Path, key: str, content: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO prompt_overrides (key, content, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
            (key, content, now),
        )
        conn.commit()


def delete_prompt_override(db_path: Path, key: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM prompt_overrides WHERE key=?", (key,))
        conn.commit()


# ── Search cache ───────────────────────────────────────────────────────────────

def save_search_cache(db_path: Path, city: str, topic: str,
                      urls: list[str], queries: list[str]) -> None:
    import json
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO search_cache (city, topic, urls, queries, cached_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(city, topic) DO UPDATE SET
                urls=excluded.urls, queries=excluded.queries,
                cached_at=excluded.cached_at, collected_at=NULL
        """, (city, topic, json.dumps(urls), json.dumps(queries), now))
        conn.commit()


def mark_search_collection_complete(db_path: Path, city: str, topic: str) -> None:
    """Mark a searched pair complete after every selected URL was attempted.

    Individual fetch failures remain visible in the run log, but do not keep the
    whole pair runnable forever. If the process dies before this call, the NULL
    marker makes the next collector resume the pair.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE search_cache SET collected_at=? WHERE city=? AND topic=?",
            (now, city, topic),
        )
        conn.commit()


def get_search_cache(db_path: Path, city: str, topic: str,
                     ttl_days: int) -> list[str] | None:
    if not db_path.exists():
        return None
    import json
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT urls FROM search_cache WHERE city=? AND topic=? AND cached_at>=?",
            (city, topic, cutoff)
        ).fetchone()
    return json.loads(row[0]) if row else None




def get_backlog_counts(db_path: Path, current_fp: str) -> dict:
    """How much work is queued, in one round trip.

    Written because the same question — "why is there so little for the
    extractor to do?" — kept being answered by inference from logs, and the
    logs only hold the last few minutes. Counting is cheap and the answer is
    exact.

    `pages_pending` is the number that decides whether an extraction window has
    anything to do: pages fetched and cached whose extraction at the *current*
    fingerprint is missing.
    """
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        def _one(sql: str, args: tuple = ()) -> int:
            return int(conn.execute(sql, args).fetchone()[0] or 0)

        return {
            "searched_pairs": _one("SELECT COUNT(*) FROM search_cache"),
            "collected_pairs": _one(
                "SELECT COUNT(*) FROM search_cache WHERE collected_at IS NOT NULL"),
            "pages_cached": _one("SELECT COUNT(*) FROM cache_pages"),
            # `scraped_at IS NOT NULL` is the marker for "this row has page
            # text"; the text itself lives inside the `data` JSON blob.
            "pages_scraped": _one(
                "SELECT COUNT(*) FROM cache_pages WHERE scraped_at IS NOT NULL"),
            "pages_pending": _one(
                "SELECT COUNT(*) FROM cache_pages"
                " WHERE scraped_at IS NOT NULL"
                "   AND (extract_fingerprint IS NULL OR extract_fingerprint != ?)",
                (current_fp,)),
            "communities": _one("SELECT COUNT(*) FROM communities WHERE hidden=0"),
            # long_description lives inside the `data` JSON blob, not a column.
            # json_extract needs SQLite's JSON1; if a deployment somehow lacks
            # it, the count is omitted rather than the whole answer lost.
            **({"unenriched": _one(
                "SELECT COUNT(*) FROM communities WHERE hidden=0"
                " AND (json_extract(data,'$.long_description') IS NULL"
                "   OR json_extract(data,'$.long_description') = '')")}
               if _has_json1(conn) else {}),
        }


def _has_json1(conn) -> bool:
    try:
        conn.execute("SELECT json_extract('{}', '$.x')")
        return True
    except sqlite3.OperationalError:
        return False


def get_covered_pairs(db_path: Path) -> set[tuple[str, str]]:
    """Return all (city, topic) pairs that have a search cache entry."""
    if not db_path.exists():
        return set()
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT city, topic FROM search_cache").fetchall()
    return {(r[0], r[1]) for r in rows}


# ── Venues ────────────────────────────────────────────────────────────────────

def upsert_venues(db_path: Path, records: list[dict]) -> int:
    if not records:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with _connect(db_path) as conn:
        for record in records:
            key = _venue_record_key(record["name"], record["city"])
            existing = conn.execute(
                "SELECT data FROM venues WHERE record_key=?", (key,)
            ).fetchone()
            old_data = json.loads(existing[0]) if existing else None
            if old_data:
                prev_urls: list[str] = old_data.get("source_urls") or []
                new_urls: list[str] = record.get("source_urls") or []
                merged = list(dict.fromkeys(new_urls + prev_urls))
                record = {**record, "source_urls": merged}
                # Merge community_ids
                prev_cids = old_data.get("community_ids") or []
                new_cids = record.get("community_ids") or []
                record["community_ids"] = list(dict.fromkeys(new_cids + prev_cids))
            conn.execute("""
                INSERT INTO venues (record_key, venue_id, city, data, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(record_key) DO UPDATE SET
                    data=excluded.data, venue_id=excluded.venue_id, updated_at=excluded.updated_at
            """, (key, record.get("venue_id", ""), record["city"],
                  json.dumps(record, ensure_ascii=False), now))
            _log_changes(conn, "venue_history", "venue_id",
                         record.get("venue_id", ""), _VENUE_HISTORY_FIELDS, old_data, record)
            count += 1
        conn.commit()
    return count


def get_venues(db_path: Path, city: str) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data FROM venues WHERE city=? ORDER BY id", (city,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_all_venues(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT data FROM venues ORDER BY city, id").fetchall()
    return [json.loads(r[0]) for r in rows]


def get_venues_by_city_topic(db_path: Path, city: str, topic: str) -> list[dict]:
    """Return venues in city whose welcomed_topics includes topic."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT v.data FROM venues v, json_each(json_extract(v.data, '$.welcomed_topics')) t
            WHERE v.city = ? AND t.value = ?
            ORDER BY v.id
            """,
            (city, topic),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_venue_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT city, COUNT(*) FROM venues GROUP BY city").fetchall()
    return {r[0]: r[1] for r in rows}


def get_venue_person_counts_by_url(db_path: Path) -> dict[str, dict]:
    """Return {url: {venues: n, persons: n}} for the progress page."""
    if not db_path.exists():
        return {}
    result: dict[str, dict] = {}
    with _connect(db_path) as conn:
        for (src_url, cnt) in conn.execute(
            "SELECT json_extract(data,'$.source_url'), COUNT(*) FROM venues"
            " WHERE json_extract(data,'$.source_url') IS NOT NULL"
            " GROUP BY json_extract(data,'$.source_url')"
        ).fetchall():
            result.setdefault(src_url, {"venues": 0, "persons": 0})["venues"] = cnt
        for (src_url, cnt) in conn.execute(
            "SELECT json_extract(data,'$.source_url'), COUNT(*) FROM persons"
            " WHERE json_extract(data,'$.source_url') IS NOT NULL"
            " GROUP BY json_extract(data,'$.source_url')"
        ).fetchall():
            result.setdefault(src_url, {"venues": 0, "persons": 0})["persons"] = cnt
    return result


# ── Persons ───────────────────────────────────────────────────────────────────

def delete_leader_persons_for_community(db_path: Path, community_name: str, city: str,
                                         only_synthesized: bool = False) -> int:
    """Delete role='leader' persons for a community before re-inserting clean parsed ones.

    only_synthesized=True restricts the delete to rows synthesized from the
    community's leader field (marked origin='leader_field' in data) — used for
    stale-leader cleanup so independently AI-extracted leader persons survive.

    Known limitation: rows synthesized before 2026-07-24 carry no origin marker
    and are indistinguishable from AI-extracted ones, so only_synthesized skips
    them (a safe migration is impossible — marking every role='leader' row
    would re-introduce the data-loss this marker prevents). They are still
    replaced whenever their community yields leaders again."""
    if not db_path.exists():
        return 0
    sql = ("DELETE FROM persons WHERE city=? AND role='leader' "
           "AND json_extract(data,'$.community_name')=?")
    if only_synthesized:
        sql += " AND json_extract(data,'$.origin')='leader_field'"
    with _connect(db_path) as conn:
        cur = conn.execute(sql, (city, community_name))
        conn.commit()
        return cur.rowcount


def upsert_persons(db_path: Path, records: list[dict]) -> int:
    if not records:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with _connect(db_path) as conn:
        for record in records:
            key = _person_record_key(
                record["name"], record["city"],
                record.get("role", "leader"), record.get("community_name", "")
            )
            existing = conn.execute(
                "SELECT data FROM persons WHERE record_key=?", (key,)
            ).fetchone()
            old_data = json.loads(existing[0]) if existing else None
            if old_data:
                prev_urls = old_data.get("source_urls") or []
                new_urls = record.get("source_urls") or []
                record = {**record, "source_urls": list(dict.fromkeys(new_urls + prev_urls))}
            conn.execute("""
                INSERT INTO persons (record_key, person_id, city, topic, role, data, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_key) DO UPDATE SET
                    data=excluded.data, person_id=excluded.person_id, updated_at=excluded.updated_at
            """, (key, record.get("person_id", ""), record["city"],
                  record.get("topic", ""), record.get("role", "leader"),
                  json.dumps(record, ensure_ascii=False), now))
            _log_changes(conn, "person_history", "person_id",
                         record.get("person_id", ""), _PERSON_HISTORY_FIELDS, old_data, record)
            count += 1
        conn.commit()
    return count


def get_persons(db_path: Path, city: str, topic: str | None = None) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        if topic:
            rows = conn.execute(
                "SELECT data FROM persons WHERE city=? AND topic=? ORDER BY role, id",
                (city, topic)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT data FROM persons WHERE city=? ORDER BY topic, role, id", (city,)
            ).fetchall()
    return [json.loads(r[0]) for r in rows]


def get_persons_for_community(db_path: Path, community_name: str, city: str) -> list[dict]:
    """Persons of one community: fetch the city's persons and match community_name
    by normalized form (the LLM's name variants differ in case/punctuation; the
    old record_key LIKE anchored on the wrong key segments and never matched)."""
    if not db_path.exists():
        return []
    target = normalized_match_key(community_name)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data FROM persons WHERE city=? ORDER BY role, id", (city,)
        ).fetchall()
    persons = [json.loads(r[0]) for r in rows]
    return [
        p for p in persons
        if normalized_match_key(p.get("community_name", "")) == target
    ]


def get_all_persons(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT data FROM persons ORDER BY city, id").fetchall()
    return [json.loads(r[0]) for r in rows]


def get_person_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT city, COUNT(*) FROM persons GROUP BY city").fetchall()
    return {r[0]: r[1] for r in rows}


def search_all(
    db_path: Path,
    query: str,
    limit: int = 20,
    cities: "set[str] | None" = None,
) -> dict[str, list[dict]]:
    """Search communities, venues, and persons by name or description.

    Returns a dict with lists of matching records from each table.
    Empty query returns empty results. Hidden communities are excluded.
    `cities` limits results to one site's cities: kozossegek.com's search
    returned foreign results whose links bounced to its home page, and they
    used up the result limit.
    """
    if not db_path.exists() or not query.strip():
        return {"communities": [], "venues": [], "persons": []}

    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    scope = " AND city IN (SELECT value FROM json_each(?))" if cities is not None else ""
    scope_args = (json.dumps(sorted(cities)),) if cities is not None else ()
    with _connect(db_path) as conn:
        community_rows = conn.execute(
            "SELECT data FROM communities WHERE hidden=0 AND ("
            "  json_extract(data,'$.name') LIKE ? ESCAPE '\\' OR"
            "  json_extract(data,'$.description') LIKE ? ESCAPE '\\'"
            f"){scope} ORDER BY city, id LIMIT ?",
            (pattern, pattern, *scope_args, limit),
        ).fetchall()
        venue_rows = conn.execute(
            "SELECT data FROM venues WHERE ("
            "  json_extract(data,'$.name') LIKE ? ESCAPE '\\' OR"
            "  json_extract(data,'$.description') LIKE ? ESCAPE '\\'"
            f"){scope} ORDER BY city, id LIMIT ?",
            (pattern, pattern, *scope_args, limit),
        ).fetchall()
        person_rows = conn.execute(
            "SELECT data FROM persons WHERE"
            f"  json_extract(data,'$.name') LIKE ? ESCAPE '\\'{scope} ORDER BY city, id LIMIT ?",
            (pattern, *scope_args, limit),
        ).fetchall()

    return {
        "communities": [json.loads(r[0]) for r in community_rows],
        "venues": [json.loads(r[0]) for r in venue_rows],
        "persons": [json.loads(r[0]) for r in person_rows],
    }


# ── Not-community reports ─────────────────────────────────────────────────────

def save_not_community_report(
    db_path: Path,
    community_id: str,
    community_name: str,
    city: str,
    topic: str,
    source_url: str,
    page_url: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO not_community_reports"
            " (community_id, community_name, city, topic, source_url, page_url, reported_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (community_id, community_name, city, topic, source_url, page_url, now),
        )
        conn.commit()
        return cur.lastrowid


def count_pending_interactions(db_path: Path) -> dict:
    """Pending user-submitted items for the admin Inbox badge.

    Not-community reports have no status column — every stored row is pending
    (handling deletes the row)."""
    counts = {"edit_requests": 0, "reports": 0, "submissions": 0, "total": 0}
    if not db_path or not db_path.exists():
        return counts
    with _connect(db_path) as conn:
        for key, sql in (
            ("edit_requests", "SELECT COUNT(*) FROM edit_requests WHERE status='pending'"),
            ("reports", "SELECT COUNT(*) FROM not_community_reports"),
            ("submissions", "SELECT COUNT(*) FROM community_submissions WHERE status='pending'"),
        ):
            try:
                counts[key] = conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                pass  # table not created yet on an old DB
    counts["total"] = counts["edit_requests"] + counts["reports"] + counts["submissions"]
    return counts


def get_not_community_reports(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, community_id, community_name, city, topic, source_url, page_url, reported_at"
            " FROM not_community_reports ORDER BY reported_at DESC"
        ).fetchall()
    return [
        {
            "id": r[0], "community_id": r[1], "community_name": r[2],
            "city": r[3], "topic": r[4], "source_url": r[5],
            "page_url": r[6], "reported_at": r[7],
        }
        for r in rows
    ]


def delete_not_community_report(db_path: Path, report_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM not_community_reports WHERE id=?", (report_id,))
        conn.commit()


# ── City requests ──────────────────────────────────────────────────────────────

def save_city_request(db_path: Path, city_name: str, email: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO city_requests (city_name, email, created_at) VALUES (?, ?, ?)",
            (city_name.strip(), email.strip(), now),
        )
        conn.commit()


# ── Duplicate candidates ───────────────────────────────────────────────────────

def insert_duplicate_candidate(
    db_path: Path,
    entity_type: str,
    winner_id: str,
    loser_id: str,
    winner_key: str,
    loser_key: str,
    similarity: float,
    signal: str,
) -> bool:
    """Record a duplicate pair once, whichever way round it is computed.

    Winner and loser carry meaning (winner = the record a merge keeps), so a
    re-scan may legitimately produce the reverse order for a pair already known.
    One pair must still be one row.

    The whole read-modify-write runs inside BEGIN IMMEDIATE. `idx_dup_pair` is a
    *partial* unique index (`WHERE resolution IS NULL`) on one orientation only,
    so it cannot express "this pair, either way round" — two writers racing on
    opposite orientations would both pass their own check and commit. Taking the
    write lock up front is what actually makes the rule hold. Without it, the
    2026-08-17 post-run scan died on "UNIQUE constraint failed:
    duplicate_candidates.entity_type, winner_key, loser_key".
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, winner_key, resolution, signal FROM duplicate_candidates"
            " WHERE entity_type=? AND ((winner_key=? AND loser_key=?)"
            "                       OR (winner_key=? AND loser_key=?))"
            # Manual first so the admin's row is the one kept, then oldest —
            # never storage order, which decided the outcome by luck.
            " ORDER BY (signal='manual') DESC, id",
            (entity_type, winner_key, loser_key, loser_key, winner_key),
        ).fetchall()

        if any(r[2] is not None for r in rows):
            # Already decided (merged or dismissed). A re-scan must not raise it
            # again, whatever orientation it computed this time — and a leftover
            # *pending* row for the same pair is exactly that: the decision
            # covers the pair, so a legacy reverse row would otherwise keep a
            # dismissed pair on the admin list forever.
            for row in rows:
                if row[2] is None:
                    conn.execute("DELETE FROM duplicate_candidates WHERE id=?", (row[0],))
            conn.commit()
            return False

        if rows:
            keep, *redundant = rows
            # Precedence: an admin's manual flag always wins, and stamps
            # signal='manual' so later auto scans cannot flip it back. An auto
            # re-scan may reorient another auto row, never a manual one.
            keep_is_manual = keep[3] == "manual"
            # Delete before reorienting, not after: the row we are about to
            # rewrite may be moving onto the orientation a redundant row still
            # occupies, and the partial unique index would reject the UPDATE.
            for row in redundant:
                conn.execute("DELETE FROM duplicate_candidates WHERE id=?", (row[0],))
            if signal == "manual" or not keep_is_manual:
                new_signal = "manual" if signal == "manual" else keep[3]
                if keep[1] != winner_key or new_signal != keep[3]:
                    conn.execute(
                        "UPDATE duplicate_candidates"
                        " SET winner_id=?, loser_id=?, winner_key=?, loser_key=?, signal=?"
                        " WHERE id=?",
                        (winner_id, loser_id, winner_key, loser_key, new_signal, keep[0]),
                    )
            conn.commit()
            return False

        conn.execute("""
            INSERT INTO duplicate_candidates
              (entity_type, winner_id, loser_id, winner_key, loser_key, similarity, signal, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (entity_type, winner_id, loser_id, winner_key, loser_key, similarity, signal, now))
        conn.commit()
        return True


def get_duplicate_candidates(
    db_path: Path,
    entity_type: str | None = None,
    resolved: bool = False,
) -> list[dict]:
    if not db_path.exists():
        return []
    clauses = []
    params: list = []
    if resolved:
        clauses.append("resolution IS NOT NULL")
    else:
        clauses.append("resolution IS NULL")
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect(db_path) as conn:
        cursor = conn.execute(
            f"SELECT * FROM duplicate_candidates {where} ORDER BY similarity DESC, detected_at DESC",
            params,
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def resolve_duplicate_candidate(db_path: Path, candidate_id: int, resolution: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE duplicate_candidates SET resolution=?, resolved_at=? WHERE id=?",
            (resolution, now, candidate_id),
        )
        conn.commit()


def delete_duplicate_candidate(db_path: Path, candidate_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM duplicate_candidates WHERE id=?", (candidate_id,))
        conn.commit()


# ── Wrong-city candidates ──────────────────────────────────────────────────────

def insert_wrong_city_candidate(
    db_path: Path,
    record_key: str,
    community_id: str,
    mentioned_city: str,
    field: str,
    snippet: str,
    matched_text: str,
) -> bool:
    """Insert a wrong-city candidate. Returns False if the (record, city) pair
    was already flagged — dismissed pairs stay dismissed and are not re-raised."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO wrong_city_candidates"
                " (record_key, community_id, mentioned_city, field, snippet,"
                "  matched_text, detected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record_key, community_id, mentioned_city, field, snippet,
                 matched_text, now),
            )
        except sqlite3.IntegrityError:
            return False
        conn.commit()
    return True


def get_wrong_city_candidates(db_path: Path, resolved: bool | None = False) -> list[dict]:
    if not db_path.exists():
        return []
    where = ""
    if resolved is True:
        where = "WHERE resolution IS NOT NULL"
    elif resolved is False:
        where = "WHERE resolution IS NULL"
    with _connect(db_path) as conn:
        cursor = conn.execute(
            f"SELECT * FROM wrong_city_candidates {where} ORDER BY detected_at DESC, id DESC"
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def resolve_wrong_city_candidate(db_path: Path, candidate_id: int, resolution: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE wrong_city_candidates SET resolution=?, resolved_at=? WHERE id=?",
            (resolution, now, candidate_id),
        )
        conn.commit()


def get_entity_by_record_key(db_path: Path, entity_type: str,
                             record_key: str) -> dict | None:
    table = {"venue": "venues", "person": "persons"}.get(entity_type)
    if table is None or not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT data FROM {table} WHERE record_key=?", (record_key,)).fetchone()
    return json.loads(row[0]) if row else None


def merge_entity_into(db_path: Path, entity_type: str,
                      winner_key: str, loser_key: str) -> bool:
    """Merge a venue/person duplicate: fill the winner's empty fields from the
    loser, union source_urls, delete the loser row. Returns False when either
    record is missing (candidate is stale)."""
    table = {"venue": "venues", "person": "persons"}.get(entity_type)
    if table is None:
        return False
    with _connect(db_path) as conn:
        winner_row = conn.execute(
            f"SELECT data FROM {table} WHERE record_key=?", (winner_key,)).fetchone()
        loser_row = conn.execute(
            f"SELECT data FROM {table} WHERE record_key=?", (loser_key,)).fetchone()
        if not winner_row or not loser_row:
            return False
        winner_data = json.loads(winner_row[0])
        loser_data = json.loads(loser_row[0])
        for field, value in loser_data.items():
            if not value:
                continue
            current = winner_data.get(field)
            if isinstance(value, list) or isinstance(current, list):
                # Relationship/list fields (community_ids, welcomed_topics,
                # social_links, …) are unioned — keeping only the winner's list
                # would silently drop the loser's associations.
                merged = list(current or []) + [v for v in value
                                                if v not in (current or [])]
                winner_data[field] = merged
            elif not current:
                winner_data[field] = value
        w_urls = list(winner_data.get("source_urls") or [])
        if winner_data.get("source_url") and winner_data["source_url"] not in w_urls:
            w_urls = [winner_data["source_url"]] + w_urls
        l_urls = list(loser_data.get("source_urls") or [])
        if loser_data.get("source_url") and loser_data["source_url"] not in l_urls:
            l_urls = [loser_data["source_url"]] + l_urls
        winner_data["source_urls"] = list(dict.fromkeys(w_urls + l_urls))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            f"UPDATE {table} SET data=?, updated_at=? WHERE record_key=?",
            (json.dumps(winner_data, ensure_ascii=False), now, winner_key),
        )
        conn.execute(f"DELETE FROM {table} WHERE record_key=?", (loser_key,))
        conn.commit()
    return True


def apply_venue_edit(db_path: Path, record_key: str, change_type: str,
                     new_value: str | None) -> bool:
    """Apply an approved venue edit. 'closed' deletes the venue (venues have no
    hidden flag); 'name_correction' renames and recomputes the record key.
    'wrong_info' carries free-text notes only and cannot be auto-applied."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM venues WHERE record_key=?", (record_key,)).fetchone()
        if not row:
            return False
        if change_type == "closed":
            conn.execute("DELETE FROM venues WHERE record_key=?", (record_key,))
            conn.commit()
            return True
        if change_type == "name_correction" and new_value:
            data = json.loads(row[0])
            data["name"] = new_value
            new_key = _venue_record_key(new_value, data.get("city", ""))
            try:
                conn.execute(
                    "UPDATE venues SET record_key=?, data=?, updated_at=? WHERE record_key=?",
                    (new_key, json.dumps(data, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(), record_key),
                )
            except sqlite3.IntegrityError:
                return False  # target key exists — surface as failed edit
            conn.commit()
            return True
    return False


def merge_community_into(
    db_path: Path,
    winner_key: str,
    loser_key: str,
    candidate_id: int | None = None,
) -> None:
    with _connect(db_path) as conn:
        winner_row = conn.execute(
            "SELECT data FROM communities WHERE record_key=?", (winner_key,)
        ).fetchone()
        loser_row = conn.execute(
            "SELECT data FROM communities WHERE record_key=?", (loser_key,)
        ).fetchone()
        if not winner_row or not loser_row:
            return
        winner_data = json.loads(winner_row[0])
        loser_data = json.loads(loser_row[0])
        # Merge source_urls
        w_urls = list(winner_data.get("source_urls") or [])
        if winner_data.get("source_url") and winner_data["source_url"] not in w_urls:
            w_urls = [winner_data["source_url"]] + w_urls
        l_urls = list(loser_data.get("source_urls") or [])
        if loser_data.get("source_url") and loser_data["source_url"] not in l_urls:
            l_urls = [loser_data["source_url"]] + l_urls
        merged_urls = list(dict.fromkeys(w_urls + l_urls))
        winner_data["source_urls"] = merged_urls
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE communities SET data=?, updated_at=? WHERE record_key=?",
            (json.dumps(winner_data, ensure_ascii=False), now, winner_key),
        )
        conn.execute(
            "UPDATE communities SET hidden=1 WHERE record_key=?",
            (loser_key,),
        )
        if candidate_id is not None:
            now2 = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE duplicate_candidates SET resolution=?, resolved_at=? WHERE id=?",
                ("merged", now2, candidate_id),
            )
        conn.commit()


def save_community_data(db_path: Path, record_key: str, data: dict) -> None:
    """Overwrite the data blob for a community record."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE communities SET data=?, updated_at=? WHERE record_key=?",
            (json.dumps(data, ensure_ascii=False), now, record_key),
        )
        conn.commit()


# ── Edit requests ──────────────────────────────────────────────────────────────

def save_edit_request(
    db_path: Path,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    entity_city: str,
    entity_topic: str,
    record_key: str,
    change_type: str,
    new_value: str | None,
    notes: str,
    email: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO edit_requests"
            " (entity_type, entity_id, entity_name, entity_city, entity_topic,"
            "  record_key, change_type, new_value, notes, email, submitted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, entity_name, entity_city, entity_topic,
             record_key, change_type, new_value, notes, email, now),
        )
        conn.commit()
        return cur.lastrowid


def get_edit_requests(db_path: Path, status: str = "pending") -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT * FROM edit_requests WHERE status=? ORDER BY submitted_at DESC",
            (status,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def resolve_edit_request(db_path: Path, request_id: int, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE edit_requests SET status=?, reviewed_at=? WHERE id=?",
            (status, now, request_id),
        )
        conn.commit()


def apply_community_edit(
    db_path: Path,
    record_key: str,
    change_type: str,
    new_value: str | None,
) -> str:
    """Apply an approved edit to a community record.

    Returns a status string: "ok" (applied), "merged" (target identity already
    existed — this row was merged into it), "not_found", or "unsupported".
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM communities WHERE record_key=?", (record_key,)
        ).fetchone()
        if not row:
            return "not_found"
        data = json.loads(row[0])
        now = datetime.now(timezone.utc).isoformat()
        if change_type in ("archive", "delete"):
            conn.execute(
                "UPDATE communities SET hidden=1, updated_at=? WHERE record_key=?",
                (now, record_key),
            )
        elif change_type in ("wrong_city", "wrong_topic", "name_correction"):
            if change_type == "wrong_city":
                data["city"] = new_value
            elif change_type == "wrong_topic":
                data["topic"] = new_value
            else:
                data["name"] = new_value
            # record_key derives from (name, city, topic) — leaving it stale made
            # the next scrape insert a duplicate row and broke follow-up edits.
            new_key = _community_record_key(data["name"], data["city"], data["topic"])
            try:
                conn.execute(
                    "UPDATE communities SET record_key=?, city=?, topic=?, data=?, updated_at=?"
                    " WHERE record_key=?",
                    (new_key, data["city"], data["topic"],
                     json.dumps(data, ensure_ascii=False), now, record_key),
                )
            except sqlite3.IntegrityError:
                # The corrected identity already exists (typically the scraper
                # also found this community under its real city). Merge this
                # row into the existing target: union source_urls, make sure
                # the target is visible, hide this row. Done on the same
                # connection — a second connection would deadlock here.
                target = conn.execute(
                    "SELECT data FROM communities WHERE record_key=?", (new_key,)
                ).fetchone()
                if not target:
                    return "not_found"
                target_data = json.loads(target[0])
                urls: list[str] = []
                for d in (target_data, data):
                    if d.get("source_url") and d["source_url"] not in urls:
                        urls.append(d["source_url"])
                    for u in d.get("source_urls") or []:
                        if u not in urls:
                            urls.append(u)
                target_data["source_urls"] = urls
                conn.execute(
                    "UPDATE communities SET data=?, hidden=0, updated_at=? WHERE record_key=?",
                    (json.dumps(target_data, ensure_ascii=False), now, new_key),
                )
                conn.execute(
                    "UPDATE communities SET hidden=1, updated_at=? WHERE record_key=?",
                    (now, record_key),
                )
                conn.commit()
                return "merged"
        else:
            return "unsupported"
        conn.commit()
    return "ok"


# ── Community Submissions ─────────────────────────────────────────────────────

def save_community_submission(
    db_path: Path,
    name: str,
    city: str,
    topic: str,
    source_url: str,
    submitter_email: str | None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO community_submissions (name, city, topic, source_url, submitter_email, submitted_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (name, city, topic, source_url, submitter_email,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_community_submissions(db_path: Path, status: str = "pending") -> list[dict]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, name, city, topic, source_url, submitter_email, submitted_at, status "
            "FROM community_submissions WHERE status=? ORDER BY submitted_at DESC",
            (status,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def resolve_community_submission(db_path: Path, sub_id: int, status: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE community_submissions SET status=? WHERE id=?",
            (status, sub_id),
        )
        conn.commit()


# ── Recategorize suggestions ───────────────────────────────────────────────────

def get_data_quality_stats(db_path: Path) -> dict:
    empty: dict = {
        "total": 0, "visible": 0, "hidden": 0,
        "cities": 0, "topics": 0,
        "has_website": 0, "has_contact": 0, "has_description": 0, "has_any": 0,
        "city_rows": [], "topic_counts": {},
    }
    if not db_path.exists():
        return empty
    with _connect(db_path) as conn:
        row = conn.execute("""
            SELECT
              COUNT(*) as total,
              SUM(CASE WHEN hidden=0 THEN 1 ELSE 0 END) as visible,
              SUM(CASE WHEN hidden=1 THEN 1 ELSE 0 END) as hidden,
              COUNT(DISTINCT CASE WHEN hidden=0 THEN city END) as cities,
              COUNT(DISTINCT CASE WHEN hidden=0 THEN topic END) as topics,
              SUM(CASE WHEN hidden=0
                   AND json_extract(data,'$.website') IS NOT NULL
                   AND json_extract(data,'$.website') != '' THEN 1 ELSE 0 END) as has_website,
              SUM(CASE WHEN hidden=0
                   AND json_extract(data,'$.contact') IS NOT NULL
                   AND json_extract(data,'$.contact') != '' THEN 1 ELSE 0 END) as has_contact,
              SUM(CASE WHEN hidden=0
                   AND length(COALESCE(json_extract(data,'$.description'),'')) > 50
                   THEN 1 ELSE 0 END) as has_description,
              SUM(CASE WHEN hidden=0 AND (
                   (json_extract(data,'$.website') IS NOT NULL AND json_extract(data,'$.website') != '')
                   OR
                   (json_extract(data,'$.contact') IS NOT NULL AND json_extract(data,'$.contact') != '')
                 ) THEN 1 ELSE 0 END) as has_any
            FROM communities
        """).fetchone()
        city_rows = conn.execute("""
            SELECT city, COUNT(*) as cnt,
              SUM(CASE WHEN json_extract(data,'$.website') IS NOT NULL
                       AND json_extract(data,'$.website') != '' THEN 1 ELSE 0 END) as w,
              SUM(CASE WHEN json_extract(data,'$.contact') IS NOT NULL
                       AND json_extract(data,'$.contact') != '' THEN 1 ELSE 0 END) as c
            FROM communities
            WHERE hidden=0
            GROUP BY city
            ORDER BY cnt DESC
            LIMIT 20
        """).fetchall()
    topic_counts = get_topic_counts(db_path)
    return {
        "total": row[0] or 0,
        "visible": row[1] or 0,
        "hidden": row[2] or 0,
        "cities": row[3] or 0,
        "topics": row[4] or 0,
        "has_website": row[5] or 0,
        "has_contact": row[6] or 0,
        "has_description": row[7] or 0,
        "has_any": row[8] or 0,
        "city_rows": [{"city": r[0], "cnt": r[1], "w": r[2] or 0, "c": r[3] or 0} for r in city_rows],
        "topic_counts": topic_counts,
    }


def get_activity_timeline(db_path: Path, period: str) -> list[dict]:
    """Return per-bucket activity counts for the given period.

    period: "24h" (hourly, last 24 h), "7d" (daily, last 7 d), "12m" (monthly, last 12 m)
    """
    from datetime import timedelta

    if not db_path.exists():
        return []

    now = datetime.now(timezone.utc)

    if period == "24h":
        fmt = "%Y-%m-%dT%H"
        now - timedelta(hours=24)
        since_sql = "datetime('now', '-24 hours')"
        buckets = [(now - timedelta(hours=i)).strftime(fmt) for i in range(23, -1, -1)]
        display = {b: b[-2:] + ":00" for b in buckets}
    elif period == "7d":
        fmt = "%Y-%m-%d"
        since_sql = "datetime('now', '-7 days')"
        buckets = [(now - timedelta(days=i)).strftime(fmt) for i in range(6, -1, -1)]
        display = {b: datetime.strptime(b, "%Y-%m-%d").strftime("%b %d") for b in buckets}
    else:  # "12m"
        fmt = "%Y-%m"
        since_sql = "datetime('now', '-12 months')"
        buckets = []
        display = {}
        for i in range(11, -1, -1):
            m = now.month - i
            y = now.year
            while m <= 0:
                m += 12
                y -= 1
            key = f"{y:04d}-{m:02d}"
            buckets.append(key)
            display[key] = datetime(y, m, 1).strftime("%b %Y")

    rows: dict[str, dict] = {b: {
        "bucket": b, "label": display[b],
        "scrapes": 0, "extractions": 0,
        "enrich_scrapes": 0, "enrich_ai": 0,
        "new_communities": 0, "community_changes": 0,
        "new_venues": 0, "new_persons": 0,
    } for b in buckets}

    def _run(conn: sqlite3.Connection, sql: str, key: str) -> None:
        for bkt, cnt in conn.execute(sql).fetchall():
            if bkt and bkt in rows:
                rows[bkt][key] = cnt

    strftime_col = f"strftime('{fmt}', {{col}})"

    with _connect(db_path) as conn:
        _run(conn, f"""
            SELECT {strftime_col.format(col='scraped_at')}, COUNT(*)
            FROM cache_pages WHERE scraped_at >= {since_sql}
            GROUP BY 1
        """, "scrapes")
        _run(conn, f"""
            SELECT {strftime_col.format(col='extracted_at')}, COUNT(*)
            FROM cache_pages WHERE extracted_at >= {since_sql}
            GROUP BY 1
        """, "extractions")
        _run(conn, f"""
            SELECT {strftime_col.format(col="json_extract(data,'$.enrich_scraped_at')")}, COUNT(*)
            FROM cache_pages
            WHERE json_extract(data,'$.enrich_scraped_at') >= {since_sql}
            GROUP BY 1
        """, "enrich_scrapes")
        _run(conn, f"""
            SELECT {strftime_col.format(col="json_extract(data,'$.enrich_extracted_at')")}, COUNT(*)
            FROM cache_pages
            WHERE json_extract(data,'$.enrich_extracted_at') >= {since_sql}
            GROUP BY 1
        """, "enrich_ai")
        # MIN(changed_at) per community_id — delete+reinsert cycles (topic
        # replace, dedup churn) re-log __created__ and would double-count.
        _run(conn, f"""
            SELECT {strftime_col.format(col='first_seen')}, COUNT(*)
            FROM (
                SELECT community_id, MIN(changed_at) AS first_seen
                FROM community_history
                WHERE field='__created__'
                GROUP BY community_id
            )
            WHERE first_seen >= {since_sql}
            GROUP BY 1
        """, "new_communities")
        _run(conn, f"""
            SELECT {strftime_col.format(col='changed_at')}, COUNT(*)
            FROM community_history
            WHERE field!='__created__' AND changed_at >= {since_sql}
            GROUP BY 1
        """, "community_changes")
        _run(conn, f"""
            SELECT {strftime_col.format(col='first_seen')}, COUNT(*)
            FROM (
                SELECT venue_id, MIN(changed_at) AS first_seen
                FROM venue_history
                WHERE field='__created__'
                GROUP BY venue_id
            )
            WHERE first_seen >= {since_sql}
            GROUP BY 1
        """, "new_venues")
        # Use MIN(changed_at) per person_id so delete+reinsert cycles only count once.
        _run(conn, f"""
            SELECT {strftime_col.format(col='first_seen')}, COUNT(*)
            FROM (
                SELECT person_id, MIN(changed_at) AS first_seen
                FROM person_history
                WHERE field='__created__'
                GROUP BY person_id
            )
            WHERE first_seen >= {since_sql}
            GROUP BY 1
        """, "new_persons")

    return [rows[b] for b in buckets]


# ── Outclick tracking ─────────────────────────────────────────────────────────

def log_outclick(db_path: Path, community_id: str, url: str, link_type: str) -> None:
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO outclick_events (community_id, url, link_type) VALUES (?, ?, ?)",
                (community_id, url, link_type),
            )
            conn.commit()
    except Exception:
        pass


def is_known_community_url(db_path: Path, community_id: str, url: str) -> bool:
    """Whether an outbound URL is actually rendered for this community.

    The analytics endpoint is public, so accepting arbitrary URLs would make it
    an unauthenticated database-growth primitive. Identity spans topics; inspect
    every row carrying the community_id rather than whichever one sorts first.
    """
    if not db_path.exists() or not community_id or not url:
        return False
    wanted = url.rstrip("/")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data FROM communities WHERE community_id=? AND hidden=0",
            (community_id,),
        ).fetchall()
    for row in rows:
        try:
            record = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = [record.get("website"), record.get("source_url")]
        candidates.extend(record.get("source_urls") or [])
        candidates.extend(record.get("social_links") or [])
        for value in candidates:
            if not isinstance(value, str) or not value:
                continue
            # The stored value may have no scheme — `public_community.html`
            # renders those as `https://…`, and the browser reports the URL it
            # actually followed. Comparing against the bare form would reject
            # every such event and lose the analytics silently, which is worse
            # than not collecting them: the number would look real.
            if value.rstrip("/") == wanted:
                return True
            if not value.startswith(("http://", "https://")) and \
                    f"https://{value}".rstrip("/") == wanted:
                return True
    return False


# ── Daily traffic + report ────────────────────────────────────────────────────

def record_pageview(db_path: Path, day: str, site: str, visitor_hash: str) -> None:
    """One public page hit. Lightweight server-side counter (bot-filtered by the
    caller); uniques approximated by a per-day visitor hash."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO traffic_daily(day, site, pageviews) VALUES(?,?,1)"
            " ON CONFLICT(day, site) DO UPDATE SET pageviews = pageviews + 1",
            (day, site))
        conn.execute(
            "INSERT OR IGNORE INTO traffic_visitors(day, site, visitor_hash) VALUES(?,?,?)",
            (day, site, visitor_hash))
        conn.commit()


# ── Joinability gate ─────────────────────────────────────────────────────────

def save_gate_decision(db_path: Path, url_hash: str, fingerprint: str,
                       score: float, model: str = "", url: str = "") -> None:
    """Record one gate score. Best-effort: a gate that cannot write must not
    stop a run — the page is simply re-scored next time."""
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO gate_decisions"
                " (url_hash, fingerprint, score, model, url, decided_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(url_hash, fingerprint) DO UPDATE SET"
                "   score=excluded.score, model=excluded.model,"
                "   decided_at=excluded.decided_at, released_at=NULL",
                (url_hash, fingerprint, float(score), model, url,
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()
    except Exception as exc:
        log.warning("gate_decision_write_failed", url_hash=url_hash, error=str(exc))


def get_gate_scores(db_path: Path, fingerprint: str) -> dict[str, float]:
    """Every scored page at this fingerprint, as `{url_hash: score}`.

    Released decisions are omitted, so an admin release is a re-scoring rather
    than a permanent exemption: the page is asked again, and may be rejected
    again if the answer has not changed.
    """
    if not db_path.exists():
        return {}
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT url_hash, score FROM gate_decisions"
                " WHERE fingerprint=? AND released_at IS NULL",
                (fingerprint,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {h: float(s) for h, s in rows}


def release_gate_decision(db_path: Path, url_hash: str,
                          fingerprint: str | None = None) -> int:
    """Hand a page back to extraction. Returns how many rows were released."""
    with _connect(db_path) as conn:
        if fingerprint:
            cur = conn.execute(
                "UPDATE gate_decisions SET released_at=? WHERE url_hash=? AND fingerprint=?",
                (datetime.now(timezone.utc).isoformat(), url_hash, fingerprint))
        else:
            cur = conn.execute(
                "UPDATE gate_decisions SET released_at=? WHERE url_hash=?",
                (datetime.now(timezone.utc).isoformat(), url_hash))
        conn.commit()
        return cur.rowcount


def get_gate_rejections(db_path: Path, fingerprint: str, threshold: float,
                        limit: int = 200) -> list[dict]:
    """The pages this gate is currently holding back, lowest score first."""
    if not db_path.exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT url_hash, url, score, model, decided_at FROM gate_decisions"
                " WHERE fingerprint=? AND released_at IS NULL AND score < ?"
                " ORDER BY score LIMIT ?",
                (fingerprint, float(threshold), int(limit))).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"url_hash": h, "url": u, "score": s, "model": m, "decided_at": d}
            for h, u, s, m, d in rows]


def bump_daily_counter(db_path: Path, day: str, name: str, amount: int = 1) -> None:
    """Add to a per-day counter. Best-effort: never fail the caller's work."""
    if amount <= 0:
        return
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO daily_counters(day, name, value) VALUES(?,?,?)"
                " ON CONFLICT(day, name) DO UPDATE SET value = value + excluded.value",
                (day, name, int(amount)))
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — a counter must not stop a run
        log.warning("daily_counter_failed", name=name, error=str(exc))


def get_daily_counters_with_prefix(db_path: Path, day: str,
                                   prefix: str) -> dict[str, int]:
    """One day's counters whose name starts with `prefix`, keyed by the rest.

    Lets a caller read a family of counters — one per model, say — without
    knowing in advance which members exist, which is the point when the fleet's
    membership changes week to week.
    """
    if not db_path.exists():
        return {}
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name, value FROM daily_counters WHERE day=? AND name LIKE ?",
                (day, prefix.replace("%", "") + "%")).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0][len(prefix):]: int(r[1]) for r in rows if r[0].startswith(prefix)}


def get_daily_counter(db_path: Path, day: str, name: str) -> int:
    if not db_path.exists():
        return 0
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM daily_counters WHERE day=? AND name=?",
                (day, name)).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def get_traffic_for_day(db_path: Path, day: str) -> dict:
    """{site: {"pageviews": n, "visitors": m}} for one UTC day."""
    if not db_path.exists():
        return {}
    out: dict = {}
    with _connect(db_path) as conn:
        for site, pv in conn.execute(
                "SELECT site, pageviews FROM traffic_daily WHERE day=?", (day,)).fetchall():
            out[site] = {"pageviews": pv, "visitors": 0}
        for site, uniq in conn.execute(
                "SELECT site, COUNT(*) FROM traffic_visitors WHERE day=? GROUP BY site",
                (day,)).fetchall():
            out.setdefault(site, {"pageviews": 0, "visitors": 0})["visitors"] = uniq
    return out


def get_daily_summary(db_path: Path, start_iso: str, end_iso: str,
                      hu_cities: set) -> dict:
    """Changes between two ISO timestamps, split into 'hu' / 'intl' scopes."""
    empty = {"new_communities": 0, "changed_communities": 0, "change_rows": 0,
             "new_venues": 0, "new_persons": 0, "pages_scraped": 0,
             "pages_extracted": 0, "searches": 0}
    stock_empty = {"communities": 0, "venues": 0, "persons": 0,
                   "pages_cached": 0, "pages_extracted": 0, "covered_pairs": 0}
    result = {"hu": dict(empty), "intl": dict(empty), "runs": [],
              "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
              "stock": {"hu": dict(stock_empty), "intl": dict(stock_empty)}}
    if not db_path.exists():
        return result

    def scope(city: str) -> str:
        return "hu" if city in hu_cities else "intl"

    with _connect(db_path) as conn:
        for city, cnt in conn.execute("""
            SELECT c.city, COUNT(DISTINCT c.community_id) FROM communities c
            JOIN (SELECT community_id, MIN(changed_at) AS fs FROM community_history
                  WHERE field='__created__' GROUP BY community_id) h
              ON h.community_id = c.community_id
            WHERE c.hidden = 0 AND h.fs >= ? AND h.fs < ?
            GROUP BY c.city
        """, (start_iso, end_iso)).fetchall():
            result[scope(city)]["new_communities"] += cnt

        for city, ids, rows in conn.execute("""
            SELECT c.city, COUNT(DISTINCT ch.community_id), COUNT(DISTINCT ch.id)
            FROM community_history ch
            JOIN communities c ON c.community_id = ch.community_id
            WHERE ch.field != '__created__' AND ch.changed_at >= ? AND ch.changed_at < ?
            GROUP BY c.city
        """, (start_iso, end_iso)).fetchall():
            result[scope(city)]["changed_communities"] += ids
            result[scope(city)]["change_rows"] += rows

        for table, id_col, target, key in (
            ("venue_history", "venue_id", "venues", "new_venues"),
            ("person_history", "person_id", "persons", "new_persons"),
        ):
            for city, cnt in conn.execute(f"""
                SELECT t.city, COUNT(DISTINCT t.{id_col}) FROM {target} t
                JOIN (SELECT {id_col} AS eid, MIN(changed_at) AS fs FROM {table}
                      WHERE field='__created__' GROUP BY {id_col}) h
                  ON h.eid = t.{id_col}
                WHERE h.fs >= ? AND h.fs < ?
                GROUP BY t.city
            """, (start_iso, end_iso)).fetchall():
                result[scope(city)][key] += cnt

        for col, key in (("scraped_at", "pages_scraped"), ("extracted_at", "pages_extracted")):
            for city, cnt in conn.execute(
                    f"SELECT city, COUNT(*) FROM cache_pages WHERE {col} >= ? AND {col} < ? GROUP BY city",
                    (start_iso, end_iso)).fetchall():
                result[scope(city or "")][key] += cnt

        for city, cnt in conn.execute(
                "SELECT city, COUNT(*) FROM search_cache WHERE cached_at >= ? AND cached_at < ? GROUP BY city",
                (start_iso, end_iso)).fetchall():
            result[scope(city)]["searches"] += cnt

        for row in conn.execute(
            "SELECT id, run_mode, started_at, finished_at, success, search_log, error, "
                f"{_OUTCOME_SQL}"
                " FROM runs WHERE started_at >= ? AND started_at < ? ORDER BY started_at",
                (start_iso, end_iso)).fetchall():
            interrupted_error = (
                "run unfinished (still running, container restart, or OOM)"
                if not row[3] and not row[6] else ""
            )
            run = {"id": row[0], "mode": row[1], "started_at": row[2],
                   "finished_at": row[3], "success": bool(row[4]),
                   # An unfinished row is an abort however its columns read.
                   "outcome": "aborted" if interrupted_error else row[7],
                   "pairs": 0, "records": 0, "search_failed": 0, "extract_failed": 0,
                   "search_error": "", "extract_error": "",
                   "error": row[6] or interrupted_error}
            if row[5]:
                try:
                    logs = json.loads(row[5])
                    run["pairs"] = len(logs)
                    run["records"] = sum(p.get("records_extracted", 0) for p in logs)
                    run["search_failed"] = sum(1 for p in logs if p.get("search_failed"))
                    run["extract_failed"] = sum(p.get("extract_failed", 0) for p in logs)
                    run["search_error"] = next(
                        (p["search_error"] for p in logs if p.get("search_error")), "")
                    run["extract_error"] = next(
                        (p["extract_error"] for p in logs if p.get("extract_error")), "")
                except Exception:
                    pass
            result["runs"].append(run)

        for city, cnt in conn.execute(
                "SELECT city, COUNT(*) FROM communities WHERE hidden=0 GROUP BY city").fetchall():
            result["totals"][scope(city)] += cnt
            result["stock"][scope(city)]["communities"] += cnt
        for city, cnt in conn.execute(
                "SELECT city, COUNT(*) FROM search_cache GROUP BY city").fetchall():
            result["totals"]["covered_pairs_" + scope(city)] += cnt
            result["stock"][scope(city)]["covered_pairs"] += cnt

        for table, key in (("venues", "venues"), ("persons", "persons")):
            for city, cnt in conn.execute(
                    f"SELECT city, COUNT(*) FROM {table} GROUP BY city").fetchall():
                result["stock"][scope(city or "")][key] += cnt
        for city, cnt in conn.execute(
                "SELECT city, COUNT(*) FROM cache_pages GROUP BY city").fetchall():
            result["stock"][scope(city or "")]["pages_cached"] += cnt
        for city, cnt in conn.execute(
                "SELECT city, COUNT(*) FROM cache_pages WHERE extracted_at IS NOT NULL"
                " GROUP BY city").fetchall():
            result["stock"][scope(city or "")]["pages_extracted"] += cnt
    return result


# ── Provider quota ledger (free-tier model router) ────────────────────────────

def get_provider_usage(db_path: Path, day: str) -> dict[str, dict]:
    """{provider: {calls, failures, rate_limits, observed_limit, blocked_until,
    last_error}} for one UTC day. Missing providers simply have no row."""
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM provider_usage WHERE day=?", (day,)).fetchall()
    return {r["provider"]: dict(r) for r in rows}


def record_provider_call(
    db_path: Path,
    day: str,
    provider: str,
    *,
    ok: bool = True,
    rate_limited: bool = False,
    blocked_until: float | None = None,
    error: str | None = None,
    observed_limit: int | None = None,
    observed_at: float | None = None,
    tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Count one provider call against its daily budget.

    Every attempt increments `calls`, including failures: a 429 or a 400 still
    consumed a request slot at most providers, and undercounting is what makes a
    router walk straight into a hard block.

    `cost_usd` follows the same rule and for the same reason. A refused call is
    usually free, but a *truncated* one is charged in full — that is most of what
    a reasoning model costs us — so the price is taken from the usage the
    provider itself reported, not from whether we could use the answer.

    `observed_limit` is only ever lowered *here*, never raised: within one
    confirmation a provider that proved it stops at N must not be talked back up
    by a later optimistic number. Raising it is a separate, deliberate act —
    `clear_observed_limit`, called when a request actually succeeds past the
    ceiling, which is direct evidence the ceiling was wrong.

    `observed_at` stamps every confirmation, including one that does not change
    the value. The router stops trusting a stale pin, so a refusal that merely
    re-confirms an existing ceiling still has to say so.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO provider_usage (day, provider) VALUES (?, ?)",
            (day, provider),
        )
        conn.execute(
            """
            UPDATE provider_usage
               SET calls          = calls + 1,
                   tokens         = tokens + ?,
                   cost_usd       = cost_usd + ?,
                   failures       = failures + ?,
                   rate_limits    = rate_limits + ?,
                   blocked_until  = MAX(blocked_until, COALESCE(?, 0)),
                   last_error     = COALESCE(?, last_error),
                   observed_limit = CASE
                       WHEN ? IS NULL THEN observed_limit
                       WHEN observed_limit IS NULL THEN ?
                       ELSE MIN(observed_limit, ?)
                   END,
                   observed_at    = CASE
                       WHEN ? IS NULL THEN observed_at
                       ELSE ?
                   END
             WHERE day=? AND provider=?
            """,
            (int(tokens or 0), float(cost_usd or 0.0),
             0 if ok else 1, 1 if rate_limited else 0,
             blocked_until, error,
             observed_limit, observed_limit, observed_limit,
             observed_limit, float(observed_at or 0.0), day, provider),
        )
        conn.commit()


def clear_observed_limit(db_path: Path, day: str, provider: str) -> None:
    """Forget a learned daily ceiling, because a call just succeeded past it.

    The counterpart to the MIN in `record_provider_call`. Lowering is cautious
    by design — an unproven higher number must never talk down a proven one —
    but that made the ceiling a one-way door, and a wrong one cost a provider
    its whole remaining day. A *success* above the ceiling is not an optimistic
    guess; it is the provider itself demonstrating the number was wrong, and it
    is the only thing allowed to raise it.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE provider_usage SET observed_limit=NULL, observed_at=0 "
            "WHERE day=? AND provider=?",
            (day, provider),
        )
        conn.commit()


# ── Extraction quarantine ─────────────────────────────────────────────────────

def bump_extract_failure(
    db_path: Path, url_hash: str, fingerprint: str, *,
    url: str | None = None, error: str | None = None,
) -> int:
    """Record one *content* failure for a page and return the new count.

    Only failures where a model answered and the answer was unusable belong
    here — a truncated or malformed response. An outage, a 429 or a spent quota
    says nothing about the page and must never reach this function, or a bad
    afternoon would quarantine the corpus.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO extract_failures
                   (url_hash, fingerprint, url, fail_count, last_error, first_at, last_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(url_hash, fingerprint) DO UPDATE SET
                   fail_count = fail_count + 1,
                   last_error = excluded.last_error,
                   last_at    = excluded.last_at,
                   url        = COALESCE(extract_failures.url, excluded.url)
            """,
            (url_hash, fingerprint, url, (error or "")[:300], now, now),
        )
        row = conn.execute(
            "SELECT fail_count FROM extract_failures WHERE url_hash=? AND fingerprint=?",
            (url_hash, fingerprint),
        ).fetchone()
        conn.commit()
    return int(row[0]) if row else 1


def clear_extract_failure(db_path: Path, url_hash: str,
                          fingerprint: str | None = None) -> None:
    """Forget a page's failures — it just extracted, or an admin reset it.

    `fingerprint=None` clears every fingerprint for the page, which is what the
    admin "try this page again" button wants.
    """
    with _connect(db_path) as conn:
        if fingerprint is None:
            conn.execute("DELETE FROM extract_failures WHERE url_hash=?", (url_hash,))
        else:
            conn.execute(
                "DELETE FROM extract_failures WHERE url_hash=? AND fingerprint=?",
                (url_hash, fingerprint),
            )
        conn.commit()


def get_extract_failure_counts(db_path: Path, fingerprint: str) -> dict[str, int]:
    """{url_hash: failures} at this fingerprint — every page, not just the
    quarantined ones.

    One query per run, not one per page. The counts below the threshold are
    worth carrying too: they are what tells a page that has just succeeded that
    it has a row to clear, so a success costs a write only for pages that
    actually failed before.

    Bounded by how many distinct pages fail deterministically at one
    fingerprint — a few hundred in production. If it ever grows past that, the
    prompt or the token cap is wrong for the whole corpus, which is a louder
    problem than this dictionary.
    """
    if not db_path.exists():
        return {}
    with _connect(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT url_hash, fail_count FROM extract_failures WHERE fingerprint=?",
                (fingerprint,),
            ).fetchall()
        except sqlite3.OperationalError:
            # Older database, table not created yet. An unreadable quarantine
            # means "nothing is quarantined", never an aborted run.
            return {}
    return {r[0]: int(r[1] or 0) for r in rows}


def count_quarantined_pages(db_path: Path, fingerprint: str, threshold: int) -> int:
    """How many pages are currently in quarantine at this fingerprint."""
    if threshold <= 0 or not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM extract_failures"
                " WHERE fingerprint=? AND fail_count>=?",
                (fingerprint, int(threshold)),
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
    return int(row[0]) if row else 0


def get_extract_failures(db_path: Path, fingerprint: str | None = None,
                         min_count: int = 1, limit: int = 200) -> list[dict]:
    """Quarantined pages for the admin page, worst first."""
    if not db_path.exists():
        return []
    sql = ("SELECT url_hash, fingerprint, url, fail_count, last_error, first_at, last_at"
           " FROM extract_failures WHERE fail_count>=?")
    params: list = [int(min_count)]
    if fingerprint:
        sql += " AND fingerprint=?"
        params.append(fingerprint)
    sql += " ORDER BY fail_count DESC, last_at DESC LIMIT ?"
    params.append(int(limit))
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


def get_extraction_quality_mix(db_path: Path, limit: int = 15) -> list[dict]:
    """Cached pages grouped by the model that extracted them, largest first.

    The honest answer to "how good is the corpus actually" — and the input the
    router's upgrade sweep works from. A NULL model/quality means the page was
    extracted before the router existed.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT extract_model, extract_quality, COUNT(*) AS pages
              FROM cache_pages
             WHERE extracted_at IS NOT NULL
             GROUP BY extract_model, extract_quality
             ORDER BY pages DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [{"model": m, "quality": q, "pages": n} for m, q, n in rows]


def get_sitemap_communities(db_path: Path) -> dict[tuple[str, str], list[dict]]:
    """{(city, topic): [{name, thin}]} for every visible community, in one query.

    The sitemap used to call `get_communities(city, topic)` inside a loop over
    every city×topic pair. With 3.8K cities that is thousands of separate
    connections and JSON decodes on the event loop — measured at >30s on
    2026-08-16, which blocked every other request behind it.

    Only the two fields the sitemap needs are decoded: the name (for the slug)
    and whether the page is thin (no description of either kind), which decides
    whether it belongs in the sitemap at all.
    """
    out: dict[tuple[str, str], list[dict]] = {}
    if not db_path.exists():
        return out
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT city, topic,
                   json_extract(data, '$.name'),
                   COALESCE(NULLIF(TRIM(COALESCE(json_extract(data, '$.description'), '')), ''),
                            NULLIF(TRIM(COALESCE(json_extract(data, '$.long_description'), '')), ''))
              FROM communities
             WHERE hidden=0
             ORDER BY city, topic, id
            """
        ).fetchall()
    for city, topic, name, described in rows:
        if not name:
            continue
        out.setdefault((city or "", topic or ""), []).append(
            {"name": name, "thin": not described})
    return out


def get_funnel_counts(db_path: Path, days: int = 30) -> dict:
    """The acquisition funnel, end to end, in one call.

    Every stage of it was already being recorded — pageviews, outclicks,
    subscriptions, claims, submissions — and none of it was readable without
    the admin password, so "is anything converting?" had no answer and the
    honest one was a guess. A funnel nobody can see is a funnel nobody tunes.

    `days` bounds the recent columns and counts calendar buckets **including
    today**, so days=1 is today alone.

    Two of the `_total` columns are standing rows, not lifetime events, and the
    names cannot be made honest without history tables nobody has asked for:
    the subscription counts drop a person who unsubscribes (the row is deleted
    by design — consent withdrawn means the address is erased), and
    `reports_total` counts only reports an admin has not yet handled. Read
    those as "standing", not "ever".
    """
    empty = {
        "visitors": 0, "pageviews": 0, "outclicks": 0, "outclicks_total": 0,
        "subscriptions": 0, "subscriptions_total": 0, "subscribers_total": 0,
        "claims": 0, "claims_total": 0, "submissions": 0, "submissions_total": 0,
        "edit_requests": 0, "edit_requests_total": 0,
        "reports": 0, "reports_total": 0,
        "records": 0, "records_with_email": 0, "records_with_website": 0,
        "records_email_distinct": 0,
        "persons": 0, "persons_with_email": 0, "persons_email_distinct": 0,
        "days": days,
    }
    if not db_path.exists():
        return empty
    out = dict(empty)

    # Two timestamp formats share this schema: Python writes ISO-8601 with a
    # "T" and an offset, SQLite's own datetime('now') writes a space and none.
    # Comparing them as text silently widens the window — "T" sorts above " ",
    # so `'2026-07-24T00:00:01+00:00' >= '2026-07-24 15:34:25'` is true and a
    # row fifteen hours outside the window counts as inside it. Both sides are
    # normalised to `YYYY-MM-DD HH:MM:SS` here, and the cutoff is computed in
    # Python so there is exactly one definition of it.
    from datetime import timedelta as _td
    now = datetime.now(timezone.utc)
    cutoff_ts = (now - _td(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    # `days` calendar buckets including today, not days+1: date('now','-30 days')
    # is inclusive at both ends.
    cutoff_day = (now - _td(days=int(days) - 1)).strftime("%Y-%m-%d")

    def _one(sql: str, *params) -> int:
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError:
            # A table added after this database was created. Report zero rather
            # than failing the whole funnel over one missing column.
            return 0
        return int(row[0] or 0) if row else 0

    def _since(table: str, col: str) -> str:
        return (f"SELECT COUNT(*) FROM {table}"
                f" WHERE substr(replace({col},'T',' '),1,19) >= ?")

    with _connect(db_path) as conn:
        out["pageviews"] = _one(
            "SELECT SUM(pageviews) FROM traffic_daily WHERE day >= ?", cutoff_day)
        out["visitors"] = _one(
            "SELECT COUNT(*) FROM traffic_visitors WHERE day >= ?", cutoff_day)
        out["outclicks"] = _one(_since("outclick_events", "clicked_at"), cutoff_ts)
        out["outclicks_total"] = _one("SELECT COUNT(*) FROM outclick_events")
        out["subscriptions"] = _one(_since("subscriptions", "created_at"), cutoff_ts)
        out["subscriptions_total"] = _one("SELECT COUNT(*) FROM subscriptions")
        # One person subscribing to four topics is one subscriber, four rows —
        # and it is the person a mail would go to, so count them separately.
        out["subscribers_total"] = _one("SELECT COUNT(DISTINCT email) FROM subscriptions")
        # A claim is not a correction; counting them together hides both.
        out["claims"] = _one(
            _since("edit_requests", "submitted_at") + " AND change_type='claim'", cutoff_ts)
        out["claims_total"] = _one(
            "SELECT COUNT(*) FROM edit_requests WHERE change_type='claim'")
        out["edit_requests"] = _one(
            _since("edit_requests", "submitted_at") + " AND change_type<>'claim'", cutoff_ts)
        out["edit_requests_total"] = _one(
            "SELECT COUNT(*) FROM edit_requests WHERE change_type<>'claim'")
        out["submissions"] = _one(_since("community_submissions", "submitted_at"), cutoff_ts)
        out["submissions_total"] = _one("SELECT COUNT(*) FROM community_submissions")
        out["reports"] = _one(_since("not_community_reports", "reported_at"), cutoff_ts)
        out["reports_total"] = _one("SELECT COUNT(*) FROM not_community_reports")
        out["records"] = _one("SELECT COUNT(*) FROM communities")
        if _has_json1(conn):
            # Contactability of the corpus. Not a licence to mail any of it —
            # Hungary's Advertising Act (2008. XLVIII. §6) needs prior express
            # consent for advertising email to a natural person, with no
            # legitimate-interest escape. This sizes what an opt-in channel
            # could reach; it is not a send list.
            out["records_with_email"] = _one(
                "SELECT COUNT(*) FROM communities"
                " WHERE COALESCE(json_extract(data,'$.email'),'') <> ''")
            out["records_with_website"] = _one(
                "SELECT COUNT(*) FROM communities"
                " WHERE COALESCE(json_extract(data,'$.website'),'') <> ''")
            # Rows and addresses are different questions. One address can sit on
            # a club's page and on its leader's, and the same office address
            # serves every group in a community centre — so the row count sizes
            # the corpus and the distinct count sizes the audience. Asked for on
            # 2026-09-17 and the row count alone could not answer it.
            out["records_email_distinct"] = _one(
                "SELECT COUNT(DISTINCT lower(trim(json_extract(data,'$.email'))))"
                " FROM communities"
                " WHERE COALESCE(json_extract(data,'$.email'),'') <> ''")
            # Persons are the other half of contactability: a named leader with
            # an address is who a claim or a correction actually reaches.
            out["persons"] = _one("SELECT COUNT(*) FROM persons")
            out["persons_with_email"] = _one(
                "SELECT COUNT(*) FROM persons"
                " WHERE COALESCE(json_extract(data,'$.email'),'') <> ''")
            out["persons_email_distinct"] = _one(
                "SELECT COUNT(DISTINCT lower(trim(json_extract(data,'$.email'))))"
                " FROM persons"
                " WHERE COALESCE(json_extract(data,'$.email'),'') <> ''")
    return out
