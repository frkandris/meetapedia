"""The guide page's render-time rules, each pinned to what it was found by."""

from scraper.web.app import _comparable_dimensions, _link_communities


COMMUNITIES = [
    {"name": "Ambassador Club Pécs"},
    {"name": "Ambassador Club Mecsek (Regionális)"},
    {"name": "Borostyán Egyesület"},
    {"name": "Kör"},  # too short to match safely
]


def test_named_groups_in_the_prose_become_links():
    """The validator refuses a draft naming fewer than three groups; leaving
    those names as flat text wastes the requirement.
    """
    text = "Ide tartozik a Borostyán Egyesület és az Ambassador Club Pécs is."
    out = str(_link_communities(text, COMMUNITIES, "Pécs"))
    assert 'href="/pecs/borostyan-egyesulet"' in out
    assert 'href="/pecs/ambassador-club-pecs"' in out


def test_the_longest_matching_name_wins():
    """"Ambassador Club Pécs" sits inside no other name, but "Ambassador Club
    Mecsek (Regionális)" contains a shorter candidate — the long one must win,
    or the link points at a group the sentence is not talking about.
    """
    text = "A harmadik az Ambassador Club Mecsek (Regionális)."
    out = str(_link_communities(text, COMMUNITIES, "Pécs"))
    assert "Ambassador Club Mecsek (Regionális)</a>" in out
    assert out.count("<a ") == 1


def test_model_prose_is_escaped_even_while_being_linked():
    """This filter is the one place model output becomes markup."""
    text = 'Egy csoport: Borostyán Egyesület <script>alert("x")</script>'
    out = str(_link_communities(text, COMMUNITIES, "Pécs"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert 'href="/pecs/borostyan-egyesulet"' in out


def test_text_without_known_names_is_returned_escaped_and_unlinked():
    out = str(_link_communities("Semmi ismerős név & egy jel.", COMMUNITIES, "Pécs"))
    assert "<a " not in out
    assert "&amp;" in out
    assert str(_link_communities("", COMMUNITIES, "Pécs")) == ""


def test_a_dimension_nearly_everyone_answers_identically_is_dropped():
    """The real numbers from the Pécs guide, 2026-09-21. "Nyelv" led the
    section with 42 of 44 groups saying "Hungarian" and two saying "Magyar" —
    one fact written two ways by the extractor, which a distinctness test
    passes and a reader gets nothing from.
    """
    language = {"field": "language", "common": [{"value": "Hungarian", "count": 42},
                                                {"value": "Magyar", "count": 2}]}
    frequency = {"field": "frequency", "common": [{"value": "Havi", "count": 1},
                                                  {"value": "Telihold", "count": 1}]}
    uniform = {"field": "fee", "common": [{"value": "ingyenes", "count": 9}]}

    assert _comparable_dimensions([language, frequency, uniform]) == [frequency]
    assert _comparable_dimensions([uniform]) == []
    assert _comparable_dimensions(None) == []

    # A field just under the threshold is still a comparison.
    close = {"field": "fee", "common": [{"value": "ingyenes", "count": 8},
                                        {"value": "2000 Ft", "count": 2}]}
    assert _comparable_dimensions([close]) == [close]


def test_dimension_sections_carry_their_denominator_and_sort_by_coverage():
    from scraper.guides import _dimension_sections
    records = [{"name": f"Csoport {i}", "fee": f"{i} Ft", "location": "Ház"}
               for i in range(5)]
    records += [{"name": f"Néma {i}"} for i in range(15)]
    sections = _dimension_sections(records, "hu")
    fee = next(s for s in sections if s["field"] == "fee")
    assert fee["covered"] == 5 and fee["total"] == 20
    # "location" is reported by five groups and every value is "Ház": dropped.
    assert not [s for s in sections if s["field"] == "location"]
    assert sections == sorted(sections, key=lambda d: -d["covered"])


def test_a_dominant_value_is_dropped_when_the_guide_is_built_too():
    """Both ends of the rule, so a stored guide and a fresh one agree."""
    from scraper.guides import _dimension_sections

    records = [{"name": f"Csoport {i}", "language": "Hungarian"} for i in range(19)]
    records.append({"name": "Kivétel", "language": "Magyar"})
    assert not [s for s in _dimension_sections(records, "hu")
                if s["field"] == "language"]


def test_a_name_is_linked_only_as_a_whole_word():
    """Hungarian inflects by suffix: "Futókör" must not link inside "futókörök"."""
    groups = [{"name": "Jóga"}, {"name": "Futókör"}]
    out = str(_link_communities("A Jógaoktatás és a Jóga, valamint a Futókörök.",
                                groups, "Pécs"))
    assert out.count("<a ") == 1
    assert ">Jóga</a>," in out and "Jógaoktatás" in out


def test_dominance_is_measured_against_everyone_who_answered():
    """`common` keeps the top five values only. 40 of 80 is half, not 40 of 44."""
    spread = {"field": "fee", "covered": 80,
              "common": [{"value": "ingyenes", "count": 40}] +
                        [{"value": f"{i} Ft", "count": 1} for i in range(4)]}
    assert _comparable_dimensions([spread]) == [spread]


def test_an_older_guide_gets_its_language_values_merged_on_render():
    """Stored before normalisation: "Hungarian" and "Magyar" as two values."""
    language = {"field": "language", "covered": 44,
                "common": [{"value": "Hungarian", "count": 40},
                           {"value": "Magyar", "count": 4}],
                "examples": [{"name": "A", "value": "Hungarian"}]}
    assert _comparable_dimensions([language], "hu") == []

    mixed = {**language, "common": [{"value": "Hungarian", "count": 20},
                                    {"value": "Magyar", "count": 10},
                                    {"value": "English", "count": 14}]}
    [shown] = _comparable_dimensions([mixed], "hu")
    assert [(e["value"], e["count"]) for e in shown["common"]] == [
        ("magyar", 30), ("angol", 14)]
    assert shown["examples"][0]["value"] == "magyar"
