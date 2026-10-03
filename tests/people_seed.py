"""List the group each stored person leads, so the person can be published.

A person page is published only when its group is listed in the same town
(`db.get_publishable_persons`). Tests about rendering people seed persons on
their own; this adds the communities they claim to lead.
"""
import json
import sqlite3
from pathlib import Path

from scraper.models import CommunityRecord
from scraper.store import save_results


def seed_groups(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        rows = [json.loads(r[0]) for r in conn.execute("SELECT data FROM persons")]
    for p in rows:
        save_results(p["city"], p["topic"], [CommunityRecord(
            name=p["community_name"], topic=p["topic"], city=p["city"], locale="hu",
            source_url=p["source_url"], extracted_at=p["extracted_at"],
        )], db)
