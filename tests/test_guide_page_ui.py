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


def test_a_dimension_where_every_group_says_the_same_thing_is_dropped():
    """Published 2026-09-21: the most prominent card was "Nyelv", where all 44
    groups said "Hungarian". A comparison whose every row is identical compares
    nothing, and it pushed the fields that did vary further down.
    """
    uniform = {"field": "language", "common": [{"value": "Hungarian", "count": 44}]}
    varied = {"field": "fee", "common": [{"value": "ingyenes", "count": 9},
                                         {"value": "2000 Ft", "count": 3}]}
    assert _comparable_dimensions([uniform, varied]) == [varied]
    assert _comparable_dimensions([uniform]) == []
    assert _comparable_dimensions(None) == []


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
