#!/usr/bin/env python3
"""Give large cities `search_districts` in config/cities.yaml — the district pilot.

Every city got the same search: 36 topics, five fetched pages each. London
came out with 62 communities, Lagos 31, against Berlin's 228 and Budapest's 315
(2026-10-23, baseline in docs/wiki/pages/decisions/district-search.md). A city
of millions is searched district by district instead: one extra query per
district per topic, two extra fetch slots each (scraper/pipeline.py,
`district_queries`, `_pages_for`).

The lists are written out here because Wikidata's "contains administrative
territorial entity" (P150) is empty for most of these cities (checked
2026-10-23: London, Los Angeles, Chicago, Toronto, Lagos, Cairo, Mumbai). They
are boroughs where a city has them (London's 32 + the City, New York's five,
Beijing's 16 from Wikidata in Chinese, Delhi's districts from Wikidata), and
otherwise well-known districts and neighbourhoods inside the city limits.

Idempotent: a city that already has `search_districts` is left alone unless
--replace.

    python scripts/set_search_districts.py            # dry run
    python scripts/set_search_districts.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

CITIES_YAML = Path(__file__).resolve().parent.parent / "config" / "cities.yaml"

DISTRICTS: dict[str, list[str]] = {
    "London": [
        "City of London", "Barking and Dagenham", "Barnet", "Bexley", "Brent", "Bromley",
        "Camden", "Croydon", "Ealing", "Enfield", "Greenwich", "Hackney",
        "Hammersmith and Fulham", "Haringey", "Harrow", "Havering", "Hillingdon",
        "Hounslow", "Islington", "Kensington and Chelsea", "Kingston upon Thames",
        "Lambeth", "Lewisham", "Merton", "Newham", "Redbridge", "Richmond upon Thames",
        "Southwark", "Sutton", "Tower Hamlets", "Waltham Forest", "Wandsworth",
        "Westminster",
    ],
    "New York": ["Manhattan", "Brooklyn", "Queens", "The Bronx", "Staten Island"],
    "Los Angeles": [
        "Hollywood", "Downtown", "Venice", "Silver Lake", "Koreatown", "Echo Park",
        "Westwood", "San Pedro", "Van Nuys", "North Hollywood", "Studio City",
        "Eagle Rock", "Highland Park", "Boyle Heights", "Sherman Oaks", "Encino",
        "Woodland Hills", "Westchester", "Mid-City", "Palms",
    ],
    "Chicago": [
        "Lincoln Park", "Lakeview", "Logan Square", "Wicker Park", "Hyde Park", "Pilsen",
        "Uptown", "Rogers Park", "Andersonville", "Bridgeport", "Bronzeville",
        "Edgewater", "West Loop", "South Loop", "Humboldt Park", "Avondale",
        "Albany Park", "Beverly", "Chinatown", "Ukrainian Village",
    ],
    "Toronto": [
        "Scarborough", "North York", "Etobicoke", "East York", "York", "Downtown",
        "The Annex", "Leslieville", "Liberty Village", "The Beaches", "Kensington Market",
        "Danforth", "High Park", "Yorkville", "Riverdale", "The Junction",
    ],
    "Lagos": [
        "Ikeja", "Lekki", "Victoria Island", "Ikoyi", "Surulere", "Yaba", "Ajah",
        "Festac Town", "Ikorodu", "Apapa", "Oshodi", "Agege", "Gbagada", "Maryland",
        "Magodo", "Ogba", "Mushin", "Badagry",
    ],
    "Cairo": [
        "Nasr City", "Heliopolis", "Maadi", "Zamalek", "Dokki", "Downtown", "New Cairo",
        "Shubra", "Abbassia", "Garden City", "Mokattam", "Helwan", "Sheikh Zayed",
        "6th of October", "Mohandessin",
    ],
    "Delhi": [
        "Central Delhi", "East Delhi", "North Delhi", "North East Delhi", "North West Delhi",
        "South Delhi", "South East Delhi", "South West Delhi", "West Delhi", "Shahdara",
        "Connaught Place", "Hauz Khas", "Saket", "Dwarka", "Rohini", "Lajpat Nagar",
    ],
    "Mumbai": [
        "Andheri", "Bandra", "Juhu", "Powai", "Colaba", "Dadar", "Borivali", "Malad",
        "Goregaon", "Chembur", "Kurla", "Vile Parle", "Worli", "Lower Parel",
        "Ghatkopar", "Mulund",
    ],
    "Beijing": [
        "东城区", "西城区", "朝阳区", "丰台区", "石景山区", "海淀区", "门头沟区", "房山区",
        "通州区", "顺义区", "昌平区", "大兴区", "怀柔区", "平谷区", "密云区", "延庆区",
    ],
}


def apply(text: str, replace: bool) -> tuple[str, list[str]]:
    done = []
    for city, districts in DISTRICTS.items():
        m = re.search(rf'^  - name: "?{re.escape(city)}"?\n((?:    .+\n)+)', text, re.M)
        if not m:
            raise SystemExit(f"{city!r} not found in {CITIES_YAML}")
        block = m.group(1)
        line = f"    search_districts: {json.dumps(districts, ensure_ascii=False)}\n"
        if "search_districts:" in block:
            if not replace:
                continue
            block = re.sub(r"    search_districts: .*\n", line, block)
        else:
            block += line
        text = text[:m.start(1)] + block + text[m.end(1):]
        done.append(f"{city} ({len(districts)})")
    return text, done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--replace", action="store_true")
    args = ap.parse_args()
    text, done = apply(CITIES_YAML.read_text(encoding="utf-8"), args.replace)
    total = sum(len(DISTRICTS[c.split(" (")[0]]) for c in done)
    print(f"{len(done)} cities, {total} districts: {', '.join(done)}")
    if args.apply and done:
        CITIES_YAML.write_text(text, encoding="utf-8")
        print(f"wrote {CITIES_YAML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
