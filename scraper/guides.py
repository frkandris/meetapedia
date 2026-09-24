"""Publish bounded, factual guide pages from the existing community corpus."""

import structlog

from collections import Counter
from datetime import datetime, timezone
import json
import re
from pathlib import Path

from .db import (bump_daily_counter, count_data_guides_published_on,
                 create_data_guide, get_communities, get_daily_counter,
                 get_daily_counters_with_prefix,
                 get_guide_candidate_groups, get_stale_data_guides,
                 init_db, replace_data_guide)
from .extract import ExtractorContentError, _message_text
from .identity import public_slug
from .web.i18n import display_languages, get_topic_labels, topic_phrase

log = structlog.get_logger(__name__)

_DIMENSIONS = ("meeting_schedule", "frequency", "location", "fee",
               "skill_level", "age_range", "language", "join_process")
_DIMENSION_LABELS = {
    "hu": dict(zip(_DIMENSIONS, ("Találkozási idő", "Gyakoriság", "Helyszín",
        "Részvételi díj", "Szint", "Korosztály", "Nyelv", "Csatlakozás"))),
    "en": dict(zip(_DIMENSIONS, ("Meeting times", "Frequency", "Location",
        "Fees", "Experience level", "Age range", "Language", "How to join"))),
}
_PROMPT_VERSION = "guide-writer-v2"

# The four sections of a v2 article, in reading order. v1 used
# introduction/comparison/choosing_advice/conclusion, which is why every v1
# article echoed itself: an introduction and a conclusion have the same job,
# and so do a comparison and the advice drawn from it. Each name below is a
# different verb — name, interpret, advise, disclose — and the validator
# enforces that no sentence is shared between them.
_SECTIONS = ("orientation", "practicalities", "choosing_advice", "gaps")
_LEGACY_SECTIONS = ("introduction", "comparison", "choosing_advice", "conclusion")

# Calibrated on the ten v1 articles published 2026-09-20/21, every one of which
# this gate rejects. See docs/wiki/pages/post-mortems/.
_MAX_SHINGLE_REPEATS = 4     # worst v1 article: 13
_MIN_NAMED_GROUPS = 3        # nine of ten v1 articles named zero

# A total floor is what a padded article is padded *to*. v1 asked for 350-700
# words and accepted 300-800; every article landed between 300 and 401, and the
# filler making up the difference is the repetition this module now rejects.
# The number was also English-shaped: Hungarian is agglutinative, and a dense,
# non-repetitive Hungarian draft of this article measures about 330 words where
# its English twin measures well over 400. So the floor is low enough that no
# draft has to pad to reach it, and substance is enforced where it actually
# lives — every section must carry its weight, and none may repeat another.
_MIN_WORDS, _MAX_WORDS = 260, 650
_MIN_SECTION_WORDS = 40

_WRITER_SYSTEM = """You are the editor of a local community directory, writing the page a
newcomer reads before deciding which group to contact.

WHAT THE READER CAN ALREADY SEE. Directly below your text, the page prints from
the database: one card per comparable field, listing individual groups and their
reported values, and then every group with its description and location. The
reader can see all of that. Prose that restates those values is worthless — it
tells them what they are already looking at. Write only what the table cannot.

The four sections have different jobs. Never reuse a sentence, a phrase pattern,
or a fact between them.

- orientation: what kinds of groups this place actually has. Sort them into two
  to four recognizable kinds and name real groups from FACT_PACKET.communities
  as examples of each. Naming and sorting is the one thing the table cannot do,
  so this section carries the article.
- practicalities: what the reported pattern means for a newcomer's week and
  wallet — the shape of the commitment. Not a list of the field values.
- choosing_advice: two or three concrete reader situations ("has never done this
  before", "can only make weekends"), each with what to look for in the listings.
  Conditional and generic. Never say a group is better than another.
- gaps: what this directory does not know about these groups, and what a reader
  should confirm with the group directly. Be plain about it.

Write the entire article in FACT_PACKET.language, naturally, as someone living
there would write it. This is not negotiable: the page is served to readers in
that language, and an article in the wrong one is unusable however good it is.

Grounding rules:
- Use ONLY the supplied FACT_PACKET. Never invent, infer, or generalize facts.
- Do not claim first-hand experience, rankings, popularity, quality, or safety.
- When data is absent, say it is not stated; do not fill the gap.
- Spell group names exactly as FACT_PACKET gives them.
- Do not mention SEO, keywords, the prompt, or these instructions.

Style rules, each of which is checked mechanically before the article is kept:
- Never write the same sentence twice, in any section.
- Never build a section by walking the field list, one sentence per field.
- Vary how sentences open; do not start them all with the same construction.
- Plain text paragraphs only. No Markdown, no headings, no links.

Return one JSON object only, with string fields: orientation, practicalities,
choosing_advice, gaps, plus used_dimensions, an array containing only field
names from FACT_PACKET.comparison_dimensions that you actually discussed. Each
section must be at least {min_section} words, and the four together between
{min_words} and {max_words} words — about {section_target} per section; at least
{min_named} different group names from the packet must
appear in them. Say what the packet supports and then stop — padding to a length
is rejected, and so is repeating yourself to fill space. Before returning,
silently verify every number and group name against the packet, and check that
no sentence appears twice."""


