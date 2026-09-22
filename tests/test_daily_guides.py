from datetime import datetime, timezone
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from scraper.db import (bump_daily_counter, get_daily_counter,
                        get_data_guides, init_db)
from scraper.guides import (_DIMENSIONS, _MAX_WORDS, _MIN_NAMED_GROUPS,
                            _MIN_SECTION_WORDS, _PACKET_BUDGET_CHARS,
                            _decode_article, _fact_packet, _message_text,
                            publish_daily_guides, writer_system_prompt)
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state


def _article(names: list[str], language: str) -> dict:
    """A draft that a careful editor would actually file.

    Four sections with four different jobs, three groups named, no sentence or
    phrase reused. Held here because the anti-repetition gate is only as honest
    as the good article it lets through: calibrated on the ten v1 articles
    published 2026-09-20/21, which it rejects, and on this one, which it must
    not. At 328 Hungarian words it also pins the floor — see `_MIN_WORDS`.
    """
    a, b, c = (names + names + names)[:3]
    if language == "Hungarian":
        return {
            "orientation": (
                f"A városban működő csoportok nagyjából három csokorba rendeződnek. "
                f"Az első a heti rendszerességgel dolgozó, zárt tanuló társaságoké, ide tartozik a {a}. "
                f"Másféle logika szerint szerveződik a {b}, amely nyitott alkalmakat hirdet, és oda "
                f"bejelentkezés nélkül is be lehet ülni. "
                f"Harmadikként azokat érdemes külön kezelni, amelyek évadokban gondolkodnak: a {c} "
                f"leírása szerint ősszel indul és tavasszal zárul egy ciklus. "
                f"Ez a hármas felosztás nem minőségi sorrend, csupán annyit mond, hogy különböző ritmusú "
                f"elköteleződést várnak a jelentkezőtől. "
                f"Akad olyan társaság is, amelynek leírásából egyik kategória sem olvasható ki egyértelműen."),
            "practicalities": (
                "Amit a bejegyzések együtt elárulnak, az inkább a ráfordítás alakja, mint a konkrét naptár. "
                "A többség heti ritmusban gondolkodik, ami a gyakorlatban azt jelenti, hogy nem alkalmi "
                "programról van szó: a belépés hetekben mérhető szokást feltételez. "
                "A díjazásnál jóval kevesebb bejegyzés tartalmaz információt, és ahol mégis, ott jellemzően "
                "helyszíni fizetésről esik szó, nem online előfizetésről. "
                "Érdemes számolni azzal, hogy a megadott helyszín sokszor intézmény neve, nem pontos cím. "
                "Aki tömegközlekedéssel érkezne, annak ezt külön utána kell néznie."),
            "choosing_advice": (
                "Ha még soha nem próbáltad, a nyitott alkalmakat hirdető társaságok felé indulj el: ezeknél a "
                "leírás maga jelzi, hogy előzetes tudás nélkül is várnak. "
                "Akinek csak hétvégén van ideje, annak szűkebb a mezőny, mert a bejegyzések többsége hétköznap "
                "esti időpontot ad meg; ilyenkor a kevés hétvégi tételt érdemes kigyűjteni a táblázatból. "
                "Ha gyereknek keresel elfoglaltságot, a korosztályt feltüntető sorokat nézd először, mert ahol "
                "ez hiányzik, ott a felnőtt alapértelmezés a valószínűbb. "
                "Mindhárom esetben ugyanaz a következő lépés: írj a csoportnak, és kérdezd meg, mikor tudsz "
                "először beülni."),
            "gaps": (
                "Amit ez az oldal nem tud, azt jobb kimondani. "
                "Nincs adatunk arról, hogy egy társaság éppen fogad-e új tagot, és arról sem, hogy a megadott "
                "időpont a nyári hónapokban is él-e. "
                "A díjak összegét a legtöbb bejegyzés nem tartalmazza, így abból sem lehet következtetni. "
                "A leírások a csoportok saját nyilvános szövegeiből származnak, tehát azt tükrözik, ahogyan "
                "magukat bemutatják, nem külső ellenőrzést. "
                "Belépés előtt mindenképpen erősítsd meg közvetlenül náluk az időpontot és a feltételeket."),
            "used_dimensions": ["location", "fee", "skill_level"],
        }
    return {
        "orientation": (
            f"The groups here fall into roughly three kinds. "
            f"{a} belongs to the first: a settled weekly circle that expects you to keep coming back. "
            f"{b} works the other way round, advertising open sessions that anyone may drop into. "
            f"A third kind thinks in seasons rather than weeks, and {c} describes exactly such a cycle, "
            f"beginning in autumn and closing in spring. "
            f"None of this is a ranking; it only says what sort of commitment each one is shaped around. "
            f"A few listings describe themselves too briefly to place in any of the three."),
        "practicalities": (
            "Taken together, the entries say more about the shape of the commitment than about any calendar. "
            "Most think in weekly terms, which in practice means joining is a habit measured in weeks rather "
            "than a single outing you can try once and forget. "
            "Far fewer say anything about money, and those that do tend to describe paying on arrival instead "
            "of subscribing online. "
            "Bear in mind that a stated location is often the name of an institution rather than a street "
            "address. "
            "Anyone planning to arrive by public transport will have to look that up separately."),
        "choosing_advice": (
            "If you have never done this before, start from the ones advertising open sessions, because their "
            "own wording says newcomers are expected. "
            "Weekends narrow the field sharply, since most entries name a weekday evening; the handful of "
            "weekend slots are worth pulling out of the table below first. "
            "Looking for something a child can join changes the order again: read the age rows before "
            "anything else, because where that line is missing an adult default is the safer assumption. "
            "In all three cases the next step is identical — write to the group and ask when you could first "
            "sit in."),
        "gaps": (
            "It is better to be plain about what this page cannot tell you. "
            "Nothing here records whether a given group is taking new members this month, nor whether a "
            "stated time survives the summer. "
            "Most entries omit what it costs, so no inference about price is available either. "
            "Descriptions come from the groups' own public wording, which means they show how each chooses to "
            "present itself rather than anything independently checked. "
            "Confirm the time and the conditions directly with them before you set out."),
        "used_dimensions": ["location", "fee", "skill_level"],
    }


