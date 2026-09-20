---
type: Concept
title: Acquisition Funnel
description: The stages between a search result and a person who acts — visitors, outclicks, subscriptions, claims, submissions — where each is recorded and what may legally be done with the addresses collected.
tags: [growth, acquisition, funnel, metrics, gdpr, email]
timestamp: 2026-09-20
resource: scraper/db.py
---

# Acquisition Funnel

*Everything between "Google sent someone" and "a person did something", and the
one legal rule that decides which of those we may follow up.*

## The stages

| Stage | Table | Written by |
|---|---|---|
| Pageview / unique visitor | `traffic_daily`, `traffic_visitors` | `record_pageview` |
| Outclick — someone left for the community itself | `outclick_events` | `log_outclick` |
| Subscription — city + topic, with consent | `subscriptions` | `save_subscription` |
| Claim — an organiser asks for their listing | `edit_requests` (`change_type='claim'`) | `/claim-community` |
| Submission — a community sent in by hand | `community_submissions` | `/submit-community` |
| Correction / report | `edit_requests`, `not_community_reports` | `/suggest-edit`, `/report-not-community` |

`get_funnel_counts(db_path, days)` reads all of them in one call; `/v1/funnel`
exposes it under the control key, and the daily report carries it as the
**Vevőszerzés** table. Before 2026-08-21 every one of these numbers existed and
none was readable without the admin password, so "is anything converting?" was
answered by guessing — the same blind spot that let a 90% index collapse pass
unremarked for two months ([[2026-06-search-index-collapse]]).

**Outclicks are recorded without redirecting the reader.** The original tracker
routed every outbound link through `/out?url=…`, a 302 on our own domain, so a
crawler never saw a direct link to the community's site; it was removed in
`e7d373e` (2026-06-05), and Search Console still lists thousands of “Page with
redirect” URLs from that period. The replacement keeps the literal external URL
in `<a href>` and marks only community-detail CTAs with `data-outclick`.
`static/js/outclick.js` sends a same-origin beacon on click/auxclick and never
prevents navigation. `POST /api/outclick` accepts only the three rendered link
types and only a URL actually stored on the named visible community, then calls
`log_outclick`; malformed analytics quietly disappear. No IP, cookie or visitor
identifier is stored.

**The outclick is the stage that would matter.** A pageview says Google sent someone;
an outclick says the site did its job and they went on to the community. Traffic
without outclicks is a page that ranks and helps nobody.

## Claims are the strongest signal on the site

A claim is a person who runs the group typing their own address in and asking
for the listing. Until 2026-08-21 it was emailed to the operator and stored
nowhere: with `RESEND_API_KEY` unset, or on any Resend failure, it vanished
while the visitor was shown a green tick. It is now persisted **before** the
mail is attempted, and the mail is still best-effort on top.

## What may be sent, and to whom

Hungary's Advertising Act (**2008. évi XLVIII. törvény, §6**) requires *prior
express consent* from a natural person before advertising may be sent by email
or equivalent individual communication. There is **no legitimate-interest
exception** of the kind some member states allow for B2B cold mail, and consent
is withdrawable at any time, free and without explanation.

Consequences, and they are not negotiable:

- The ~42K scraped records carry `email`, `phone` and `website` fields. That is
  **not a send list.** The standard directory playbook — mass "claim your
  listing" outreach to everyone indexed, the move that built Yelp — is closed to
  us in this market.
- The `subscriptions` table **is** an opt-in list: the visitor asked to be told
  about a named city and topic. Mail to it must stay within what they asked
  for, and every message needs the working unsubscribe (`/unsubscribe?token=`,
  already built, `Disallow`ed in robots.txt).
- `records_with_email` in the funnel exists to size what an opt-in channel
  *could* reach, not to build a list from. Since 2026-09-17 the same question is
  answered for `persons` (a named leader or contact) and in **distinct addresses**
  as well as rows: one address sits on a club's page and on its leader's, and a
  community centre's office address serves every group in the building, so rows
  overstate reach. The row counts size the corpus; `records_email_distinct` and
  `persons_email_distinct` size the audience. The two tables are reported
  separately rather than summed, because they overlap by design.

Postal mail to a natural person does not need prior consent under the same Act.
Noted for completeness; nobody is posting letters.

## What is missing

Nothing is ever sent to the subscription list — the addresses are collected,
mailed to the operator once, and never used again. That is the largest unspent
asset in the funnel and the only channel that does not depend on Google, which
matters more after an index collapse than before one.

Related: [[production-monitoring]], [[2026-06-search-index-collapse]].
