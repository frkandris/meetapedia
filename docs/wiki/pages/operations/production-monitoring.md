---
type: Runbook
title: Production Monitoring
description: What each health signal actually measures, why every one of them missed a full outage, and the external smoke test that did not.
tags: [operations, monitoring, healthcheck, smoke-test, coolify]
timestamp: 2026-08-16
resource: scripts/smoke_test.py
---

# Production Monitoring

*Four "the site is down" reports in one evening, none of them caught by monitoring, all of them with a perfectly healthy process.*

## The signals, and what each one is blind to

| Signal | Answers | Blind to |
|---|---|---|
| Docker `HEALTHCHECK` | is the process serving `localhost:8000`? | anything between the container and the user |
| Coolify `status` | is the container running and passing its check? | reachability; it reports the container, not the site |
| Application logs | what did the code do? | outages the code is not involved in |
| **`scripts/smoke_test.py`** | can a visitor load the site? | *(this is the one that catches the rest)* |

The first three all answer "is the process alive". On 2026-08-16 the process was
alive through every episode, and the site was still unreachable
([[2026-08-healthz-db-query-outage]]).

## After every deploy

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_test.py --wait 420
```

Goes through the public hostnames over the CDN, checks both sites, the city
list, the sitemap and a static asset, and asserts that `/admin` and `/v1` still
**refuse** unauthenticated requests. Exit code 1 on any failure, so it can gate a
deploy.

It also warns on responses slower than 5s. That is not cosmetic: its first run
surfaced a 30-second sitemap that had been degrading every request for hours,
and which nothing else was measuring.

## /healthz is a liveness probe

It returns 200 whenever the app is serving, and reports `db: "busy"` rather than
failing when the database is slow. **Do not add a query to it.** The endpoint
previously ran a `COUNT(*)`, and a write lock in the pipeline was enough to fail
the healthcheck, mark the container unhealthy and have Traefik remove it from
rotation — turning a slow database into a total outage.

If you need a readiness signal (is the data usable), add a separate endpoint.
Conflating the two is what caused the incident.

## When the site is unreachable but Coolify says healthy

1. Confirm from outside: `curl -o /dev/null -w '%{http_code}' https://kozossegek.com/healthz`
2. Check the container's own view: `GET /api/v1/applications/{uuid}/logs` — a
   clean log with no traffic means the route, not the app.
3. Restore routing: `POST /api/v1/deploy?uuid=…&force=true` (Coolify API, Bearer
   token). Prefer it over `restart`, which does not reliably re-register the
   route.

**First rule out the deploy window.** A bare `404 page not found` with
content-type `text/plain` is Traefik saying it has no route for the host — and
**every deploy produces exactly that for about four minutes**, measured on
2026-09-17: the push of `d2efea8` served 200 while Coolify still reported
`in_progress`, then 404 from 19:11:06 to somewhere before 19:14:57 UTC, then 200
again with nobody touching it. That is what `smoke_test.py --wait 420` is
waiting for. So the diagnosis in this section applies to a 404 that is *still*
there five minutes after the last deploy finished; inside that window it is the
swap, and a forced deploy on top of one in flight only starts the window over.
Confirm the timing first: `GET /api/v1/deployments` shows anything in flight and
`GET /api/v1/deployments/applications/{uuid}` the finish times. Container logs come from
   `GET /api/v1/applications/{uuid}/logs?lines=N`; there is no exec endpoint in
   the v1 API.
4. Verify with the smoke test before declaring it fixed.

## It runs automatically

`.github/workflows/smoke.yml` runs the smoke test on every push to `main` and
**every 15 minutes** on a schedule. GitHub emails on failure.

Both triggers are needed, because the site failed in two different ways:

- after a push, a deploy left it unreachable;
- with **no deploy at all**, a database lock failed the healthcheck and Traefik
  pulled the container for minutes at a time.

A deploy-only check would have caught half the incidents. Fifteen minutes is
chosen against the observed outage length — the episodes lasted minutes, so an
hourly check would mostly have reported "fine" after the fact.

Running it in GitHub Actions rather than on the server is the point: the checker
must not share fate with the thing it checks. It uses only the Python standard
library, so the job cannot fail for reasons unrelated to the site.

## Why this is an SEO problem too

An outage is not only lost visits. Googlebot asking for a page it has indexed
and receiving a bare 404 is the strongest de-indexing signal there is —
stronger than a 5xx, which Google treats as temporary. The site loses minutes
several times a day whenever the pipeline stalls the event loop, and it is
doing that while 23,461 pages are being re-evaluated after
[[2026-06-search-index-collapse]]. Availability work is ranking work.

### Limits worth knowing

- GitHub's scheduled runs are best-effort and can be delayed under load; treat
  15 minutes as an upper bound on detection, not a guarantee.
- GitHub disables schedules on a repository with no activity for 60 days.
- The workflow checks reachability, not correctness. It cannot tell you the
  extraction stopped producing records — that is what the daily report is for.
- Neither one tells you whether any of the traffic converts. That is the
  [[acquisition-funnel]], carried by `/v1/funnel` and the report's
  **Vevőszerzés** block since 2026-08-21.
