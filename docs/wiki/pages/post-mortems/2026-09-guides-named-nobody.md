---
type: Post-mortem
title: The Guides That Named Nobody
description: Nine of the first ten published guides mentioned not one of the groups they were guiding readers to, because the community list was the tail of a packet that overflowed an 8,192-token window.
tags: [guides, prompting, context-window, localgpu, quality-gate]
timestamp: 2026-09-21
resource: scraper/guides.py
---

# The Guides That Named Nobody

*The model was not being vague. It had never seen the names.*

## What shipped

Between 2026-09-20 and 2026-09-21 the worker published ten data guides, all
Hungarian, all written by `localgpu:qwen3-4b-q4km`. The operator's verdict on
reading them was blunt and correct. Measured against the corpus:

| Symptom | Measurement across the ten articles |
|---|---|
| Groups named in the prose | **0** in nine of ten; 6 in the tenth |
| Verbatim sentences repeated | up to 10 per article; 8 of 10 had at least 2 |
| Most repeated three-word phrase | 5 to 13 occurrences (a normal article: 2) |
| Length | 300–401 words, under the 350 the prompt asked for |

One article's `choosing_advice` section was **83% verbatim copy** of its own
`comparison` section. Another opened its conclusion with the same sentence as
its introduction.

## The cause nobody would have guessed from reading the prose

Three causes, and only the third explains the headline number.

**The sections had no distinct jobs.** An `introduction` and a `conclusion`
about the same subject *are* the same paragraph; so are a `comparison` and the
advice drawn from it. Four such slots are an invitation to say everything twice.

**The word floor was English-shaped.** The prompt asked for 350–700 words and
the validator accepted 300–800. A dense, non-repetitive Hungarian draft of this
article measures about **330 words** where its English twin measures well over
400 — Hungarian is agglutinative. The model had roughly 300 words of real
material and a floor above it, so it padded, and padding is repetition.

**The names were trimmed off the prompt.** This is the one that matters.
`localgpu` runs llama.cpp with `-c 8192`, and llama.cpp answers an over-long
prompt by *trimming* it rather than failing — a behaviour [[our-own-gpu-in-the-fleet]]
already records for page text. Hungarian tokenizes at roughly **1.4 characters
per token** against an English-trained vocabulary, so the 9,400-character fact
packet was near 6,700 tokens; with the system prompt and the reserved answer it
did not fit. And `communities` — the list of names, descriptions and locations —
was the **last key in the packet**, so it was the part that fell off the end.

Nine articles named nobody because nine packets arrived with the names removed.
Nothing logged it. The trim is silent by design.

It only became visible when a longer v2 prompt pushed the same packets past
trimming into a hard `500 Context size has been exceeded`.

## The fixes

**Order the packet by what it costs to lose.** `communities` now comes first and
the dimension tables last, because the page prints those tables in cards under
the prose either way — a reader sees them regardless, while a name can only
reach them through the article. A deterministic trim to a 6,000-character budget
sheds dimension examples first and never takes the community list below the
number of names the article is required to use.

**Give the sections different verbs.** `orientation` (name and sort), 
`practicalities` (interpret), `choosing_advice` (advise), `gaps` (disclose).
The prompt also states what the reader can already see in the cards below, so
prose that restates field values is understood to be worthless.

**Check mechanically what the prompt asks for.** The gate now rejects a repeated
sentence, a three-word phrase occurring more than four times, fewer than three
named groups, a section under 40 words, and — added after a model answered a
Hungarian packet in fluent English — a Hungarian guide without Hungarian
function words. Every threshold is calibrated on these ten articles, which the
gate rejects 10/10, and on a hand-written draft in `tests/test_daily_guides.py`
that it must accept.

**Say why, when nothing publishes.** Each refusal increments
`guide_rejected_<reason>` for the day and logs the city, topic and reason. A day
that publishes nothing must be able to say whether the corpus ran out of
candidates or the writer kept failing the gate; those need opposite fixes.

**Replace what is already public.** A guide whose `prompt_version` is not the
current one is rewritten in place — same slug, same `published_at`, new body —
before any new guide is published, because fixing a page people can already read
beats adding an eleventh.

## What this generalizes to

A silent trim is worse than an error, and you find it by measuring the output
against the input, not by reading the output. Ten articles were reviewed by
eye before anyone counted how many names they contained — and "vague prose" is
what a missing input looks like from the outside.

The second lesson is narrower and repeats one this project keeps relearning: a
number carried from English does not survive translation. The word floor was
one. The token budget was another.

## See also

- [[our-own-gpu-in-the-fleet]] — the 8,192-token window and why it is set there
- [[free-model-catalogue-churn]] — why the model that writes a guide changes without notice
