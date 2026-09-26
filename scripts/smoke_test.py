#!/usr/bin/env python3
"""Post-deploy smoke test — checks the site the way a visitor sees it.

Why an external check
---------------------
On 2026-08-16 a container restart left the app reporting `running:healthy` in
Coolify while every request from outside returned 404: the Traefik route had not
been re-registered. The Docker healthcheck could not see it, because it calls
`localhost:8000/healthz` from *inside* the container — where everything was
genuinely fine.

So this deliberately goes through the public hostname, over the CDN, exactly
like a browser. Anything that only proves the process is alive would have passed
during that outage.

Usage
-----
    PYTHONPATH=. .venv/bin/python scripts/smoke_test.py
    PYTHONPATH=. .venv/bin/python scripts/smoke_test.py --wait 300   # after a deploy
    PYTHONPATH=. .venv/bin/python scripts/smoke_test.py --expect-version v.2026-08-16.22:43

Exit code 1 if any check fails, so it can gate a deploy or drive an alert.
"""
from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.error
import urllib.request

# A slow first byte is a real symptom: the app shares one event loop with the
# pipeline, and a blocked loop shows up here as seconds, not as an error.
SLOW_SECONDS = 5.0
TIMEOUT = 30
_UA = "meetapedia-smoke/1.0"


def fetch(url: str, headers: dict | None = None) -> tuple[int, str, float]:
    """(status, body, seconds). HTTP errors are results, not exceptions —
    a 401 is the expected answer on the admin route."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.monotonic() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000).decode("utf-8", "replace"), time.monotonic() - t0
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}", time.monotonic() - t0


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def check(self, name: str, url: str, *, status: int = 200,
              must_contain: str = "", headers: dict | None = None) -> str:
        code, body, secs = fetch(url, headers)
        ok = code == status and (not must_contain or must_contain in body)
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {name:34} {code} in {secs:5.2f}s")
        if not ok:
            detail = (f"expected {status}, got {code}" if code != status
                      else f"missing {must_contain!r}")
            self.failures.append(f"{name}: {detail}")
            if code == 0:
                print(f"       {body[:140]}")
        elif secs > SLOW_SECONDS:
            # Not a failure — the site works — but a blocked event loop is worth
            # surfacing before it becomes one.
            self.warnings.append(f"{name}: slow ({secs:.1f}s)")
        return body


def run(base_hu: str, base_intl: str, expect_version: str = "") -> Checks:
    c = Checks()
    print("Public reachability (through the CDN, as a visitor sees it):")

    health = c.check("healthz", f"{base_hu}/healthz", must_contain='"ok":true')
    version = ""
    try:
        version = json.loads(health).get("version", "")
    except Exception:
        pass

    # A homepage that renders but lists nothing is a working app with a broken
    # database, which the health endpoint alone would not catch.
    c.check("kozossegek home", f"{base_hu}/", must_contain="</html>")
    c.check("kozossegek cities", f"{base_hu}/varosok", must_contain="data-name=")
    c.check("kozossegek search widget", f"{base_hu}/", must_contain="/static/js/listing.js")
    c.check("meetapedia home", f"{base_intl}/", must_contain="</html>")
    # An index of parts above 45,000 URLs (both sites are), a plain urlset
    # below it; either way part 1 is a urlset.
    c.check("sitemap", f"{base_hu}/sitemap.xml", must_contain="sitemaps.org/schemas/sitemap")
    c.check("sitemap part", f"{base_hu}/sitemap-1.xml", must_contain="<urlset")
    c.check("static asset", f"{base_hu}/static/js/listing.js", must_contain="MpAutocomplete")

    print("Auth boundaries (these MUST refuse):")
    c.check("admin requires auth", f"{base_hu}/admin", status=401)
    c.check("gateway requires auth", f"{base_hu}/v1/models", status=401)
    # The operator endpoints start and stop pipeline runs. An unauthenticated
    # one would be worse than an open gateway, so it is checked by name.
    c.check("control requires auth", f"{base_hu}/v1/control/status", status=401)

    if version:
        print(f"\ndeployed version: {version}")
    if expect_version and version != expect_version:
        c.failures.append(f"version is {version!r}, expected {expect_version!r}")
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hu", default="https://kozossegek.com")
    ap.add_argument("--intl", default="https://meetapedia.com")
    ap.add_argument("--expect-version", default="",
                    help="fail unless /healthz reports this build")
    ap.add_argument("--wait", type=int, default=0,
                    help="seconds to keep retrying while the site is down (post-deploy)")
    args = ap.parse_args()

    deadline = time.monotonic() + args.wait
    while True:
        c = run(args.hu, args.intl, args.expect_version)
        if not c.failures or time.monotonic() >= deadline:
            break
        print(f"\n… {len(c.failures)} failing, retrying (waiting up to "
              f"{int(deadline - time.monotonic())}s more)\n")
        time.sleep(15)

    if c.warnings:
        print("\nWarnings:")
        for w in c.warnings:
            print(f"  - {w}")
        print("  A slow response usually means the pipeline is blocking the "
              "event loop, not that the site is down.")
    if c.failures:
        print(f"\nFAILED ({len(c.failures)}):")
        for f in c.failures:
            print(f"  - {f}")
        print("\nIf everything 404s while Coolify says running:healthy, the "
              "Traefik route was not re-registered — redeploy (not restart).")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