class _Writer:
    """Writes the good draft, so publication tests exercise the happy path."""

    last_model = "test-writer"

    def __init__(self):
        self.calls_made = 0

    async def completion(self, messages, **params):
        self.calls_made += 1
        packet = json.loads(messages[-1]["content"].split("\n", 1)[1])
        names = [c["name"] for c in packet["communities"]]
        article = _article(names, packet["language"])
        return {"choices": [{"message": {"content": json.dumps(article, ensure_ascii=False)}}]}


class _SloppyWriter(_Writer):
    """The v1 failure mode: every section the same filler, nobody named."""

    async def completion(self, messages, **params):
        self.calls_made += 1
        packet = json.loads(messages[-1]["content"].split("\n", 1)[1])
        filler = ("Ez az útmutató kizárólag a katalógusban rögzített adatokat értelmezi. "
                  if packet["language"] == "Hungarian" else
                  "This guide interprets only the details recorded in the directory. ")
        body = filler * 9
        article = {k: body for k in ("orientation", "practicalities", "choosing_advice", "gaps")}
        article["used_dimensions"] = ["location", "fee", "skill_level"]
        return {"choices": [{"message": {"content": json.dumps(article, ensure_ascii=False)}}]}


def _seed(db, city: CityConfig, topic: str = "running", count: int = 8) -> None:
    names = ("Aurora", "Borealis", "Canyon", "Delta", "Evergreen", "Falcon",
             "Galaxy", "Harbour", "Indigo", "Juniper")
    # Deliberately varied: a dimension now earns its card by discriminating, so
    # a fixture where every group reports the same fee and the same skill level
    # produces no comparable dimensions at all — which is exactly what the real
    # corpus does with `language`, where every Hungarian group says "Hungarian".
    fees = ("free", "2000 Ft / alkalom", "első alkalom ingyenes")
    levels = ("kezdő", "haladó", "minden szint")
    ages = ("felnőtt", "14+", "minden korosztály")
    records = [CommunityRecord(
        name=f"{city.name} {names[i]}", city=city.name, topic=topic, locale=city.locale,
        description=("A recurring open community with public joining details and "
                     "a sufficiently informative description for prospective members."),
        meeting_schedule=f"Tuesday {i}:00", location=f"Hall {i}",
        fee=fees[i % len(fees)], skill_level=levels[i % len(levels)],
        age_range=ages[i % len(ages)], language=city.locale,
        source_url=f"https://example.org/{city.name}/{i}",
        extracted_at="2026-09-20T00:00:00Z",
    ) for i in range(count)]
    save_results(city.name, topic, records, db)


