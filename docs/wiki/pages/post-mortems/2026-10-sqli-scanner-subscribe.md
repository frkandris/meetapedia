---
type: Post-mortem
title: The Scanner That Subscribed Nine Times
description: An SQL-injection scanner probed /subscribe and /varosok/kerelem; the parameterised inserts ran nothing, but every probe became a stored row (and for /subscribe a notification e-mail), because no public form checked what it was given.
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
the time. At 09:14 UTC the same scanner sent fourteen probes to the town-request
form (`/varosok/kerelem`): names and addresses `1`, `1'`, `1')`, `98766`. Every
row in `city_requests` was one of them.

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

### Every other public form (same day)

| Form | Now requires |
|---|---|
| `/claim-community` | a stored, visible `community_id` and a plausible address; name and city come from the record |
| `/report-not-community` | a stored `community_id`; name, city from the record, topic only if configured, `source_url` only if it is one of the record's own |
| `/suggest-edit` | a `record_key` that resolves (community, or venue via `get_venue_by_record_key`) — approval applies by that key; name, city, topic from the record; `wrong_city`/`wrong_topic` values must be configured; an address, if given, must be plausible |
| `/feedback` | nothing new to send, but an unknown city or topic and an implausible reply-to are dropped |
| `/varosok/kerelem` | a name of letters, spaces, `.'-` (the town is not listed yet, so it cannot be checked against `cities.yaml`); the echo into the redirect is URL-quoted |
| `/submit-community` | a configured city and topic — both are `<select>`s on the form |

Every link printed into the operator's mail (`page_url`) must be an http(s)
page on the host that received the request (`_own_page_url`); a
`javascript:` URL or a foreign site is dropped. Free text is capped
(`_FORM_NAME_MAX` 200, `_FORM_TEXT_MAX` 5,000). `tests/test_public_form_validation.py`
covers all six. The fourteen `city_requests` rows were deleted the same day.

## Lessons

- A parameterised query stops injection, not garbage. Every public form that
  writes or e-mails accepts only values the site itself offered, and a form
  about a record takes that record's name from the database, not the request.
- `not_community_reports` holds 1,565 rows whose community no longer exists
  as of 2026-10-03; those are reports that were acted on (the record was
  hidden or merged), not probes. Do not clean them up as junk.
