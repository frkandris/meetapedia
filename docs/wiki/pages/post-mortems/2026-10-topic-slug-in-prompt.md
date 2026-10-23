---
type: Post-mortem
title: The Model Was Asked for "nok" Community Groups
description: The extraction prompt sent topic slugs, so "nok" (women) meant nothing and city-wide NGO lists were filed as women's groups — 527 of 1,328 visible records.
tags: [extraction, prompt, topics, data-quality]
timestamp: 2026-10-23
resource: scraper/extract.py
---
# The Model Was Asked for "nok" Community Groups

*A slug in the prompt meant nothing to the model, so whole NGO lists became women's groups.*

## Symptom

On 2026-10-23, when the city page started printing one heading per topic, the
largest section on kozossegek.com/pecs was "Nők 51" — and it held the county
civil guard federation, the sports federations' federation, a diabetes society
and a Babits society. Their descriptions said "női civil szervezet Pécsett"; one
said "Lelkivezetés Pécsben, a nok számára".

## Root cause

`USER_PROMPT_TEMPLATE` began "Extract all **{topic}** community groups located in
or near {city}" and `{topic}` was the slug. For English slugs that reads well
("choir"); for `nok` (unaccented Hungarian "women") it is not a word, and a
search for "nők közössége Pécs" had returned pecs.hu/civil-szervezetek, a list of
every NGO in the city. The model took all of them, and wrote the topic into the
descriptions to match. Same exposure for `kisallat`, `vallalkozas`,
`hagyomanyorzes`, `fogyatekossag`, `baby`, `other`.

Measured in production (`scripts/audit_topic_misfits.py`, dry run): **527 of
1,328** visible `nok` records have no female marker in name or tags — 59 of them
also listed under another topic, 468 listed only here. Top sources are municipal
NGO directories (pecs.hu 31, a Bottrop parish 20, Taipei 19, miskolc.hu 12…).
The marker is a heuristic: International Women in Portugal's and the University
Women's Club's sub-groups (book club, walking group) are genuine and carry no
marker in their own names.

## Fix

- `extract.topic_for_prompt()` gives the prompt a readable phrase ("women's",
  "pet owners'", "folk tradition and heritage"); `extract.topic_rule()` adds
  {topic_rule}: a general NGO list is not a list of this topic, take only the
  groups that belong, and never write the topic into a description the page does
  not support. Empty for `other`, whose groups are exactly such lists.
- The user prompt is **not** part of the extraction fingerprint (only the system
  prompt and model are, `get_extract_fingerprint`), so this invalidates nothing:
  it governs new extractions; stored records are a separate clean-up, decided by
  the owner with the audit's CSV.

## Lessons

- A slug is an identifier, not an instruction. Anything that reaches a model is
  copy, and gets read as such.
- A grouped view surfaced in one glance what flat lists had hidden for months.
