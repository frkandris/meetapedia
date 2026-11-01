#!/usr/bin/env python3
"""Break a traffic spike down by source, country, landing page and device.

The daily report counts visitors per site; this answers *who* they were —
the question a spike raises. Read-only; uses the same GA4 credentials as
`scraper.report.fetch_ga4_traffic`, so it runs inside the container:

    ssh meetapedia-prod "docker exec -i -w /app -e PYTHONPATH=/app <container> \\
        python3 - --start 2026-10-25 --end 2026-10-31" < scripts/ga4_breakdown.py

Each table prints the top rows for one dimension with users, sessions, views,
engaged sessions and average engagement seconds per session — bot traffic
shows as one view per user and near-zero engagement.
"""
from __future__ import annotations

import argparse
import json
import os

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

DIMENSIONS = ["date", "hostName", "sessionSourceMedium", "country", "city",
              "landingPage", "deviceCategory", "browser", "operatingSystem"]
METRICS = ["activeUsers", "sessions", "screenPageViews", "engagedSessions",
           "userEngagementDuration"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--host", default="", help="substring filter on hostName")
    args = ap.parse_args()

    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GA4_CREDENTIALS_JSON"]),
        scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    creds.refresh(Request())
    url = (f"https://analyticsdata.googleapis.com/v1beta/properties/"
           f"{os.environ['GA4_PROPERTY_ID']}:runReport")

    for dim in DIMENSIONS:
        body = {"dateRanges": [{"startDate": args.start, "endDate": args.end}],
                "dimensions": [{"name": dim}], "metrics": [{"name": m} for m in METRICS],
                "orderBys": [{"metric": {"metricName": "activeUsers"}, "desc": True}],
                "limit": 40 if dim == "date" else args.top}
        if args.host:
            body["dimensionFilter"] = {"filter": {"fieldName": "hostName", "stringFilter": {
                "matchType": "CONTAINS", "value": args.host}}}
        if dim == "date":
            body["orderBys"] = [{"dimension": {"dimensionName": "date"}}]
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {creds.token}"},
                       timeout=60)
        r.raise_for_status()
        print(f"\n== {dim}  (users sess views engaged avg_eng_s)")
        for row in r.json().get("rows", []):
            u, s, v, e, d = (float(x["value"]) for x in row["metricValues"])
            print(f"  {row['dimensionValues'][0]['value'][:60]:60} {u:6.0f} {s:6.0f} "
                  f"{v:6.0f} {e:6.0f} {d / max(s, 1):6.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
