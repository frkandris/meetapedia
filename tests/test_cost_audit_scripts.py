import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_inline_enrichment_report_counts_value(tmp_path):
    module = _load("report_inline_enrichment")
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cache_pages (data TEXT)")
        conn.execute("INSERT INTO cache_pages VALUES (?)", (json.dumps({
            "enrich_log": [
                {"search_query": "a", "research_urls": [{"fetched": True}],
                 "fields_added": ["website"]},
                {"search_query": "b", "research_urls": [{"fetched": False}],
                 "fields_added": []},
            ]
        }),))
    result = module.measure(db)
    assert result["attempts_and_searches"] == 2
    assert result["approx_llm_calls"] == 1
    assert result["success_rate"] == 0.5
    assert result["fields_added"] == {"website": 1}


def _gate_db(path: Path, pages: int = 30, extracted: bool = True) -> Path:
    """A cache_pages table shaped like production's, including the sentinel.

    `records_count = -1` means scraped but never extracted (see db.py); the
    sampler must skip those, and it is the column it selects on, so a fixture
    without them would not exercise the filter at all.
    """
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE cache_pages (
            url_hash TEXT PRIMARY KEY, url TEXT, city TEXT, topic TEXT, data TEXT,
            records_count INTEGER, extracted_at TEXT, scraped_at TEXT
        )""")
        for i in range(pages):
            positive = i % 2
            text = ("weekly public chess club welcomes members " if positive else
                    "one day commercial sale and news article ") * 20
            conn.execute(
                "INSERT INTO cache_pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"h{i}", f"https://site{i}.example/page", "City", "chess",
                 json.dumps({"raw_text": text}),
                 positive if extracted else -1,
                 "now" if extracted else None, "now"),
            )
    return path


def test_gate_sample_is_balanced_and_local_runner_holds_pages_out(tmp_path):
    module = _load("benchmark_joinability_gate")
    db = _gate_db(tmp_path / "test.db")
    pages = module.load_sample(db, per_class=10, seed="test", max_chars=8000)
    assert len(pages) == 20
    assert sum(page.positive for page in pages) == 10
    scores = module.local_scores(pages, seed="test", buckets=1024)
    assert 0 < len(scores) < len(pages)
    assert all(0 <= score <= 1 for score in scores.values())


def test_the_sampler_reads_page_text_only_for_the_pages_it_picked(tmp_path):
    """The bug that killed a production run on 2026-09-20.

    The first version selected in Python: one `fetchall()` over every
    extracted page's `raw_text`, then a sort, then 2,000 kept. That is ~4 GB
    resident on the real corpus — 128,072 rows averaging 30 KB of blob — on an
    8 GB host already running the scraper. Selection must happen on keys.
    """
    module = _load("benchmark_joinability_gate")
    db = _gate_db(tmp_path / "test.db", pages=200)

    statements: list[str] = []
    real_connect = sqlite3.connect

    def _tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    module.sqlite3.connect = _tracing_connect
    try:
        pages = module.load_sample(db, per_class=5, seed="test", max_chars=8000)
    finally:
        module.sqlite3.connect = real_connect

    assert len(pages) == 10
    blob_reads = [s for s in statements if "json_extract" in s]
    assert blob_reads, "the text has to be fetched somewhere"
    for statement in blob_reads:
        assert "url_hash IN" in statement, (
            "page text may only be read for the sampled keys, never corpus-wide:\n"
            + statement)
    selection = [s for s in statements if "json_extract" not in s and "FROM cache_pages" in s]
    assert selection, "expected a key-only selection query"
    for statement in selection:
        # The partial index idx_cache_pages_done is only eligible when the
        # query repeats its own WHERE clause.
        assert "scraped_at IS NOT NULL" in statement
        assert "records_count >= 0" in statement


def test_pages_that_were_never_extracted_are_not_sampled(tmp_path):
    module = _load("benchmark_joinability_gate")
    db = _gate_db(tmp_path / "unextracted.db", extracted=False)
    with pytest.raises(SystemExit):
        module.load_sample(db, per_class=5, seed="test", max_chars=8000)


def _mixed_gate_db(path: Path, pages: int = 240) -> Path:
    """Pages of varying difficulty, on many hosts.

    The clean fixture cannot show a calibration problem: if every positive is
    obviously positive, any monotone score separates them. Real pages sit on a
    spectrum, and a third of these deliberately carry both vocabularies.
    """
    import random
    rng = random.Random(11)
    club = "kozosseg klub egyesulet heti proba tagfelvetel varjuk jelentkezes korus"
    shop = "akcio arak webshop kosar szallitas hirek cikk kapcsolat impresszum"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE cache_pages (
            url_hash TEXT PRIMARY KEY, url TEXT, city TEXT, topic TEXT, data TEXT,
            records_count INTEGER, extracted_at TEXT, scraped_at TEXT
        )""")
        for i in range(pages):
            positive = i % 2
            # Length varies by an order of magnitude: that is what made the
            # unnormalized score scale with the page rather than its content.
            length = rng.choice((40, 120, 400))
            if i % 3 == 0:          # ambiguous: both vocabularies present
                words = (club + " " + shop).split()
            else:
                words = (club if positive else shop).split()
            text = " ".join(rng.choice(words) for _ in range(length))
            conn.execute(
                "INSERT INTO cache_pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"h{i:04d}", f"https://host{i % 40}.example/p{i}", "City", "choir",
                 json.dumps({"raw_text": text}), positive, "now", "now"),
            )
    return path