def test_daily_limit_is_shared_and_country_priority_falls_through(tmp_path):
    db = tmp_path / "guides.db"
    init_db(db)
    cities = [
        CityConfig("Berlin", "de", [], "Germany"),
        CityConfig("Budapest", "hu", [], "Hungary"),
        CityConfig("Stockholm", "sv", [], "Sweden"),
    ]
    for city in cities:
        _seed(db, city)
    now = datetime(2026, 9, 20, 1, tzinfo=timezone.utc)

    created = asyncio.run(publish_daily_guides(
        db, cities, _Writer(), limit=2,
        country_priority=["Hungary", "Germany", "Sweden"], now=now))

    assert [(g["city"], g["site"]) for g in created] == [
        ("Budapest", "kozossegek"), ("Berlin", "meetapedia")]
    assert asyncio.run(publish_daily_guides(db, cities, _Writer(), limit=2, now=now)) == []
    assert len(get_data_guides(db, "kozossegek")) == 1
    assert len(get_data_guides(db, "meetapedia")) == 1
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert get_daily_counter(db, day, "guide_attempts") == 2


def test_failed_guide_call_still_records_provider_attempts(tmp_path):
    class _FailingWriter:
        calls_made = 0

        async def completion(self, messages, **params):
            self.calls_made += 2  # one routed call tried two providers
            raise RuntimeError("fleet unavailable")

    db = tmp_path / "failed-guide.db"
    city = CityConfig("Budapest", "hu", [], "Hungary")
    init_db(db)
    _seed(db, city)
    with pytest.raises(RuntimeError, match="fleet unavailable"):
        asyncio.run(publish_daily_guides(db, [city], _FailingWriter(), limit=1))

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert get_daily_counter(db, day, "guide_attempts") == 2


def test_quality_gate_does_not_force_daily_quota(tmp_path):
    db = tmp_path / "quality.db"
    init_db(db)
    city = CityConfig("Budapest", "hu", [], "Hungary")
    _seed(db, city, count=7)
    assert asyncio.run(publish_daily_guides(db, [city], _Writer(), limit=10)) == []


NAMES = ["Budapest Aurora", "Budapest Borealis", "Budapest Canyon"]


def _check(article, *, dims={"location", "fee", "skill_level"}, numbers=set(),
           names=NAMES, locale="en"):
    return _decode_article(json.dumps(article, ensure_ascii=False), dims, numbers,
                           names, locale)


def test_validator_accepts_a_genuine_draft_in_both_languages():
    for language in ("Hungarian", "English"):
        article, reason = _check(_article(NAMES, language))
        assert article, f"{language}: {reason}"
        assert article["paragraphs"] == [article[k] for k in
                                         ("orientation", "practicalities",
                                          "choosing_advice", "gaps")]
        assert article["named_groups"] >= 3


def test_validator_rejects_untraceable_numbers_and_dimensions():
    good = _article(NAMES, "English")
    assert _check({**good, "gaps": good["gaps"] + " It has 999 members."},
                  numbers={"8"})[1] == "untraceable_number"
    assert _check({**good, "used_dimensions": ["popularity"]})[1] == "bad_dimensions"
    assert _check({**good, "gaps": good["gaps"] + " See https://example.org for more."}
                  )[1] == "link_in_prose"


def test_validator_rejects_the_v1_failure_modes():
    """Each check is pinned to what it was calibrated against."""
    good = _article(NAMES, "English")

    # A sentence reused between sections — seven of the ten v1 articles.
    first = good["orientation"].split(". ")[1] + "."
    assert _check({**good, "gaps": first + " " + good["gaps"]})[1] == "repeated_sentence"

    # The sentence mill: one phrase repeated without repeating a sentence.
    mill = " ".join(f"On the question of {field} the groups differ in ways worth noting here."
                    for field in ("timing", "cost", "level", "age", "language", "access"))
    assert _check({**good, "practicalities": mill})[1] == "repeated_phrasing"

    # Naming nobody — nine of the ten v1 articles.
    anonymous = good["orientation"]
    for name in NAMES:
        anonymous = anonymous.replace(name, "one listed group")
    assert _check({**good, "orientation": anonymous})[1] == "no_named_groups"

    # A stub section, which the total word count alone would not catch.
    assert _check({**good, "gaps": "Little else is recorded here."})[1] == "thin_section"


