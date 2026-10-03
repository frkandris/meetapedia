---
type: Data-model
title: PersonRecord
description: Leaders/instructors extracted per community; enforces a two-word name rule and normalizes role to one of 12 values (default "leader").
tags: [models, person, pydantic, validation]
timestamp: 2026-10-03
resource: scraper/models.py
---

# PersonRecord

*Required: `name, role, city, topic, community_name`. `person_id` = SHA-256[:12] of `name|city|role|community_name`.*

# Schema

`role` is normalized: if not one of `PERSON_ROLES` (12 values — `leader, instructor, speaker, organizer, founder, coach, trainer, moderator, admin, member, volunteer, coordinator`) it is forced to `"leader"`. The `PERSON_SYSTEM_PROMPT` only documents 3 of these (leader/instructor/speaker); the other 9 are accepted-but-undocumented, so most non-standard roles collapse to `"leader"`.

## Two-word name rule

`if len(self.name.split()) < 2: raise ValueError` — a single-word "name" aborts record creation. `_parse_persons` catches this and logs `person_validation_failed`. Rationale: single-word names are usually role labels or noise, not real people.

## Who gets a public page

A person page states a fact — this person leads that group in this town — so since 2026-10-03
(see [[bot-crawl-audit-2026-10-03]]) a stored row is published only when both hold:

- **The name is a name.** `models.is_publishable_person_name` refuses the placeholders the
  extractor writes when a page names nobody — "Not specified", "Jane Smith" (the prompt's own
  `leader` example, copied by weak models; the prompt is not reworded because that would change
  the extraction fingerprint and re-extract the corpus), group descriptors opening with an article
  or "local" ("Local organizers", "Az alapítvány vezetői") — and names with unbalanced
  parentheses, where the leader parser cut inside a role. The model refuses the same names at
  creation, and `CommunityRecord` drops a `leader` whose name part is a placeholder; the community
  page also scrubs one stored earlier.
- **The group is listed in that town.** `db.get_publishable_persons` keeps a row only when a
  visible community with the same `normalized_match_key` name exists in the same city.

The sitemap, `/emberek`·`/people` and the person page all read `get_publishable_persons`, so a URL
cannot be submitted that would refuse to render. A person URL that does not resolve now answers
**404**, not a 302 to the index — a redirect to a list is a soft 404 that keeps the URL queued.
Rows are not deleted: if the group reappears in that town, the page comes back.

## Extraction gating

Persons are only extracted for pages that yielded communities — see the person-skip optimization in [[extraction-layer]]. The `PERSON_USER_PROMPT_TEMPLATE` injects the known `community_names` as reference context. Extraction runs at `temperature 0.0`.

## History churn

`delete_leader_persons_for_community` deletes all `role='leader'` persons before re-inserting parsed ones; each cycle re-logs a `__created__` history row, which is why the activity timeline dedups via `MIN(changed_at)`. See [[history-created-sentinel-overcounting]].