def test_the_gate_threshold_actually_moves_the_decision(tmp_path):
    """The failure the 2026-09-20 production run exposed.

    Naive Bayes adds one log-probability per n-gram, so the raw sum scales with
    page length and every page lands on the ±50 clamp. The measured threshold
    column was inert: 0.01 -> 0.50 moved recall by half a point, because no
    page scored anywhere in between. A gate whose threshold does nothing cannot
    be tuned to a recall target, which is the only thing a gate is for.
    """
    module = _load("benchmark_joinability_gate")
    db = _mixed_gate_db(tmp_path / "mixed.db")
    pages = module.load_sample(db, per_class=60, seed="cal", max_chars=8000)
    scores = module.local_scores(pages, seed="cal", buckets=4096)
    assert scores

    values = sorted(scores.values())
    strict = sum(v < 0.02 for v in values)
    loose = sum(v < 0.50 for v in values)
    assert loose > strict, (
        "moving the threshold from 0.02 to 0.50 skipped the same pages — "
        f"scores are stuck at the extremes: {values[:5]} … {values[-5:]}")

    middle = sum(0.02 <= v <= 0.98 for v in values)
    assert middle >= len(values) * 0.10, (
        f"only {middle} of {len(values)} scores carry any uncertainty; a gate "
        "needs a band it can trade recall against")


def test_the_cloudflare_route_sends_and_reads_that_wire_format(tmp_path, monkeypatch):
    """Jev on Workers AI, which is the route that needs no waitlist.

    Same model and questions as the direct API; the envelope differs. TypeSafe
    takes `{model, state, questions}` and answers `{answers}`; Cloudflare nests
    the payload under `input` and may nest the answer under `result`. Both are
    asserted here because neither can be checked against the live service
    until a key exists, and a wrong envelope fails as an empty answer rather
    than as an error.
    """
    import asyncio

    module = _load("benchmark_joinability_gate")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    sent: list[dict] = []

    class _Response:
        status_code = 200

        def raise_for_status(self): ...

        def json(self):
            # The real shape, captured from the live service on 2026-09-20:
            # the gateway envelope wraps the model envelope, so `answers` is
            # two levels down, not one.
            return {"result": {"state": "Completed",
                               "result": {"model": "jev-1.13.0",
                                          "answers": {"has_joinable_community":
                                                      {"type": "noul", "noul": 0.87}},
                                          "usage": {"input_tokens": 311}},
                               "gatewayMetadata": {"keySource": "Unified"}},
                    "success": True}

    class _Client:
        def __init__(self, **kw): self.kw = kw
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            sent.append({"url": url, "body": json})
            return _Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    page = module.Page("h1", "https://a.test/p", "Budapest", "choir", "szöveg", True)
    scores = asyncio.run(module.jev_scores([page], "jev-1.13.0", 1, None, "cloudflare"))

    assert scores == {"h1": 0.87}
    assert len(sent) == 1
    assert sent[0]["url"].endswith("/ai/run")
    body = sent[0]["body"]
    assert body["model"] == "typesafe/jev"
    assert set(body["input"]) == {"state", "questions"}
    assert "model" not in body["input"]
    assert body["input"]["questions"]["has_joinable_community"]["type"] == "noul"