def test_sloppy_writer_publishes_nothing_and_says_why(tmp_path):
    """The gate must fail closed: no guide, a counted reason, quota unspent."""
    db = tmp_path / "sloppy.db"
    init_db(db)
    city = CityConfig("Budapest", "hu", [], "Hungary")
    _seed(db, city)
    assert asyncio.run(publish_daily_guides(db, [city], _SloppyWriter(), limit=3)) == []
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert get_daily_counter(db, day, "guide_rejected_repeated_sentence") >= 1
    assert get_data_guides(db, "kozossegek") == []


def test_prompt_states_the_limits_the_validator_enforces():
    """A model refused for a rule it was never told costs a call to learn it."""
    prompt = writer_system_prompt()
    assert str(_MIN_SECTION_WORDS) in prompt and str(_MAX_WORDS) in prompt
    assert str(_MIN_NAMED_GROUPS) in prompt
    assert "{" not in prompt, "an unfilled placeholder reached the model"


def test_validator_rejects_a_hungarian_town_written_up_in_english():
    """Measured 2026-09-21: given a Hungarian packet, a model returned fluent
    English prose that named seven Hungarian groups and passed every other
    check. The article was competent and useless to the reader it was for.
    """
    english = _article(NAMES, "English")
    assert _check(english, locale="hu")[1] == "wrong_language"
    assert _check(_article(NAMES, "Hungarian"), locale="hu")[0]
    assert _check(english, locale="en")[0], "English must still pass on an en guide"


def test_reasoning_model_answer_is_read_from_whichever_field_carries_it():
    good = _article(NAMES, "English")
    body = json.dumps(good, ensure_ascii=False)
    assert _message_text({"choices": [{"message": {"content": body}}]}) == body
    assert _message_text(
        {"choices": [{"message": {"content": "", "reasoning": body}}]}) == body
    assert _message_text({"choices": [{"message": {}}]}) == ""


def test_fact_packet_fits_the_smallest_context_and_never_trims_the_names():
    """The v1 root cause: the community list was the tail, and the tail is what
    an overflowing context window drops — so the writer never saw a single name.
    """
    records = [{"name": f"Csoport {i} Egyesület",
                "long_description": "Hosszú, részletes leírás a közösségről. " * 12,
                "location": f"Művelődési Ház {i}"} for i in range(40)]
    dimensions = [{
        "field": field, "label": field, "covered": 30,
        "examples": [{"name": f"Csoport {j} Egyesület", "value": "heti egy alkalom, kedd este"}
                     for j in range(5)],
        "common": [{"value": "heti egy alkalom, kedd este", "count": 9} for _ in range(5)],
    } for field in _DIMENSIONS]

    packet = _fact_packet("Veszprém", "Hungary", "Színház", "hu",
                          records, dimensions, 40, 38)
    blob = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    assert len(blob) <= _PACKET_BUDGET_CHARS

    # Names survive the trim, and come before the tables that may be cut.
    assert len(packet["communities"]) >= _MIN_NAMED_GROUPS
    keys = list(packet)
    assert keys.index("communities") < keys.index("comparison_dimensions")
    assert blob.index('"communities"') < blob.index('"comparison_dimensions"')


def test_guide_routes_and_sitemaps_are_site_scoped(tmp_path, monkeypatch):
    db = tmp_path / "routes.db"
    init_db(db)
    cities = [CityConfig("Budapest", "hu", [], "Hungary"),
              CityConfig("Berlin", "de", [], "Germany")]
    for city in cities:
        _seed(db, city)
    asyncio.run(publish_daily_guides(
        db, cities, _Writer(), limit=10,
        now=datetime(2026, 9, 20, tzinfo=timezone.utc)))
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setattr(app_state, "cities", cities)
    monkeypatch.setattr(app_state, "topics", [TopicConfig("running", {})])
    client = TestClient(web_app.app)

    hu = client.get("/utmutatok/budapest-futas", headers={"host": "kozossegek.com"})
    assert hu.status_code == 200
    assert "Budapest Aurora" in hu.text
    assert '"@type": "Article"' in hu.text
    international = client.get("/guides/berlin-running", headers={"host": "meetapedia.com"})
    assert international.status_code == 200
    assert "What can you compare?" in international.text

    hu_map = client.get("/sitemap.xml", headers={"host": "kozossegek.com"}).text
    en_map = client.get("/sitemap.xml", headers={"host": "meetapedia.com"}).text
    assert "https://kozossegek.com/utmutatok/budapest-futas" in hu_map
    assert "berlin-running" not in hu_map
    assert "https://meetapedia.com/guides/berlin-running" in en_map
    assert "budapest-futas" not in en_map