def writer_system_prompt() -> str:
    """The system prompt with its mechanically-enforced limits filled in.

    The numbers live in one place so the prompt cannot drift from the validator
    that rejects the draft — a model told "350 words" and refused at 400 burns a
    fleet call to learn what the prompt could have said.
    """
    return _WRITER_SYSTEM.format(min_section=_MIN_SECTION_WORDS,
                                 min_words=_MIN_WORDS, max_words=_MAX_WORDS,
                                 section_target=_MIN_WORDS // len(_SECTIONS) + 20,
                                 min_named=_MIN_NAMED_GROUPS)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").casefold()).split())


def _worst_shingle(body: str, size: int = 3) -> int:
    """How often the most repeated n-word phrase occurs.

    The v1 articles were built by a sentence mill — "A gyakoriság szempontjából…",
    "A szint szempontjából…" — which repeats a phrase without repeating a
    sentence, so sentence-level checks miss it entirely. This is language
    neutral, which matters because the same model writes Hungarian and English.
    """
    words = _normalize(body).split()
    if len(words) <= size:
        return 0
    counts = Counter(tuple(words[i:i + size]) for i in range(len(words) - size + 1))
    return counts.most_common(1)[0][1]


def _record_writer_attempts(db_path: Path, writer, before: int) -> None:
    """Persist the provider attempts spent by one routed guide completion."""
    after = int(getattr(writer, "calls_made", 0) or 0)
    spent = max(0, after - before)
    if spent:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        bump_daily_counter(db_path, day, "guide_attempts", spent)


#: Hungarian function words, chosen for having no English homograph — the first
#: draft of this list included "a", "is" and "de", which an English article
#: supplies by the dozen, so the check passed everything. Word-bounded so "az"
#: cannot match inside "Zászlóforgatás".
_HU_FUNCTION_WORDS = re.compile(
    r"\b(?:az|és|hogy|nem|vagy|ezt|azt|ott|ahol|amely|ami|már|csak|egy|van"
    r"|kell|lehet|meg|mert|nincs|így|ezek|ilyen)\b")