def test_the_typesafe_route_keeps_its_flat_payload(tmp_path, monkeypatch):
    import asyncio

    module = _load("benchmark_joinability_gate")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    sent: list[dict] = []

    class _Response:
        status_code = 200
        def raise_for_status(self): ...
        def json(self):
            return {"answers": {"has_joinable_community": {"noul": 0.2}}}

    class _Client:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            sent.append({"url": url, "body": json})
            return _Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    page = module.Page("h1", "https://a.test/p", "Budapest", "choir", "szöveg", False)
    scores = asyncio.run(module.jev_scores([page], "jev-1.13.0", 1, None, "typesafe"))

    assert scores == {"h1": 0.2}
    assert sent[0]["url"] == module.API_URL
    assert "input" not in sent[0]["body"]
    assert sent[0]["body"]["model"] == "jev-1.13.0"


def test_held_out_only_scores_the_same_pages_the_local_runner_reports_on(tmp_path):
    """A paid run must measure the set the free one was measured on.

    Jev does not train, so scoring it on the whole sample would include the
    pages the local model learned from — flattering Jev on a comparison it was
    supposed to lose or win honestly. It is also five times the API bill for a
    number that cannot be placed next to the local table.
    """
    module = _load("benchmark_joinability_gate")
    db = _mixed_gate_db(tmp_path / "mixed.db", pages=240)
    pages = module.load_sample(db, per_class=60, seed="cal", max_chars=8000)

    held_out = [p for p in pages if not module._split(p, "cal")]
    scored = module.local_scores(pages, seed="cal", buckets=4096)

    assert held_out, "the fixture must produce a hostname split"
    assert {p.url_hash for p in held_out} == set(scored), (
        "--held-out-only selects by the same predicate local_scores reports on")


def test_a_rate_limited_call_is_retried_rather_than_ending_the_run(tmp_path, monkeypatch):
    """429 killed a 1,200-page run after 422 pages on 2026-09-20.

    `asyncio.gather` propagates the first exception, so one rate limit loses
    every page still in flight with it. The response cache makes a restart
    free, but a run that needs babysitting does not finish overnight.
    """
    import asyncio

    module = _load("benchmark_joinability_gate")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    # Hold the real sleep first: the lambda would otherwise call the patched
    # name and recurse forever.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(module.asyncio, "sleep", lambda _s: real_sleep(0))
    codes = [429, 503, 200]

    class _Response:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"raise_for_status on {self.status_code}")
        def json(self):
            return {"result": {"result": {"answers": {
                "has_joinable_community": {"noul": 0.42}}}}}

    class _Client:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            return _Response(codes.pop(0))

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    page = module.Page("h1", "https://a.test/p", "Budapest", "choir", "szöveg", True)
    scores = asyncio.run(module.jev_scores([page], "jev-1.13.0", 1, None, "cloudflare"))

    assert scores == {"h1": 0.42}, "the third attempt's answer must be used"
    assert not codes, "every queued status code should have been consumed"