def test_guide_writer_drops_models_that_keep_failing_the_prose_gate(tmp_path):
    """Measured 2026-09-21: the fleet is ordered by an extraction score, and
    writing is a different skill — a 4B scoring 73 returned a 132-word stub
    where a 120B scoring 62 wrote 359 usable words. Rather than maintain a
    second score for models that change weekly, the writer reads what the gate
    already recorded today.
    """
    from types import SimpleNamespace
    from scraper import pipeline as pipeline_mod

    db = tmp_path / "writer.db"
    init_db(db)
    day = datetime.now(timezone.utc).date().isoformat()
    fleet = [SimpleNamespace(provider="local", model="tiny-4b", quality=73),
             SimpleNamespace(provider="cloud", model="big-120b", quality=62)]

    def _fake_build(config):
        return SimpleNamespace(primaries=list(fleet), router=None, exhausted=False)

    cfg = SimpleNamespace(db_path=db)
    original = pipeline_mod.build_extractor
    pipeline_mod.build_extractor = _fake_build
    try:
        # Nothing refused yet: the whole fleet, in its own order.
        assert [e.model for e in
                pipeline_mod.build_guide_writer(cfg).primaries] == ["tiny-4b", "big-120b"]

        for _ in range(3):
            bump_daily_counter(db, day, "guide_rejected_by_tiny-4b", 1)
        assert [e.model for e in
                pipeline_mod.build_guide_writer(cfg).primaries] == ["big-120b"]

        # Fails open: with every model refused, trying beats not trying.
        for _ in range(3):
            bump_daily_counter(db, day, "guide_rejected_by_big-120b", 1)
        assert [e.model for e in
                pipeline_mod.build_guide_writer(cfg).primaries] == ["tiny-4b", "big-120b"]
    finally:
        pipeline_mod.build_extractor = original


def test_a_model_failing_every_draft_is_dropped_mid_pass(tmp_path):
    """Measured in production 2026-09-21: twelve consecutive refusals from one
    model in a single pass. `build_guide_writer` could not have stopped it —
    the writer is built when the day's counters are still zero — so the pass
    itself has to notice.
    """
    from types import SimpleNamespace

    class _Chain:
        """A two-model chain whose head always writes an unusable stub."""

        def __init__(self, primaries, router=None):
            self.primaries = primaries
            self.router = router
            self.calls_made = 0

        @property
        def last_model(self):
            return self.primaries[0].model

        async def completion(self, messages, **params):
            self.calls_made += 1
            packet = json.loads(messages[-1]["content"].split("\n", 1)[1])
            if self.primaries[0].model == "stub-4b":
                article = {k: "Túl rövid." for k in
                           ("orientation", "practicalities", "choosing_advice", "gaps")}
                article["used_dimensions"] = ["location"]
            else:
                article = _article([c["name"] for c in packet["communities"]],
                                   packet["language"])
            return {"choices": [{"message": {"content": json.dumps(article, ensure_ascii=False)}}]}

    db = tmp_path / "drop.db"
    init_db(db)
    # Several candidates, because one pass makes one attempt per candidate —
    # which is how production accumulated twelve refusals across twelve cities.
    cities = [CityConfig(name, "hu", [], "Hungary")
              for name in ("Budapest", "Debrecen", "Szeged")]
    for city in cities:
        _seed(db, city)
    fleet = [SimpleNamespace(provider="local", model="stub-4b"),
             SimpleNamespace(provider="cloud", model="able-120b")]
    chain = _Chain(fleet)

    published = asyncio.run(publish_daily_guides(
        db, cities, chain, limit=5, give_up_at=2))

    # The stub model is dropped after two refusals, and the pass then succeeds
    # instead of spending the whole budget on a model that cannot do the work.
    assert published, "the pass must recover once the failing model is dropped"
    assert published[0]["data"]["writer_model"] == "able-120b"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert get_daily_counter(db, day, "guide_rejected_by_stub-4b") == 2


