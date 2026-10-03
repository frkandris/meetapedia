import pytest

from scraper.models import CommunityRecord, PersonRecord


def make_record(**overrides) -> CommunityRecord:
    data = {
        "name": "Budapest Runners",
        "topic": "running",
        "city": "Budapest",
        "locale": "hu",
        "source_url": "https://example.com/source",
        "extracted_at": "2026-01-01T00:00:00+00:00",
    }
    data.update(overrides)
    return CommunityRecord(**data)


def test_community_record_normalizes_links_and_nullish_values():
    record = make_record(
        website="example.com",
        social_links=["https://facebook.com/group", "not a url"],
        contact="Nincs megadva",
        email="not-an-email",
        phone="call us",
        tags=[" trail ", "trail", "road", "tempo", "social", "weekly", "city", "park", "extra"],
    )

    assert record.website == "https://example.com"
    assert record.social_links == ["https://facebook.com/group"]
    assert record.contact is None
    assert record.email is None
    assert record.phone is None
    assert record.tags == ["trail", "road", "tempo", "social", "weekly", "city", "park", "extra"]
    assert record.source_urls[0] == "https://example.com/source"


def test_community_id_is_stable_for_name_and_city():
    first = make_record(topic="running")
    second = make_record(topic="cycling")

    assert first.community_id == second.community_id


@pytest.mark.parametrize("leader", [
    "Jane Smith, conductor", "Jane Smith", "Not specified", "Local organizers",
    "Az alapítvány vezetői",
])
def test_a_leader_that_names_nobody_is_dropped(leader):
    """'Jane Smith, conductor' is the extraction prompt's example, copied."""
    r = CommunityRecord(name="Kórus", topic="music", city="Pécs", locale="hu",
                        source_url="https://a.test", extracted_at="2026-01-01", leader=leader)
    assert r.leader is None


@pytest.mark.parametrize("leader", [
    "Kovács János, karnagy", "Dr. Nagy Éva", "Anna Svensson (ordförande)",
])
def test_a_real_leader_is_kept(leader):
    r = CommunityRecord(name="Kórus", topic="music", city="Pécs", locale="hu",
                        source_url="https://a.test", extracted_at="2026-01-01", leader=leader)
    assert r.leader == leader


@pytest.mark.parametrize("name", [
    "Jane Smith", "Not specified", "Local community leaders", "Liszt-díjas karnagy)",
])
def test_a_placeholder_person_is_refused(name):
    with pytest.raises(ValueError):
        PersonRecord(name=name, role="leader", city="Pécs", topic="music",
                     community_name="Kórus", source_url="https://a.test",
                     extracted_at="2026-01-01")