def _decode_article(raw: str, allowed_dimensions: set[str],
                    allowed_numbers: set[str],
                    community_names: "list[str] | None" = None,
                    locale: str = "") -> tuple[dict | None, str]:
    """Return ``(article, reason)``; ``article`` is None when the draft is refused.

    The reason is returned rather than logged here so the caller can count
    refusals per UTC day. A silent `continue` is what let ten repetitive
    articles reach the public site without anything recording why the gate did
    not stop them — it had nothing to say about prose.
    """
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None, "unparseable_json"
    if not isinstance(obj, dict) or any(not isinstance(obj.get(k), str) for k in _SECTIONS):
        return None, "missing_sections"
    used = obj.get("used_dimensions")
    if (not isinstance(used, list) or not used
            or any(not isinstance(v, str) or v not in allowed_dimensions for v in used)):
        return None, "bad_dimensions"

    body = " ".join(obj[k] for k in _SECTIONS)
    word_count = len(body.split())
    # Two reasons, not one: "word_count" was the most common refusal for three
    # days running and could not say which way the drafts missed.
    if word_count < _MIN_WORDS:
        return None, "too_few_words"
    if word_count > _MAX_WORDS:
        return None, "too_many_words"
    if any(len(obj[k].split()) < _MIN_SECTION_WORDS for k in _SECTIONS):
        return None, "thin_section"

    # A professional draft must be auditable. Numbers are the easiest class of
    # fabricated claim to reject deterministically: every digit sequence in the
    # prose must already occur in the fact packet. Links belong to deterministic
    # page chrome, never to model-written copy.
    if set(re.findall(r"\d+", body)) - allowed_numbers:
        return None, "untraceable_number"
    if re.search(r"https?://|www\.", body, flags=re.I):
        return None, "link_in_prose"

    # Everything below is the anti-repetition gate, calibrated on the v1 corpus.
    seen: dict[str, str] = {}
    for section in _SECTIONS:
        for sentence in _sentences(obj[section]):
            key = _normalize(sentence)
            if len(key.split()) < 4:
                continue  # a short fragment repeating is not the failure mode
            if key in seen:
                return None, "repeated_sentence"
            seen[key] = section
    if _worst_shingle(body) > _MAX_SHINGLE_REPEATS:
        return None, "repeated_phrasing"

    # Language, checked rather than hoped for. Hungarian function words are
    # absent from an English article even when it quotes Hungarian group names,
    # which is exactly the draft this catches: a Hungarian town written up in
    # English reads as competent work and is useless to the reader it is for.
    # Measured on the ten real Hungarian articles of 2026-09-20/21: 15 to 44
    # hits, against 0 for an English draft naming the same Hungarian groups.
    # The floor is 8 rather than 10 because the shortest article this validator
    # accepts is 260 words, where the thinnest of those ten would score ~12.
    if locale == "hu" and len(_HU_FUNCTION_WORDS.findall(body.casefold())) < 8:
        return None, "wrong_language"

    # A directory guide that names none of its groups is not a guide. Nine of
    # the ten v1 articles named zero, which no other check could see.
    names = [n for n in (community_names or []) if n and len(n) > 3]
    if names:
        lowered = body.casefold()
        mentioned = sum(1 for n in names if n.casefold() in lowered)
        if mentioned < min(_MIN_NAMED_GROUPS, len(names)):
            return None, "no_named_groups"
        obj["named_groups"] = mentioned

    obj["word_count"] = word_count
    # Rendering order belongs with the article, not with the template: a guide
    # published under an older prompt version keeps its own section order and
    # keys for as long as it stays online.
    obj["paragraphs"] = [obj[k] for k in _SECTIONS]
    return obj, ""


# The smallest context window in the fleet is localgpu's 8,192 tokens, and
# llama.cpp answers an over-long prompt by trimming it, not by failing. Hungarian
# tokenizes badly against a mostly-English vocabulary — measured at roughly 1.4
# characters per token on these packets, against 3.5 for the English prompt — so
# a packet that looks modest in characters is not. Budget: 8,192 minus 1,400
# reserved for the answer and ~600 for the system prompt leaves ~6,000 tokens,
# which at the Hungarian ratio is about 8,400 characters. 6,000 keeps a margin.
# A 650-word article is roughly 900 tokens, but a reasoning model spends its
# thinking here too — nemotron returned 1,400 tokens of deliberation and no JSON
# at all on 2026-09-21. The headroom is affordable only because the packet
# shrank: Groq charges prompt + max_tokens against an 8,000-token minute window
# before generating, and 1,700 + 2,400 fits where the old 3,400 + 2,400 did not.
_WRITER_MAX_TOKENS = 2400
#: A field where this share of reports carry one value is not a comparison.
#: Measured on the 2026-09-21 Pécs guide: language 42/44 = 95% (dropped),
#: frequency 1/2 = 50% and join_process 1/3 = 33% (both kept).
_DOMINANT_VALUE_SHARE = 0.9
_PACKET_BUDGET_CHARS = 6000

#: Daily counters that make the guide budget restart-safe. A rewrite keeps its
#: original `published_at`, so counting publications alone never sees it; and a
#: refused city/topic is remembered so a retry moves on to the next candidate
#: instead of paying for the same refusal again.
_REWRITTEN_COUNTER = "guide_rewritten"
_REFUSED_PREFIX = "guide_refused:"