def test_hungarian_guide_titles_are_grammatical():
    """Published 2026-09-21: "Nők közösségek Pécs városában". Hungarian cannot
    put one bare noun in front of another, and the title is the page's H1.
    """
    from scraper.web.i18n import (TOPIC_ADJECTIVES_HU, get_topic_labels,
                                  topic_phrase)

    labels = get_topic_labels("hu")
    assert not set(labels) - set(TOPIC_ADJECTIVES_HU), \
        "every shipped topic needs an attributive form"

    assert topic_phrase("nok", labels["nok"], "hu", "közösségek") == "női közösségek"
    assert topic_phrase("book_club", labels["book_club"], "hu",
                        "közösségek") == "könyvklub-közösségek"
    # An unknown topic degrades to formal-but-correct, never to a broken headline.
    assert topic_phrase("newly_added", "Sárkányrepülés", "hu",
                        "közösségek") == "Sárkányrepülés témájú közösségek"
    # English stacks nouns unchanged and must not be touched.
    assert topic_phrase("dance", "Dance", "en", "communities") == "Dance communities"


def test_published_hungarian_guide_uses_the_attributive_title(tmp_path):
    db = tmp_path / "title.db"
    init_db(db)
    city = CityConfig("Pécs", "hu", [], "Hungary")
    _seed(db, city, topic="dance")
    published = asyncio.run(publish_daily_guides(db, [city], _Writer(), limit=1))
    assert published and published[0]["title"].startswith("Táncos közösségek Pécs")
    assert "Tánc közösségek" not in published[0]["title"]


def _cities(*names):
    return [CityConfig(name, "hu", [], "Hungary") for name in names]


def test_the_budget_spans_passes_and_the_day_settles_when_it_is_spent(tmp_path):
    """Found in review 2026-09-22: the worker compared one pass's count with
    the limit, so a 4 + 6 day never counted as done and retried until midnight.
    """
    db = tmp_path / "span.db"
    init_db(db)
    cities = _cities("Budapest", "Debrecen", "Szeged", "Pécs")
    for city in cities:
        _seed(db, city)
    now = datetime(2026, 9, 22, 1, tzinfo=timezone.utc)

    first = asyncio.run(publish_daily_guides(db, cities[:2], _Writer(), limit=4, now=now))
    assert len(first) == 2 and first.settled, "every candidate it could see was tried"
    second = asyncio.run(publish_daily_guides(db, cities, _Writer(), limit=4, now=now))
    assert len(second) == 2 and second.settled
    third = asyncio.run(publish_daily_guides(db, cities, _Writer(), limit=4, now=now))
    assert third == [] and third.settled


def test_a_refused_candidate_is_not_redrafted_by_a_later_pass(tmp_path):
    """Each retry used to re-draft the same top candidates — real fleet calls
    for refusals already bought — and never reached the ones behind them.
    """
    db = tmp_path / "refused.db"
    init_db(db)
    cities = _cities("Budapest", "Debrecen", "Szeged", "Pécs", "Győr")
    for city in cities:
        _seed(db, city)
    now = datetime(2026, 9, 22, 1, tzinfo=timezone.utc)
    writer = _SloppyWriter()

    # limit=2 caps a pass at four drafts, so five candidates need two passes.
    first = asyncio.run(publish_daily_guides(db, cities, writer, limit=2, now=now))
    assert first == [] and not first.settled
    assert writer.calls_made == 4
    second = asyncio.run(publish_daily_guides(db, cities, writer, limit=2, now=now))
    assert writer.calls_made == 5, "only the one untried candidate is drafted"
    assert second.settled, "every candidate has now been tried today"

    # A new day forgets the refusals: the writer may have improved overnight.
    tomorrow = datetime(2026, 9, 23, 1, tzinfo=timezone.utc)
    asyncio.run(publish_daily_guides(db, cities, writer, limit=2, now=tomorrow))
    assert writer.calls_made == 9


def test_rewrites_spend_the_same_daily_budget_across_passes(tmp_path):
    """A rewrite keeps its original `published_at`, so counting publications
    alone never saw it, and every retry got a fresh ten rewrites.
    """
    import sqlite3

    db = tmp_path / "rewrites.db"
    init_db(db)
    cities = _cities("Budapest", "Debrecen", "Szeged", "Pécs")
    for city in cities:
        _seed(db, city)
    asyncio.run(publish_daily_guides(
        db, cities, _Writer(), limit=10, now=datetime(2026, 9, 20, tzinfo=timezone.utc)))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE data_guides SET data = json_set(data, '$.prompt_version', 'old')")

    now = datetime(2026, 9, 22, 1, tzinfo=timezone.utc)
    first = asyncio.run(publish_daily_guides(db, cities, _Writer(), limit=2, now=now))
    assert first.settled and first == [], "two rewrites spend a limit of two"
    asyncio.run(publish_daily_guides(db, cities, _Writer(), limit=2, now=now))
    assert get_daily_counter(db, "2026-09-22", "guide_rewritten") == 2
