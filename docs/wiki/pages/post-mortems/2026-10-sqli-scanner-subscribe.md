---
type: Post-mortem
title: The Scanner That Subscribed Nine Times
description: An SQL-injection scanner probed /subscribe with nine forms in twenty seconds; the parameterised insert ran nothing, but every probe became a subscriptions row and a notification e-mail, because the form accepted any email, city and topic.
tags: [security, forms, subscribe, validation]
timestamp: 2026-10-03
resource: scraper/web/app.py
---

# The Scanner That Subscribed Nine Times

*Nothing was breached. The form just believed everything it was told.*

## Symptom

On 2026-10-03 at 08:49–08:50 UTC the feedback inbox received nine
"Új feliratkozás — Sydney" e-mails. Every one had email `1`, city `Sydney`, and a
topic field walking an automated scanner's script: `chess`, then `chess'`,
`chess"`, `chess')`, `chess)`, a reflection marker `chessxh3probe9`, a MySQL
`EXTRACTVALUE(...)` error-based payload and a PostgreSQL `CAST(...||(SELECT CASE ...))`
one. All nine rows landed in `subscriptions`, which held no real subscriber at
the time.

## Root cause

Not an injection: `save_subscription` (`scraper/db.py`) uses bound parameters,
the notification escapes every field with `html.escape`, the payloads targeted
MySQL and PostgreSQL rather than SQLite, and the endpoint answers only a 302, so
the scanner got no error signal. The defect was that `POST /subscribe` validated
nothing — any string was an email, a city or a topic — so each probe was stored
and announced.

## Fix

`public_subscribe` keeps only topics in `app_state.topics`, a city in
`app_state.cities`, and an address matching a loose `_EMAIL_RE`. Anything else
falls into the existing "incomplete form" branch: redirect, no row, no e-mail,
and nothing of the probe reflected into the redirect URL.
`tests/test_subscribe_validation.py` replays the scanner's payloads. The nine
rows were deleted from production the same day.

## Lessons

- A parameterised query stops injection, not garbage. Every public form that
  writes or e-mails should accept only values the site itself offered.
- The other public forms (`/suggest-edit`, `/claim-community`,
  `/report-not-community`) were not part of this change.
