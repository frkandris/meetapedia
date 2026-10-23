#!/usr/bin/env python3
"""Import every country's cities above a population threshold, in waves.

The world expansion (decided 2026-10-23): every country DataForSEO can search,
largest population first, in waves of decreasing city size — ≥100,000 people
first, later ≥15,000, then ≥1,000 — so small countries are not left behind a
single large country's long tail. Each wave imports only `pipeline.core_topics`
(`topic_tier: core`).

Two outputs, both additive:

* `config/countries.yaml` — per country: ISO code, population (Wikidata),
  DataForSEO `location_code` and the `search_language` its cities are written
  with. Read by `scraper/countries.py` for the search location and the
  population order of the worker. Regenerated whole on every run.
* new entries in `config/cities.yaml` (and `CITY_COORDS` with
  `--write-coords`). Existing entries are never rewritten; a city is skipped
  when its name or public slug is already taken, or when an existing city lies
  within 3 km — Wikidata's "New York City" is the existing "New York".

Usage
-----
    python scripts/import_world.py --min-pop 100000                # dry run
    python scripts/import_world.py --min-pop 100000 --apply --write-coords

DataForSEO's country list comes from its free `serp/google/locations`
endpoint: with `DATAFORSEO_LOGIN`/`DATAFORSEO_PASSWORD` set it is fetched,
otherwise pass `--locations FILE` (the endpoint's JSON: a `countries` list of
`{location_code, location_name, country_iso_code}`, or its raw response).
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.import_cities import (  # noqa: E402
    APP_PY, CITIES_YAML, _deaccent, _parse_point, _sparql, existing_names,
)

COUNTRIES_YAML = ROOT / "config" / "countries.yaml"

# Sovereign states and the territories Wikidata types as countries, with an
# ISO 3166-1 code. DataForSEO's list decides which of them are searchable.
_COUNTRIES_QUERY = """
SELECT ?item ?iso ?name (MAX(?p) AS ?pop) WHERE {
  ?item wdt:P297 ?iso ; wdt:P1082 ?p .
  FILTER NOT EXISTS { ?item wdt:P576 ?dissolved }
  ?item rdfs:label ?name . FILTER(LANG(?name) = "en")
} GROUP BY ?item ?iso ?name
"""

# Instances of city (Q515) or a subclass — "big city", "city of India", … —
# which keeps out the districts and wards a "human settlement" query returns
# (Seoul's 25 gu, Tokyo's wards). Metropolitan areas and conurbations are
# subclasses of city in Wikidata, so they are subtracted explicitly. Not
# "urban area" (Q702492): US cities reach it through their own class chain, and
# subtracting it left 6 cities in the United States.
_CITIES_QUERY = """
SELECT ?item ?name ?pop ?coord WHERE {
  ?item wdt:P17 wd:%s ; wdt:P1082 ?pop . FILTER(?pop >= %d)
  ?item wdt:P31/wdt:P279* wd:Q515 .
  MINUS { ?item wdt:P31/wdt:P279* wd:Q1907114 }
  MINUS { ?item wdt:P31/wdt:P279* wd:Q159313 }
  FILTER NOT EXISTS { ?item wdt:P576 ?dissolved }
  ?item rdfs:label ?name . FILTER(LANG(?name) = "en")
  OPTIONAL { ?item wdt:P625 ?coord }
  OPTIONAL { ?item rdfs:label ?local . FILTER(LANG(?local) = "%s") }
}
"""

#: Closer than this to a listed city, a "new" one is the same place under
#: another Wikidata item. 10 km also removed real ≥100k suburbs — 80 in Japan.
_SAME_PLACE_KM = 3

_NOISE = re.compile(r"(metropolitan|urban area|conurbation|agglomeration|"
                    r"\bmetro\b|\bregion\b|–)", re.I)
_TRIM = re.compile(r"^(City of|Municipality of|Kota)\s+|(-shi|-si|-gun| City)$")

#: The language a country's cities are searched in, where it is not English.
#: Chosen for where community groups publish, not for official status: India,
#: Pakistan, Nigeria or the Philippines organise in English online, while
#: Kazakhstan and Central Asia publish largely in Russian. Must be a DataForSEO
#: language (checked against `serp/google/languages` on 2026-10-23).
SEARCH_LANGUAGE: dict[str, str] = {
    **dict.fromkeys("DZ BH EG IQ JO KW LB LY MA OM QA SA TN AE YE MR".split(), "ar"),
    **dict.fromkeys("FR BE LU MC CI CM SN ML BF NE GN BJ TG CD CG GA MG HT BI "
                    "CF TD DJ KM NC PF".split(), "fr"),
    **dict.fromkeys("ES MX CO AR PE VE CL EC GT BO DO HN PY SV NI CR PA UY GQ AD".split(), "es"),
    **dict.fromkeys("BR PT AO MZ GW CV ST TL".split(), "pt"),
    **dict.fromkeys("DE AT CH LI".split(), "de"),
    **dict.fromkeys("IT SM VA".split(), "it"),
    **dict.fromkeys("NL SR CW SX BQ".split(), "nl"),
    **dict.fromkeys("KZ KG TJ UZ TM".split(), "ru"),
    "MD": "ro", "RO": "ro", "AM": "hy", "AZ": "az", "GE": "ka", "TR": "tr",
    "JP": "ja", "KR": "ko", "CN": "zh", "VN": "vi", "TH": "th", "ID": "id",
    "MY": "ms", "BN": "ms", "BD": "bn", "NP": "ne", "MM": "my", "KH": "km",
    "LA": "lo", "MN": "mn", "IL": "he", "ET": "am", "TZ": "sw", "AF": "fa",
    "SO": "so", "PL": "pl", "CZ": "cs", "SK": "sk", "HU": "hu", "BG": "bg",
    "GR": "el", "CY": "el", "RS": "sr", "ME": "sr", "HR": "hr", "SI": "sl",
    "BA": "bs", "MK": "mk", "AL": "sq", "UA": "uk", "LT": "lt", "LV": "lv",
    "EE": "et", "FI": "fi", "SE": "sv", "NO": "no", "DK": "da", "IS": "is",
}


#: DataForSEO's spelling where it is not the name a page should print.
_NAME_FIX = {"MM": "Myanmar"}


def _iso_names_from_app() -> dict[str, str]:
    """`_ISO2_COUNTRY` in app.py: the country names cities.yaml already uses
    ("Czech Republic", "UAE"). One spelling per country, or the worker would
    see two countries."""
    text = APP_PY.read_text(encoding="utf-8")
    block = text[text.index("_ISO2_COUNTRY"):]
    block = block[:block.index("}")]
    return dict(re.findall(r'"([A-Z]{2})": "([^"]+)"', block))


def _load_locations(path: str | None) -> list[dict]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        login, pw = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
        if not (login and pw):
            raise SystemExit("set DATAFORSEO_LOGIN/PASSWORD or pass --locations FILE")
        req = urllib.request.Request(
            "https://api.dataforseo.com/v3/serp/google/locations",
            headers={"Authorization": "Basic " + base64.b64encode(
                f"{login}:{pw}".encode()).decode()})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
    if "tasks" in data:
        data = {"countries": data["tasks"][0]["result"]}
    return [c for c in data["countries"] if c.get("location_type", "Country") == "Country"]


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def build_countries(locations: list[dict], existing: dict[str, str]) -> list[dict]:
    """Every DataForSEO-searchable country Wikidata knows, largest first."""
    by_iso = {c["country_iso_code"]: c for c in locations}
    wiki: dict[str, dict] = {}
    for row in _sparql(_COUNTRIES_QUERY):
        iso = row["iso"]["value"]
        pop = int(float(row["pop"]["value"]))
        if iso in by_iso and pop > wiki.get(iso, {}).get("population", -1):
            wiki[iso] = {"qid": row["item"]["value"].rsplit("/", 1)[-1],
                         "population": pop}
    out = []
    for iso, w in wiki.items():
        loc = by_iso[iso]
        out.append({
            "name": existing.get(iso) or _NAME_FIX.get(iso) or loc["location_name"],
            "iso2": iso,
            "wikidata": w["qid"],
            "population": w["population"],
            "dataforseo_location": int(loc["location_code"]),
            "search_language": SEARCH_LANGUAGE.get(iso, "en"),
        })
    out.sort(key=lambda c: -c["population"])
    return out


def write_countries(countries: list[dict]) -> None:
    lines = ["# Generated by scripts/import_world.py — do not edit by hand; re-run it.",
             "# Searchable countries (DataForSEO locations ∩ Wikidata), largest first.",
             "countries:"]
    for c in countries:
        lines.append(f'  - name: "{c["name"]}"')
        for k in ("iso2", "wikidata", "population", "dataforseo_location", "search_language"):
            v = c[k]
            lines.append(f'    {k}: "{v}"' if k == "iso2" else f"    {k}: {v}")
    COUNTRIES_YAML.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_cities(country: dict, min_pop: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    lang = country.get("search_language", "en").split("-")[0]
    for row in _sparql(_CITIES_QUERY % (country["wikidata"], min_pop, lang)):
        name = _TRIM.sub("", row["name"]["value"].strip()).strip()
        if not name or _NOISE.search(name) or name.startswith("Q") and name[1:].isdigit():
            continue
        pop = int(float(row["pop"]["value"]))
        coord = _parse_point(row.get("coord", {}).get("value", ""))
        prev = out.get(name)
        local = row.get("local", {}).get("value", "").strip()
        if prev is None or pop > prev["pop"]:
            out[name] = {"pop": pop, "coord": coord or (prev or {}).get("coord"),
                         "local": local or (prev or {}).get("local", "")}
    return out


def _existing_locales(text: str) -> dict[str, str]:
    """{country: locale} as cities.yaml already writes it, so a new city joins
    its country's existing convention (Belgium, Switzerland, India …)."""
    found: dict[str, dict[str, int]] = {}
    for m in re.finditer(r"country: (.+)\n\s+locale: (\S+)", text):
        found.setdefault(m.group(1).strip().strip('"'), {}).setdefault(m.group(2), 0)
        found[m.group(1).strip().strip('"')][m.group(2)] += 1
    return {c: max(v, key=v.get) for c, v in found.items()}