def is_comparable(counts: list[int], covered: int) -> bool:
    """Whether a field's value counts make a comparison worth a card.

    `counts` is the per-value tally, largest first; `covered` is how many
    groups reported the field at all. It is the denominator even when `counts`
    is truncated — the stored guide keeps only the top five values, and
    dividing by their sum would call a field dominant when half its reports are
    spread across values that fell off the list. One value is never a
    comparison, and a value carrying `_DOMINANT_VALUE_SHARE` of the reports is
    near enough one. Shared by the build-time and the render-time rule so the
    two cannot disagree.
    """
    counts = [int(c) for c in counts if int(c) > 0]
    covered = max(int(covered or 0), sum(counts))
    if len(counts) < 2 or not covered:
        return False
    return max(counts) / covered < _DOMINANT_VALUE_SHARE


def guide_budget_left(db_path: Path, limit: int, day: str) -> int:
    """Today's guide slots not yet spent on a publication or a rewrite."""
    spent = (count_data_guides_published_on(db_path, day)
             + get_daily_counter(db_path, day, _REWRITTEN_COUNTER))
    return max(0, limit - spent)


class GuidePass(list):
    """The guides one pass published, plus whether the day is finished.

    `settled` is True when there is nothing left to do today: the budget is
    spent, or every candidate has been tried. The worker marks the day done on
    it; retrying an unsettled day is what fills the remaining slots.
    """

    settled: bool = False
_PACKET_MAX_COMMUNITIES = 12
_PACKET_DESCRIPTION_CHARS = 160
_PACKET_EXAMPLES = 3


def _fact_packet(city: str, country: str, topic_label: str, locale: str,
                 records: list[dict], dimensions: list[dict],
                 count: int, described: int) -> dict:
    """The writer's whole world, ordered so that a trim costs the least.

    `communities` comes first and the bulky dimension tables last. This is not
    cosmetic. A context window that overflows drops the tail, and in v1 the tail
    was the community list: nine of the ten articles published 2026-09-20/21
    named no group at all, because the names never reached the model. The
    dimension examples are the right thing to lose instead — the page prints
    them in cards directly under the prose, so the reader sees them either way,
    while a name can only come from the article.
    """
    packet = {
        "language": "Hungarian" if locale == "hu" else "English",
        "reader_goal": "Choose a suitable community to contact or visit",
        "city": city, "country": country, "topic": topic_label,
        "community_count": count, "described_count": described,
        "communities": [
            {"name": r.get("name", ""),
             "description": _text(r)[:_PACKET_DESCRIPTION_CHARS],
             "location": r.get("location", "")}
            for r in records[:_PACKET_MAX_COMMUNITIES] if r.get("name")
        ],
        "comparison_dimensions": [
            {**d,
             "examples": d["examples"][:_PACKET_EXAMPLES],
             "common": d["common"][:_PACKET_EXAMPLES]}
            for d in dimensions
        ],
    }
    # Deterministic trim, cheapest thing first, and never below the number of
    # names the article is required to use.
    while (len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
           > _PACKET_BUDGET_CHARS):
        dims = packet["comparison_dimensions"]
        longest = max(dims, key=lambda d: len(d["examples"]), default=None)
        if longest is not None and len(longest["examples"]) > 1:
            longest["examples"] = longest["examples"][:-1]
            continue
        if len(packet["communities"]) > _MIN_NAMED_GROUPS + 2:
            packet["communities"] = packet["communities"][:-1]
            continue
        if len(dims) > 2:
            packet["comparison_dimensions"] = dims[:-1]
            continue
        break
    return packet


def _text(record: dict) -> str:
    return (record.get("long_description") or record.get("description")
            or record.get("short_description") or "").strip()


def _dimension_value(field: str, raw, locale: str) -> str:
    """One field's value as the reader should see it.

    Only `language` is normalised, and only because it has a closed vocabulary
    that the corpus writes three ways: "Magyar", "Hungarian" and "magyar" are
    16,611 records describing one language. Left raw they defeat the comparison
    rules — three spellings look like variation — and put an English word on a
    Hungarian page. The other fields are free text with no closed vocabulary,
    so they are left exactly as the source wrote them; mapping those would mean
    inventing meaning the extractor never recorded.
    """
    text = str(raw or "").strip()
    if field == "language" and text:
        return display_languages(text, locale)
    return text


