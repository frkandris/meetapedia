from pathlib import Path

from scraper.db import get_communities, init_db
from scraper.models import CommunityRecord
from scraper.store import save_results


def make_record(name: str, website: str, source_url: str) -> CommunityRecord:
    return CommunityRecord(
        name=name,
        topic="running",
        city="Budapest",
        locale="hu",
        website=website,
        source_url=source_url,
        extracted_at="2026-01-01T00:00:00+00:00",
    )


def test_save_results_deduplicates_by_website(tmp_path: Path):
    db_path = tmp_path / "scraper.db"
    init_db(db_path)

    save_results(
        "Budapest",
        "running",
        [
            make_record("Budapest Runners", "https://example.com", "https://source-a.test"),
            make_record("Budapest Running Club", "https://example.com/", "https://source-b.test"),
        ],
        db_path,
    )

    records = get_communities(db_path, "Budapest", "running")
    assert len(records) == 1


def test_a_hidden_community_survives_a_save_that_does_not_mention_it(tmp_path):
    """`save_results` builds its batch from the *visible* rows, and the delete
    before the reinsert used to remove hidden ones too: every moderated
    community not in the batch was erased, and came back visible when a later
    page mentioned it again (review, 2026-09-24).
    """
    import sqlite3

    from scraper.db import _community_record_key, init_db, set_community_hidden
    from scraper.models import CommunityRecord
    from scraper.store import save_results

    db = tmp_path / "hidden.db"
    init_db(db)

    def rec(name):
        return CommunityRecord(name=name, city="Pécs", topic="running", locale="hu",
                               source_url=f"https://x.test/{name}",
                               extracted_at="2026-09-24T00:00:00Z")

    save_results("Pécs", "running", [rec("Mecsek Futók"), rec("Tettye Kocogók")], db)
    hidden_key = _community_record_key("Tettye Kocogók", "Pécs", "running")
    set_community_hidden(db, hidden_key, True)

    save_results("Pécs", "running", [rec("Mecsek Futók")], db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT hidden FROM communities WHERE record_key=?",
                            (hidden_key,)).fetchone() == (1,)

    # Mentioned again later: updated, still hidden.
    save_results("Pécs", "running", [rec("Tettye Kocogók")], db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT hidden FROM communities WHERE record_key=?",
                            (hidden_key,)).fetchone() == (1,)


_REQUIRED = {"source_url": "https://x.test/", "extracted_at": "2026-09-24T00:00:00Z"}


def test_placeholder_values_are_not_published():
    from scraper.models import CommunityRecord

    r = CommunityRecord(name="A", city="Pécs", topic="running", locale="hu", **_REQUIRED,
                        website="N/A", member_count="unknown", email="nincs megadva")
    assert (r.website, r.member_count, r.email) == (None, None, None)
    assert CommunityRecord(name="A", city="Bern", topic="x", locale="de", **_REQUIRED,
                           website="Lerne Deutsch in Bern").website is None
    assert CommunityRecord(name="A", city="Pécs", topic="x", locale="hu", **_REQUIRED,
                           website="pecsifutok.hu/klub").website == "https://pecsifutok.hu/klub"
