import json
from typing import Union

from ..models import CommunityRecord

_TOPIC_TYPE: dict[str, str] = {
    "running": "SportsClub",
    "cycling": "SportsClub",
    "hiking": "SportsClub",
    "swimming": "SportsClub",
    "yoga": "SportsClub",
    "martial_arts": "SportsClub",
    "fitness": "SportsClub",
    "choir": "MusicGroup",
    "music": "MusicGroup",
    "dance": "DanceGroup",
    "theater": "PerformingGroup",
}

_DEFAULT_TYPE = "Organization"


def community_to_schema(record: Union["CommunityRecord", dict],
                        page_url: str | None = None) -> dict:
    """One community as a schema.org object.

    Only fields the record actually carries are emitted. Nothing here infers:
    a group with no stated founding year has no `foundingDate`, because a
    guessed one is a wrong answer given confidently, and structured data is
    read by machines that cannot tell the difference.

    `page_url` is our own page for this community. It becomes
    `mainEntityOfPage` and, when there is no site of the group's own, `@id` —
    never `url`, which belongs to the organisation's own web presence.
    """
    if isinstance(record, dict):
        try:
            record = CommunityRecord.model_validate(record)
        except Exception:
            return {}

    schema_type = _TOPIC_TYPE.get(record.topic, _DEFAULT_TYPE)
    obj: dict = {"@type": schema_type, "name": record.name}

    if page_url:
        obj["@id"] = page_url
        obj["mainEntityOfPage"] = page_url

    # long_description is the enriched body, short_description the one-liner,
    # description the extractor's own. Richest first — Google reads one.
    description = (record.long_description or record.description
                   or record.short_description)
    if description:
        obj["description"] = description
    if record.website:
        obj["url"] = record.website

    # `location` is whatever the extractor read off the page: a street
    # address sometimes, but just as often a venue name, a district, or
    # "online". It is the Place's *name*, never `streetAddress` — writing an
    # unvalidated string into a postal field is the inference this module
    # refuses to make everywhere else. The city is the only part we know to be
    # an administrative locality, because it is the pair we searched for.
    loc: dict = {"@type": "Place", "addressLocality": record.city,
                 "address": {"@type": "PostalAddress",
                             "addressLocality": record.city}}
    if record.location:
        loc["name"] = record.location
    obj["location"] = loc
    obj["areaServed"] = {"@type": "Place", "name": record.city}

    if record.email:
        obj["email"] = record.email
    if record.phone:
        obj["telephone"] = record.phone
    if record.contact:
        obj["contactPoint"] = {
            "@type": "ContactPoint",
            "contactType": "general",
            "description": record.contact,
        }

    if record.social_links:
        obj["sameAs"] = record.social_links

    if record.meeting_schedule:
        obj["openingHoursSpecification"] = {
            "@type": "OpeningHoursSpecification",
            "description": record.meeting_schedule,
        }

    if record.founding_year:
        obj["foundingDate"] = str(record.founding_year)
    if record.tags:
        obj["keywords"] = ", ".join(record.tags)
    if record.language:
        obj["knowsLanguage"] = record.language
    if record.leader:
        # The extractor stores "Name, role" or just a name. Both are a
        # person's label; the split into name and jobTitle happens in
        # pipeline._parse_leader_field, and repeating that guess here would
        # let the two drift apart.
        obj["member"] = {"@type": "Person", "name": record.leader}

    return obj


def breadcrumb_jsonld(items: list) -> str:
    """BreadcrumbList JSON-LD from a list of {"name", "url"} dicts (absolute URLs).

    Gives Google the site hierarchy (home → city → topic → community) for SERP
    breadcrumb display and reinforces internal structure. Empty string if <2 items.
    """
    items = [it for it in (items or []) if it.get("name") and it.get("url")]
    if len(items) < 2:
        return ""
    ld = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": it["name"], "item": it["url"]}
            for i, it in enumerate(items, 1)
        ],
    }
    return json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")


def records_to_jsonld(records: list, page_url: str | None = None) -> str:
    """Convert CommunityRecord objects or dicts to a JSON-LD string.

    `page_url` is passed through only for a single-record page; on a listing it
    would claim that every community on it is the page's main entity.
    """
    single = page_url if len(records) == 1 else None
    items = [item for r in records if (item := community_to_schema(r, single))]
    if not items:
        return ""
    ld = {
        "@context": "https://schema.org",
        "@graph": items,
    }
    return json.dumps(ld, ensure_ascii=False, indent=2).replace("</", "<\\/")


#: schema.org types for the venue_type values the extractor is allowed to
#: produce (see VENUE_SYSTEM_PROMPT). Anything else stays a bare Place rather
#: than being forced into a type that would claim more than we know.
_VENUE_TYPE: dict[str, str] = {
    "café": "CafeOrCoffeeShop",
    "cafe": "CafeOrCoffeeShop",
    "bar": "BarOrPub",
    "park": "Park",
    "cultural_center": "CivicStructure",
    "library": "Library",
    "church": "PlaceOfWorship",
    "sports_hall": "SportsActivityLocation",
    "studio": "CivicStructure",
    "coworking": "LocalBusiness",
    "restaurant": "Restaurant",
}