def _dimension_sections(records: list[dict], locale: str,
                        minimum_values: int = 2) -> list[dict]:
    """The fields worth putting side by side, most-reported first.

    A dimension earns its card by **discriminating**. The guide published on
    2026-09-21 gave its most prominent card to "Nyelv", where 42 of 44 groups
    said "Hungarian" and the other two said "Magyar" — the same fact written two
    ways by the extractor. A distinctness test passes that; a card listing five
    groups that all say the same thing still compares nothing. So the test is
    **dominance**: if one value accounts for `_DOMINANT_VALUE_SHARE` of the
    reports, the field is not a comparison, however complete it is. "Most of
    them are free" is a real finding, but it belongs in the prose — the writer
    gets the value counts for exactly that — not in a table of five identical
    rows.

    Coverage is carried with its denominator. "2 közösségnél ismert" reads like
    a fact about the topic until you learn there are 44 groups; "2 / 44" says
    what it is. Low coverage is not a reason to hide the field — it is a reason
    to be honest about it — so the number is shown, not the field removed.
    """
    total = sum(1 for r in records if r.get("name"))
    sections = []
    for field in _DIMENSIONS:
        values = [(r.get("name", ""), _dimension_value(field, r.get(field), locale))
                  for r in records]
        values = [(name, value) for name, value in values if name and value]
        if len(values) < minimum_values:
            continue
        counts = Counter(value for _, value in values)
        if not is_comparable(list(counts.values()), len(values)):
            continue
        common = counts.most_common(5)
        sections.append({
            "field": field,
            "label": _DIMENSION_LABELS[locale][field],
            "covered": len(values),
            "total": total,
            "distinct": len(counts),
            "examples": [{"name": name, "value": value} for name, value in values[:5]],
            "common": [{"value": value, "count": count} for value, count in common],
        })
    # Best-covered first: the card a reader meets first should be the one with
    # the most behind it, not whichever field happens to sort earliest.
    sections.sort(key=lambda d: (-d["covered"], -d["distinct"], d["field"]))
    return sections


