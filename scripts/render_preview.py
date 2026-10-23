#!/usr/bin/env python3
"""Render public pages to static HTML files for a design review, without a server.

There is no local dev environment (CLAUDE.md): production is the only running
app. This builds a throwaway database with a handful of sample communities,
renders the requested paths through FastAPI's TestClient for one host, and
writes each page to `<out>/<name>.html` to open in a browser. Tailwind (CDN),
the fonts and the icons load from the network as in production.

    python scripts/render_preview.py --out /tmp/preview
    python scripts/render_preview.py --out /tmp/preview --host meetapedia.com / /about
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = [
    ("Pécs", "choir", "Mecsek Kórus", "A Mecsek Kórus Pécs város vegyeskara, hetente kétszer próbál."),
    ("Pécs", "choir", "Pécsi Kamarakórus", "1958-ban alapított pécsi kamarakórus."),
    ("Pécs", "running", "FutaPécs", "Pécsi futóközösség, közös futásokkal csütörtök esténként."),
    ("Pécs", "senior", "Mindenki Nyugdíjasklub", "Heti klubdélután teával, kirándulásokkal."),
    ("Pécs", "board_games", "Játszó-Tér Egyesület", "Társasjáték-estek a Király utcában."),
    ("Budapest", "dance", "Budai Táncklub", "Társastánc kezdőknek és haladóknak."),
    ("Budapest", "volunteering", "Önkéntes Kör", "Havi önkéntes akciók a kerületben."),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*", default=["/", "/rolunk", "/pecs", "/pecs/mecsek-korus"])
    ap.add_argument("--host", default="kozossegek.com")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from fastapi.testclient import TestClient

    from scraper.db import init_db
    from scraper.models import CommunityRecord
    from scraper.pipeline import CityConfig
    from scraper.store import save_results
    from scraper.web import app as web_app
    from scraper.web.state import app_state

    db = Path(tempfile.mkdtemp()) / "scraper.db"
    init_db(db)
    for city, topic, name, desc in SAMPLES:
        save_results(city, topic, [CommunityRecord(
            name=name, topic=topic, city=city, locale="hu",
            source_url=f"https://{topic}.example/{name.split()[0].lower()}",
            extracted_at="2026-10-01T00:00:00+00:00", description=desc)], db)
    app_state.db_path = db
    app_state.cities = [
        CityConfig(name=c, country="Hungary", locale="hu", search_variants=[c])
        for c in ("Budapest", "Pécs", "Debrecen", "Szeged", "Győr")]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = TestClient(web_app.app)
    for path in args.paths:
        r = client.get(path, headers={"host": args.host}, follow_redirects=True)
        # Root-relative assets must resolve against the live site from file://.
        html = re.sub(r'(href|src)="/(?!/)', rf'\1="https://{args.host}/', r.text)
        name = path.strip("/").replace("/", "_") or "home"
        (out / f"{name}.html").write_text(html, encoding="utf-8")
        print(f"{r.status_code} {path} -> {out / (name + '.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
