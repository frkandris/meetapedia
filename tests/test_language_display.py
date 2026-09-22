"""The `language` field is written three ways for one language; this fixes it.

Measured over the 45,546 filled values in production on 2026-09-22: Hungarian
appears as "Magyar" (10,413), "Hungarian" (5,420) and "magyar" (778); German as
"Deutsch" (10,796) and "German" (3,252); Japanese as "Japanese" and "日本語".
Each source page writes it its own way, and the extractor records what it read.
"""

from scraper.guides import _dimension_sections
from scraper.web.i18n import display_language, display_languages


def test_one_language_written_three_ways_reads_the_same():
    for stored in ("Magyar", "Hungarian", "magyar", "  MAGYAR  "):
        assert display_language(stored, "hu") == "magyar"
        assert display_language(stored, "en") == "Hungarian"


def test_the_endonym_and_the_english_name_are_the_same_language():
    assert display_language("Deutsch", "hu") == display_language("German", "hu")
    assert display_language("日本語", "en") == "Japanese"
    assert display_language("Svenska", "hu") == "svéd"


def test_an_unknown_language_passes_through_untouched():
    """Dropping it loses what the source said; guessing invents it."""
    assert display_language("Klingon", "hu") == "Klingon"
    assert display_language("", "hu") == ""
    assert display_language(None, "hu") == ""


def test_a_multi_language_value_is_split_and_deduplicated():
    assert display_languages("Magyar, English és Deutsch", "hu") == "magyar, angol, német"
    # The same language twice, spelled two ways, is one language.
    assert display_languages("Hungarian, Magyar", "hu") == "magyar"
    assert display_languages("Magyar / Deutsch", "en") == "Hungarian, German"


def test_three_spellings_stop_looking_like_variation_in_a_comparison():
    """The spellings defeated the comparison rules: a field everyone answers
    identically looked varied because they spelled the answer differently.
    """
    records = [{"name": f"Csoport {i}",
                "language": ("Magyar", "Hungarian", "magyar")[i % 3]}
               for i in range(20)]
    assert not [d for d in _dimension_sections(records, "hu")
                if d["field"] == "language"]


def test_a_genuinely_bilingual_place_keeps_its_language_card():
    records = [{"name": f"Csoport {i}",
                "language": ("Magyar", "Hungarian", "Deutsch", "German")[i % 4]}
               for i in range(20)]
    language = next(d for d in _dimension_sections(records, "hu")
                    if d["field"] == "language")
    assert [(v["value"], v["count"]) for v in language["common"]] == [
        ("magyar", 10), ("német", 10)]