async def _draft_guide(db_path: Path, city: str, topic: str, meta, writer, *,
                       stamp: str, day: str, min_dimensions: int,
                       min_communities: int = 0,
                       min_description_ratio: float = 0.0) -> tuple[dict | None, str]:
    """Build the fact packet, spend one routed call, and gate the result.

    Shared by publication and rewriting so a guide is held to the same standard
    however it came to be written — the rewrite path exists precisely because
    the standard moved.
    """
    records = sorted(get_communities(db_path, city, topic),
                     key=lambda r: (not bool(_text(r)), r.get("name", "").casefold()))
    count = len(records)
    described = sum(1 for r in records if _text(r))
    if count < min_communities:
        return None, "too_few_communities"
    if count and described / count < min_description_ratio:
        return None, "too_few_descriptions"

    is_hu = getattr(meta, "country", "") == "Hungary"
    locale = "hu" if is_hu else "en"
    dimensions = _dimension_sections(records, locale)
    if len(dimensions) < min_dimensions:
        return None, "too_few_dimensions"
    labels = get_topic_labels(locale)
    topic_label = labels.get(topic, topic.replace("_", " ").title())
    if is_hu:
        # Hungarian cannot stack two bare nouns, so the topic needs its
        # attributive form here — see `topic_phrase`.
        title = (f"{topic_phrase(topic, topic_label, locale, 'közösségek').capitalize()} "
                 f"{city} városában: {count} lehetőség")
        summary = (
            f"Adatainkban jelenleg {count} "
            f"{topic_phrase(topic, topic_label.lower(), locale, 'közösség')} szerepel "
            f"{city} területén. {described} közösségről részletes leírás is elérhető; "
            f"az útmutató {len(dimensions)} összehasonlítható szempontot mutat be."
        )
    else:
        title = f"{topic_label} communities in {city}: {count} ways to join"
        summary = (
            f"Our directory currently lists {count} {topic_label.lower()} communities "
            f"in {city}. {described} have a detailed description, and this guide "
            f"compares {len(dimensions)} practical attributes reported by the groups."
        )
    packet = _fact_packet(city, getattr(meta, "country", ""), topic_label,
                          locale, records, dimensions, count, described)

    before = int(getattr(writer, "calls_made", 0) or 0)
    try:
        response = await writer.completion([
            {"role": "system", "content": writer_system_prompt()},
            {"role": "user", "content": "FACT_PACKET:\n" + json.dumps(
                packet, ensure_ascii=False, separators=(",", ":"))},
        ], temperature=0.3, max_tokens=_WRITER_MAX_TOKENS,
           response_format={"type": "json_object"})
    except ExtractorContentError:
        # Every model that answered wrote something unusable (Groq's
        # `json_validate_failed`, a truncated draft). That is a verdict on this
        # draft, like a gate refusal — not an outage, which would defer the
        # whole day's step and retry the same candidate first next time.
        response = {}
    finally:
        # Refused and failed calls spend allowance too, and one routed
        # completion may try multiple providers before returning.
        _record_writer_attempts(db_path, writer, before)
    raw = _message_text(response)
    article, reason = _decode_article(
        raw,
        {d["field"] for d in dimensions},
        set(re.findall(r"\d+", json.dumps(packet, ensure_ascii=False))),
        [c["name"] for c in packet["communities"]],
        locale,
    )
    if not article:
        # Counted and logged, never silent. A day that publishes nothing must be
        # able to say whether the corpus ran out of candidates or the writer
        # kept failing the prose gate — they need opposite fixes.
        model = getattr(writer, "last_model", "") or "unknown"
        bump_daily_counter(db_path, day, f"guide_rejected_{reason}", 1)
        # Also per model. The fleet is ordered by a quality score measured for
        # structured extraction, which is not the same skill: on 2026-09-21 a
        # 4B model scoring 73 returned a 132-word stub for a packet that a 120B
        # scoring 62 turned into 359 usable words naming seven groups. Ordering
        # the writer differently needs evidence per model, and this is it.
        bump_daily_counter(db_path, day, f"guide_rejected_by_{model}", 1)
        log.info("guide_draft_rejected", city=city, topic=topic, reason=reason,
                 model=model)
        return None, reason
    return {
        "slug": f"{public_slug(city)}-{public_slug(topic_label)}",
        "site": "kozossegek" if is_hu else "meetapedia",
        "city": city, "topic": topic, "locale": locale,
        "title": title, "summary": summary,
        "published_at": stamp, "updated_at": stamp,
        "data": {
            "community_count": count, "described_count": described,
            "topic_label": topic_label, "dimensions": dimensions,
            "article": article,
            "writer_model": getattr(writer, "last_model", ""),
            "prompt_version": _PROMPT_VERSION,
            # A bounded snapshot prevents both page-weight growth and later
            # corpus edits from silently rewriting an already indexed article.
            "communities": [
                {"name": r.get("name", ""), "description": _text(r),
                 "location": r.get("location", "")}
                for r in records[:20] if r.get("name")
            ],
        },
    }, ""


def _without_model(writer, model: str):
    """The same chain with one model removed, or unchanged if that empties it.

    `build_guide_writer()` applies the day's evidence when the writer is built,
    which covers a restart but not the pass now running: at the start of a pass
    today's counters are necessarily near zero, so a model that fails every
    draft of *this* pass would otherwise keep being asked. Narrowing in-run is
    what actually stops it; the build-time filter is what remembers across a
    restart. Both fail open, for the same reason — an attempt that might be
    refused beats a day with no attempt.
    """
    primaries = getattr(writer, "primaries", None)
    if not primaries:
        return writer
    keep = [e for e in primaries if getattr(e, "model", "") != model]
    if not keep or len(keep) == len(primaries):
        return writer
    # The chain's own type, not a hard-coded one: whatever assembled this writer
    # is what should assemble the narrowed one.
    try:
        return type(writer)(primaries=keep, router=getattr(writer, "router", None))
    except TypeError:
        return writer