def _existing_coords() -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in re.findall(
        r'^    "[^"]+": \(([-\d.]+), ([-\d.]+)\),$', APP_PY.read_text(encoding="utf-8"), re.M)]


def render(name: str, info: dict, country: dict, locale: str) -> str:
    search_name = info.get("search_name", name)
    variants = [search_name]
    plain = _deaccent(search_name)
    if plain != search_name:
        variants.append(plain)
    variants.append(f"{search_name} {country['name']}")
    rendered = ", ".join(json.dumps(v, ensure_ascii=False) for v in variants)
    return (f"  - name: {json.dumps(name, ensure_ascii=False)}\n"
            f"    country: {json.dumps(country['name'], ensure_ascii=False)}\n"
            f"    locale: {locale}\n"
            f"    search_variants: [{rendered}]\n"
            f"    topic_tier: core\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-pop", type=int, required=True)
    ap.add_argument("--locations", help="DataForSEO locations JSON (else fetched)")
    ap.add_argument("--only", help="comma-separated ISO codes, for a trial run")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--write-coords", action="store_true")
    args = ap.parse_args()

    from scraper.identity import public_slug

    countries = build_countries(_load_locations(args.locations), _iso_names_from_app())
    print(f"{len(countries)} searchable countries; largest: "
          + ", ".join(c["name"] for c in countries[:8]))
    if args.apply:
        write_countries(countries)
        print(f"wrote {COUNTRIES_YAML}")

    text = CITIES_YAML.read_text(encoding="utf-8")
    have = existing_names(text)
    slugs = {public_slug(n) for n in have}
    coords = _existing_coords()
    locales = _existing_locales(text)
    only = set(args.only.split(",")) if args.only else None

    plan: list[tuple[dict, str, list[tuple[str, dict]]]] = []
    total = skipped_near = 0
    named = set((yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
                 .get("pipeline") or {}).get("country_priority") or [])
    for country in countries:
        if only and country["iso2"] not in only:
            continue
        if country["name"] in named:
            # Hungary, Germany, Indonesia, Sweden: imported deeper than this
            # wave by import_cities.py, under their own spelling conventions.
            print(f"  {country['name']:<28} skipped (pipeline.country_priority)")
            continue
        locale = locales.get(country["name"]) or country["search_language"]
        listed_here = set(re.findall(
            rf'- name: "?([^"\n]+)"?\n\s+country: "?{re.escape(country["name"])}"?\n', text))
        found = fetch_cities(country, args.min_pop)
        new: list[tuple[str, dict]] = []
        for name, info in sorted(found.items(), key=lambda x: -x[1]["pop"]):
            # The listed name may be the local one (Nürnberg for Nuremberg) or
            # a longer form (Ludwigshafen am Rhein).
            # Duplicates are judged within the country: Venezuela's Valencia is
            # not Spain's. A cross-country name clash reaches the slug check
            # below and is imported as "Valencia (Venezuela)".
            if name in listed_here or info.get("local") in listed_here or any(
                    h.startswith(name + " ") for h in listed_here):
                continue
            if info["coord"] and any(_km(info["coord"], c) < _SAME_PLACE_KM for c in coords):
                skipped_near += 1
                continue
            display = name
            if public_slug(display) in slugs:
                display = f"{name} ({country['name']})"
                if public_slug(display) in slugs:
                    continue
            info = {**info, "search_name": name}
            new.append((display, info))
            have.add(display)
            listed_here.add(name)
            slugs.add(public_slug(display))
            if info["coord"]:
                coords.append(info["coord"])
        total += len(new)
        print(f"  {country['name']:<28} {len(found):>4} found, {len(new):>4} new  ({locale})")
        if new:
            plan.append((country, locale, new))
    print(f"{total} new cities in {len(plan)} countries "
          f"({skipped_near} skipped as within 3 km of a listed city)")
    if not args.apply:
        print("dry run — pass --apply to write")
        return 0

    for country, locale, new in plan:
        block = "".join(render(n, i, country, locale) for n, i in new)
        marker = f"  # {country['name']}\n"
        start = text.find(marker)
        if start < 0:
            text = text.rstrip("\n") + "\n\n" + marker + block
        else:
            nxt = re.search(r"^  # ", text[start + len(marker):], re.M)
            at = start + len(marker) + nxt.start() if nxt else len(text)
            text = text[:at].rstrip("\n") + "\n" + block + ("\n" if nxt else "") + text[at:]
    CITIES_YAML.write_text(text, encoding="utf-8")
    print(f"wrote {total} entries to {CITIES_YAML}")

    if args.write_coords:
        app = APP_PY.read_text(encoding="utf-8")
        # CITY_COORDS also holds pins for cities never in cities.yaml (Detroit
        # was); a second literal for the same key is a lint error, not a pin.
        pinned = set(re.findall(r'^    "([^"]+)": \(', app, re.M))
        lines = [f"    {json.dumps(n, ensure_ascii=False)}: ({i['coord'][0]}, {i['coord'][1]}),"
                 for _, _, new in plan for n, i in new if i["coord"] and n not in pinned]
        anchor = "CITY_COORDS: dict[str, tuple[float, float]] = {"
        header = (f"\n    # World, ≥{args.min_pop} — imported from Wikidata by "
                  f"scripts/import_world.py")
        APP_PY.write_text(app.replace(anchor, anchor + header + "\n" + "\n".join(lines), 1),
                          encoding="utf-8")
        print(f"spliced {len(lines)} coordinates into CITY_COORDS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
