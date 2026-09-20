"""Publish bounded, factual guide pages from the existing community corpus."""

from collections import Counter
from datetime import datetime, timezone
import json
import re
from pathlib import Path

from .db import (count_data_guides_published_on, create_data_guide,
                 get_communities, get_guide_candidate_groups, init_db)
from .identity import public_slug
from .web.i18n import get_topic_labels

_DIMENSIONS = ("meeting_schedule", "frequency", "location", "fee",
               "skill_level", "age_range", "language", "join_process")
_DIMENSION_LABELS = {
    "hu": dict(zip(_DIMENSIONS, ("Találkozási idő", "Gyakoriság", "Helyszín",
        "Részvételi díj", "Szint", "Korosztály", "Nyelv", "Csatlakozás"))),
    "en": dict(zip(_DIMENSIONS, ("Meeting times", "Frequency", "Location",
        "Fees", "Experience level", "Age range", "Language", "How to join"))),
}

_WRITER_SYSTEM = """You are the careful editor of a community directory.
Write a genuinely useful local guide for someone choosing a group to join.

Grounding rules:
- Use ONLY the supplied FACT_PACKET. Never invent, infer, or generalize facts.
- Do not claim first-hand experience, rankings, popularity, quality, safety, or that one group is best.
- When data is absent, say it is not stated; do not fill the gap.
- Distinguish directory facts from practical advice. Advice must be conditional and generic.
- Do not mention SEO, keywords, the prompt, or these instructions.
- Avoid repetitive filler and avoid paraphrasing every card one by one.
- Use the requested language naturally and make the article helpful without search traffic.

Return one JSON object only, with string fields: introduction, comparison,
choosing_advice, conclusion. The four body sections together must
be 350-700 words. Use plain text paragraphs, no Markdown headings or links.
Before returning, silently verify every number and named-group claim against the packet."""


def _decode_article(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    fields = ("introduction", "comparison", "choosing_advice", "conclusion")
    if not isinstance(obj, dict) or any(not isinstance(obj.get(k), str) for k in fields):
        return None
    word_count = len(" ".join(obj[k] for k in fields).split())
    if not 300 <= word_count <= 800:
        return None
    obj["word_count"] = word_count
    return obj


def _text(record: dict) -> str:
    return (record.get("long_description") or record.get("description")
            or record.get("short_description") or "").strip()


def _dimension_sections(records: list[dict], locale: str,
                        minimum_values: int = 2) -> list[dict]:
    sections = []
    for field in _DIMENSIONS:
        values = [(r.get("name", ""), str(r.get(field) or "").strip()) for r in records]
        values = [(name, value) for name, value in values if name and value]
        if len(values) < minimum_values:
            continue
        common = Counter(value for _, value in values).most_common(5)
        sections.append({
            "field": field,
            "label": _DIMENSION_LABELS[locale][field],
            "covered": len(values),
            "examples": [{"name": name, "value": value} for name, value in values[:5]],
            "common": [{"value": value, "count": count} for value, count in common],
        })
    return sections


async def publish_daily_guides(db_path: Path, cities: list, writer, *, limit: int = 10,
                         min_communities: int = 8,
                         min_description_ratio: float = 0.6,
                         min_dimensions: int = 3,
                         country_priority: list[str] | None = None,
                         now: datetime | None = None) -> list[dict]:
    """Publish today's remaining quota of quality-gated guides.

    The function is restart-safe: the database counts UTC-day publications and
    has a unique key per site/city/topic. It intentionally publishes fewer than
    ``limit`` when the corpus cannot support ten useful pages.
    """
    init_db(db_path)
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat()
    remaining = max(0, limit - count_data_guides_published_on(
        db_path, now.astimezone(timezone.utc).date().isoformat()))
    if not remaining:
        return []

    city_meta = {c.name: c for c in cities}
    order = country_priority or ["Hungary", "Germany", "Indonesia", "Sweden"]
    rank = {country: index for index, country in enumerate(order)}
    candidates = get_guide_candidate_groups(db_path)
    candidates.sort(key=lambda c: (
        rank.get(getattr(city_meta.get(c["city"]), "country", ""), len(rank)),
        -int(c["community_count"]), -int(c["described_count"] or 0),
        c["city"], c["topic"],
    ))
    published = []
    attempts = 0
    for candidate in candidates:
        if len(published) >= remaining:
            break
        if attempts >= remaining * 2:
            break
        city, topic = candidate["city"], candidate["topic"]
        count = int(candidate["community_count"])
        described = int(candidate["described_count"] or 0)
        meta = city_meta.get(city)
        if not meta or count < min_communities:
            continue
        if described / count < min_description_ratio:
            continue

        records = sorted(get_communities(db_path, city, topic),
                         key=lambda r: (not bool(_text(r)), r.get("name", "").casefold()))
        is_hu = meta.country == "Hungary"
        locale = "hu" if is_hu else "en"
        dimensions = _dimension_sections(records, locale)
        if len(dimensions) < min_dimensions:
            continue
        labels = get_topic_labels(locale)
        topic_label = labels.get(topic, topic.replace("_", " ").title())
        if is_hu:
            title = f"{topic_label} közösségek {city} városában: {count} lehetőség"
            summary = (
                f"Adatainkban jelenleg {count} {topic_label.lower()} közösség szerepel "
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
        packet = {
            "language": "Hungarian" if is_hu else "English",
            "reader_goal": "Choose a suitable community to contact or visit",
            "city": city, "country": meta.country, "topic": topic_label,
            "community_count": count, "described_count": described,
            "comparison_dimensions": dimensions,
            "communities": [
                {"name": r.get("name", ""), "description": _text(r)[:280],
                 "location": r.get("location", "")}
                for r in records[:20] if r.get("name")
            ],
        }
        attempts += 1
        response = await writer.completion([
            {"role": "system", "content": _WRITER_SYSTEM},
            {"role": "user", "content": "FACT_PACKET:\n" + json.dumps(
                packet, ensure_ascii=False, separators=(",", ":"))},
        ], temperature=0.3, max_tokens=1400,
           response_format={"type": "json_object"})
        raw = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        article = _decode_article(raw)
        if not article:
            continue
        guide = {
            "slug": f"{public_slug(city)}-{public_slug(topic_label)}",
            "site": "kozossegek" if is_hu else "meetapedia",
            "city": city,
            "topic": topic,
            "locale": locale,
            "title": title,
            "summary": summary,
            "published_at": stamp,
            "updated_at": stamp,
            "data": {
                "community_count": count,
                "described_count": described,
                "topic_label": topic_label,
                "dimensions": dimensions,
                "article": article,
                "writer_model": getattr(writer, "last_model", ""),
                # A bounded snapshot prevents both page-weight growth and later
                # corpus edits from silently rewriting an already indexed article.
                "communities": [
                    {"name": r.get("name", ""), "description": _text(r),
                     "location": r.get("location", "")}
                    for r in records[:20] if r.get("name")
                ],
            },
        }
        if create_data_guide(db_path, guide):
            published.append(guide)
    return published
