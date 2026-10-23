"""Find community records filed under a topic they do not belong to. Dry run.

Written for `nok` (women's groups): until 2026-10-23 the extraction prompt sent
the topic slug, so the model read "Extract all nok community groups" and filed
whole city-wide NGO lists under it (pecs.hu/civil-szervezetek: polgárőrök,
sport federations, a diabetes society…). The prompt is fixed for new
extractions; this measures and, on request, cleans what is already stored.

A record is a suspect when neither its name nor its tags carry a marker of the
topic (the description cannot be trusted — the model wrote "női" into it). Each
suspect is then one of:

  duplicate — the same community (name, city) is also listed under another
              topic, so hiding this copy loses nothing;
  only-here — this is its only listing; hiding would remove a real
              organisation from the site, so it is reported for re-filing.

    python3 scripts/audit_topic_misfits.py --db /app/data/scraper.db --topic nok
    python3 scripts/audit_topic_misfits.py --db … --topic nok --csv misfits.csv
    python3 scripts/audit_topic_misfits.py --db … --topic nok --hide-duplicates   # writes

Only `--hide-duplicates` writes, and only `hidden = 1` on duplicate suspects.
In production it runs inside the container over stdin (`python3 - …`).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter

MARKERS = {
    "nok": (r"(\bnő|női|nők|anya|anyák|lány|asszony|hölgy|feminin|feminist|wom[ae]n|ladies|"
            r"lady|girl|mother|mum|mom|mama|frau|frauen|mütter|mädchen|femme|mère|mujer|madre|"
            r"donne|kobiet|kvinn|nais|ženy|žen|female|sisters?|schwester)"),
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--topic", default="nok", choices=sorted(MARKERS))
    ap.add_argument("--csv", help="write every suspect here")
    ap.add_argument("--hide-duplicates", action="store_true",
                    help="set hidden=1 on suspects listed under another topic too")
    args = ap.parse_args()

    marker = re.compile(MARKERS[args.topic], re.I)
    uri = f"file:{args.db}" + ("" if args.hide_duplicates else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    elsewhere = {(_norm(json.loads(d).get("name", "")), city)
                 for city, d in conn.execute(
                     "SELECT city, data FROM communities WHERE topic != ? "
                     "AND COALESCE(hidden, 0) = 0", (args.topic,))}

    rows, kinds, sources = [], Counter(), Counter()
    total = 0
    for rid, city, data in conn.execute(
            "SELECT id, city, data FROM communities WHERE topic = ? "
            "AND COALESCE(hidden, 0) = 0", (args.topic,)):
        total += 1
        j = json.loads(data)
        name = j.get("name", "")
        if marker.search(name) or marker.search(" ".join(j.get("tags") or [])):
            continue
        kind = "duplicate" if (_norm(name), city) in elsewhere else "only-here"
        kinds[kind] += 1
        sources[(j.get("source_url") or "")[:80]] += 1
        rows.append({"id": rid, "kind": kind, "city": city, "name": name,
                     "source_url": j.get("source_url", ""),
                     "description": (j.get("description") or "")[:160]})

    print(f"{args.topic}: {total} visible, {len(rows)} suspects "
          f"({kinds['duplicate']} duplicate, {kinds['only-here']} only-here)")
    print("top sources of suspects:")
    for src, n in sources.most_common(10):
        print(f"  {n:4}  {src}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["id"])
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {args.csv}")
    elif not args.hide_duplicates:
        w = csv.DictWriter(sys.stdout, fieldnames=["kind", "city", "name"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows[:15])

    if args.hide_duplicates:
        ids = [r["id"] for r in rows if r["kind"] == "duplicate"]
        conn.executemany("UPDATE communities SET hidden = 1 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        print(f"hid {len(ids)} duplicate suspects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
