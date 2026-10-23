#!/usr/bin/env python3
"""Search Console snapshot for both sites: totals, trend, top queries/pages,
sitemaps, and URL Inspection on a sample. Read-only.

Repeats the measurement behind docs/wiki/pages/seo/search-console-2026-10-03.md
so the next one is a command, not an afternoon:

    .venv/bin/python scripts/gsc_audit.py --key ~/.config/gsc/meetapedia-gsc-reader.json
    .venv/bin/python scripts/gsc_audit.py --key KEY --inspect 30 --json out.json

The service account (see the session memory) is a Full user on both domain
properties. URL Inspection is 2,000/day per property; the default sample is small.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

API = "https://searchconsole.googleapis.com/webmasters/v3"
INSPECT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
SITES = {"kozossegek.com": "sc-domain:kozossegek.com", "meetapedia.com": "sc-domain:meetapedia.com"}


def session(key: str):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        str(Path(key).expanduser()), scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    return AuthorizedSession(creds)


def query(s, prop: str, start: str, end: str, dims: list[str], limit: int = 25) -> list[dict]:
    r = s.post(f"{API}/sites/{urllib.parse.quote(prop, safe='')}/searchAnalytics/query",
               json={"startDate": start, "endDate": end, "dimensions": dims,
                     "rowLimit": limit, "dataState": "all"})
    r.raise_for_status()
    return r.json().get("rows", [])


def sitemap_urls(host: str, n: int) -> list[str]:
    import httpx
    urls: list[str] = []
    body = httpx.get(f"https://{host}/sitemap.xml", timeout=60).text
    parts = re.findall(r"<loc>([^<]+sitemap-\d+\.xml)</loc>", body)
    for part in parts or [None]:
        text = httpx.get(part, timeout=60).text if part else body
        urls += re.findall(r"<loc>([^<]+)</loc>", text)
    random.Random(42).shuffle(urls)
    guides = [u for u in urls if "/utmutatok/" in u or "/guides/" in u][: max(1, n // 4)]
    return guides + [u for u in urls if u not in guides][: n - len(guides)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--inspect", type=int, default=20, help="URLs per site to inspect")
    ap.add_argument("--json", help="write everything here too")
    args = ap.parse_args()
    s = session(args.key)
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=args.days - 1)
    prev_end, prev_start = start - timedelta(days=1), start - timedelta(days=args.days)
    out: dict = {"window": [str(start), str(end)]}
    for host, prop in SITES.items():
        site: dict = {}
        tot = query(s, prop, str(start), str(end), [], 1)
        prev = query(s, prop, str(prev_start), str(prev_end), [], 1)
        site["totals"] = tot[0] if tot else {}
        site["previous"] = prev[0] if prev else {}
        site["weeks"] = query(s, prop, str(start), str(end), ["date"], 100)
        site["queries"] = query(s, prop, str(start), str(end), ["query"], 25)
        site["pages"] = query(s, prop, str(start), str(end), ["page"], 25)
        site["countries"] = query(s, prop, str(start), str(end), ["country"], 8)
        r = s.get(f"{API}/sites/{urllib.parse.quote(prop, safe='')}/sitemaps")
        site["sitemaps"] = r.json().get("sitemap", []) if r.ok else r.text[:200]
        verdicts: dict = {}
        if args.inspect:
            for url in sitemap_urls(host, args.inspect):
                r = s.post(INSPECT, json={"inspectionUrl": url, "siteUrl": prop})
                res = (r.json().get("inspectionResult") or {}).get("indexStatusResult", {}) if r.ok else {}
                verdicts.setdefault(res.get("coverageState", f"error {r.status_code}"), []).append(url)
        site["inspection"] = verdicts
        out[host] = site

        t, p = site["totals"], site["previous"]
        print(f"\n== {host}  {start}..{end}")
        print(f"  clicks {t.get('clicks', 0):.0f} (prev {p.get('clicks', 0):.0f})  "
              f"impressions {t.get('impressions', 0):.0f} (prev {p.get('impressions', 0):.0f})  "
              f"avg pos {t.get('position', 0):.1f}")
        print("  top queries:", "; ".join(f"{r['keys'][0]} ({r['clicks']:.0f}/{r['impressions']:.0f}, "
                                          f"pos {r['position']:.0f})" for r in site["queries"][:10]))
        print("  top pages:", "; ".join(f"{r['keys'][0].split('//')[1]} ({r['clicks']:.0f}/{r['impressions']:.0f})"
                                        for r in site["pages"][:8]))
        print("  countries:", ", ".join(f"{r['keys'][0]} {r['clicks']:.0f}/{r['impressions']:.0f}"
                                        for r in site["countries"]))
        for sm in site["sitemaps"] if isinstance(site["sitemaps"], list) else []:
            print(f"  sitemap {sm.get('path')} downloaded {sm.get('lastDownloaded')} "
                  f"submitted {sum(int(c.get('submitted', 0)) for c in sm.get('contents', []))} "
                  f"errors {sm.get('errors')} warnings {sm.get('warnings')}")
        for state, urls in sorted(verdicts.items(), key=lambda x: -len(x[1])):
            print(f"  {len(urls):>3}  {state}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