def venue_to_schema(venue: dict, page_url: str | None = None) -> dict:
    """One venue as a schema.org Place.

    Venue pages carried no structured data at all until 2026-09-20, which is
    the page type where it is worth the most: a named physical place in a named
    town is exactly the shape local search understands.
    """
    if not isinstance(venue, dict) or not venue.get("name"):
        return {}
    obj: dict = {
        "@type": _VENUE_TYPE.get((venue.get("venue_type") or "").lower(), "Place"),
        "name": venue["name"],
    }
    if page_url:
        obj["@id"] = page_url
        obj["mainEntityOfPage"] = page_url
    if venue.get("description"):
        obj["description"] = venue["description"]
    if venue.get("website"):
        obj["url"] = venue["website"]

    address: dict = {"@type": "PostalAddress"}
    if venue.get("address"):
        address["streetAddress"] = venue["address"]
    if venue.get("city"):
        address["addressLocality"] = venue["city"]
    if len(address) > 1:
        obj["address"] = address

    if venue.get("email"):
        obj["email"] = venue["email"]
    if venue.get("phone"):
        obj["telephone"] = venue["phone"]
    if venue.get("social_links"):
        obj["sameAs"] = list(venue["social_links"])
    return obj


def person_to_schema(person: dict, communities: list | None = None,
                     page_url: str | None = None) -> dict:
    """One person as a schema.org Person.

    `communities` are the groups this person leads or instructs, as
    `{"name", "url"}` dicts — the same list the page renders, so the markup and
    the visible page cannot disagree.
    """
    if not isinstance(person, dict) or not person.get("name"):
        return {}
    obj: dict = {"@type": "Person", "name": person["name"]}
    if page_url:
        obj["@id"] = page_url
        obj["mainEntityOfPage"] = page_url
    if person.get("bio"):
        obj["description"] = person["bio"]
    if person.get("website"):
        obj["url"] = person["website"]
    if person.get("email"):
        obj["email"] = person["email"]
    if person.get("social_links"):
        obj["sameAs"] = list(person["social_links"])
    if person.get("city"):
        obj["homeLocation"] = {"@type": "Place", "name": person["city"]}
    if person.get("role"):
        obj["jobTitle"] = person["role"]

    member_of = [
        {"@type": "Organization", "name": c["name"], "url": c["url"]}
        for c in (communities or [])
        if c.get("name") and c.get("url")
    ]
    if member_of:
        obj["memberOf"] = member_of
    return obj


def site_jsonld(site_name: str, site_url: str, search_path: str,
                description: str = "") -> str:
    """WebSite + SearchAction + Organization for the home page.

    The search action is the one piece here that can appear in a result: it
    tells Google the site has its own search and how to call it, which is what
    a sitelinks search box is built from. `search_path` must be the real route
    — `/kereses` or `/search` depending on the edition.
    """
    if not site_name or not site_url:
        return ""
    base = site_url.rstrip("/")
    website: dict = {
        "@type": "WebSite",
        "@id": f"{base}/#website",
        "name": site_name,
        "url": base + "/",
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{base}{search_path}?q={{search_term_string}}",
            },
            "query-input": "required name=search_term_string",
        },
    }
    organization: dict = {
        "@type": "Organization",
        "@id": f"{base}/#organization",
        "name": site_name,
        "url": base + "/",
    }
    if description:
        website["description"] = description
        organization["description"] = description
    website["publisher"] = {"@id": organization["@id"]}
    ld = {"@context": "https://schema.org", "@graph": [website, organization]}
    return json.dumps(ld, ensure_ascii=False, indent=2).replace("</", "<\\/")


def venue_jsonld(venue: dict, page_url: str | None = None) -> str:
    obj = venue_to_schema(venue, page_url)
    if not obj:
        return ""
    return json.dumps({"@context": "https://schema.org", **obj},
                      ensure_ascii=False, indent=2).replace("</", "<\\/")


def person_jsonld(person: dict, communities: list | None = None,
                  page_url: str | None = None) -> str:
    obj = person_to_schema(person, communities, page_url)
    if not obj:
        return ""
    return json.dumps({"@context": "https://schema.org", **obj},
                      ensure_ascii=False, indent=2).replace("</", "<\\/")


def article_jsonld(title: str, summary: str, page_url: str,
                   published_at: str, updated_at: str, site_name: str) -> str:
    """Structured data for one materialized, data-derived guide."""
    obj = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": summary,
        "url": page_url,
        "mainEntityOfPage": page_url,
        "datePublished": published_at,
        "dateModified": updated_at,
        "publisher": {"@type": "Organization", "name": site_name},
    }
    return json.dumps(obj, ensure_ascii=False, indent=2).replace("</", "<\\/")