async def publish_daily_guides(db_path: Path, cities: list, writer, *, limit: int = 10,
                         min_communities: int = 8,
                         min_description_ratio: float = 0.6,
                         min_dimensions: int = 3,
                         country_priority: list[str] | None = None,
                         give_up_at: int = 3,
                         now: datetime | None = None) -> GuidePass:
    """Spend today's guide budget: first rewriting stale pages, then publishing new.

    Rewrites come first deliberately. A guide written by a superseded prompt is
    already public and already being read, so replacing it is worth more than
    adding an eleventh page — and it is the only way an improvement to the
    writer reaches the articles that most needed it.

    The function is restart-safe: the database counts UTC-day publications and
    rewrites, and has a unique key per site/city/topic. It intentionally does
    less than ``limit`` when the corpus cannot support that many useful pages.

    A pass is also bounded (``remaining * 2`` new drafts), and a city/topic the
    gate refuses is remembered for the day, so calling it again later tries the
    *next* candidates rather than re-buying the same refusals. The returned
    list's ``settled`` says whether another call today could do anything.
    """
    init_db(db_path)
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat()
    day = now.astimezone(timezone.utc).date().isoformat()
    published = GuidePass()
    remaining = guide_budget_left(db_path, limit, day)
    if not remaining:
        published.settled = True
        return published

    city_meta = {c.name: c for c in cities}
    written: list[dict] = []
    refused_by_model: Counter = Counter()
    refused_today = set(get_daily_counters_with_prefix(db_path, day, _REFUSED_PREFIX))

    def _refuse(city: str, topic: str):
        key = f"{city}|{topic}"
        refused_today.add(key)
        bump_daily_counter(db_path, day, _REFUSED_PREFIX + key, 1)

    def _note_refusal(model: str):
        """Stop asking a model that is failing every draft of this pass."""
        nonlocal writer
        if not model:
            return
        refused_by_model[model] += 1
        if refused_by_model[model] == give_up_at:
            narrowed = _without_model(writer, model)
            if narrowed is not writer:
                log.info("guide_writer_dropped_model", model=model,
                         refusals=refused_by_model[model])
                writer = narrowed

    # --- stale rewrites, oldest first -------------------------------------
    stale_guides = get_stale_data_guides(
        db_path, _PROMPT_VERSION, limit=remaining + len(refused_today))
    for stale in stale_guides:
        if len(written) >= remaining:
            break
        meta = city_meta.get(stale["city"])
        if not meta or f"{stale['city']}|{stale['topic']}" in refused_today:
            continue
        guide, _reason = await _draft_guide(
            db_path, stale["city"], stale["topic"], meta, writer,
            stamp=stamp, day=day, min_dimensions=min_dimensions)
        if not guide:
            _refuse(stale["city"], stale["topic"])
            _note_refusal(getattr(writer, "last_model", ""))
            continue
        # The rewrite keeps the original publication date and slug; only the
        # body, the counts and `updated_at` move.
        guide["published_at"] = stale["published_at"]
        if replace_data_guide(db_path, stale["slug"], guide):
            bump_daily_counter(db_path, day, _REWRITTEN_COUNTER, 1)
            log.info("guide_rewritten", slug=stale["slug"],
                     model=guide["data"]["writer_model"])
            written.append(guide)

    # --- new publications --------------------------------------------------
    order = country_priority or ["Hungary", "Germany", "Indonesia", "Sweden"]
    rank = {country: index for index, country in enumerate(order)}
    candidates = get_guide_candidate_groups(db_path)
    candidates.sort(key=lambda c: (
        rank.get(getattr(city_meta.get(c["city"]), "country", ""), len(rank)),
        -int(c["community_count"]), -int(c["described_count"] or 0),
        c["city"], c["topic"],
    ))
    capped = False
    attempts = 0
    for candidate in candidates:
        if len(written) >= remaining:
            break
        if attempts >= remaining * 2:
            capped = True
            break
        meta = city_meta.get(candidate["city"])
        if not meta or f"{candidate['city']}|{candidate['topic']}" in refused_today:
            continue
        attempts += 1
        guide, _reason = await _draft_guide(
            db_path, candidate["city"], candidate["topic"], meta, writer,
            stamp=stamp, day=day, min_dimensions=min_dimensions,
            min_communities=min_communities,
            min_description_ratio=min_description_ratio)
        if not guide:
            _refuse(candidate["city"], candidate["topic"])
            _note_refusal(getattr(writer, "last_model", ""))
            continue
        if create_data_guide(db_path, guide):
            published.append(guide)
            written.append(guide)
    # Budget spent, or every candidate tried without hitting the per-pass cap:
    # either way another pass today would do nothing.
    published.settled = len(written) >= remaining or not capped
    return published
