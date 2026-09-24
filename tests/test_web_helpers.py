from fastapi.testclient import TestClient

from scraper.web import app as web_app
from scraper.web.app import _BasicAuth, _safe_redirect_target
from scraper.web.schema import records_to_jsonld
from scraper.web.state import app_state


def test_safe_redirect_target_allows_only_local_paths():
    assert _safe_redirect_target("/admin/cache", "/") == "/admin/cache"
    assert _safe_redirect_target("//evil.test", "/") == "/"
    assert _safe_redirect_target("https://evil.test", "/") == "/"


def test_same_origin_admin_write_rejects_cross_origin_posts():
    scope = {"method": "POST"}

    assert _BasicAuth._same_origin_admin_write(
        scope,
        {b"host": b"example.com", b"origin": b"https://example.com"},
    )
    assert not _BasicAuth._same_origin_admin_write(
        scope,
        {b"host": b"example.com", b"origin": b"https://evil.test"},
    )
    assert not _BasicAuth._same_origin_admin_write(scope, {b"host": b"example.com"})


def test_jsonld_escapes_script_end_tags():
    raw = records_to_jsonld([
        {
            "name": "</script><script>alert(1)</script>",
            "topic": "running",
            "city": "Budapest",
            "locale": "hu",
            "source_url": "https://example.com",
            "extracted_at": "2026-01-01T00:00:00+00:00",
        }
    ])

    assert "</script>" not in raw
    assert "<\\/script>" in raw


def test_healthz_is_public_and_reports_status():
    response = TestClient(web_app.app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_reload_runtime_config_updates_app_state(monkeypatch, tmp_path):
    old_db_path = app_state.db_path
    old_cities = app_state.cities
    old_topics = app_state.topics
    old_pipeline_cfg = app_state.pipeline_cfg

    try:
        app_state.db_path = tmp_path / "scraper.db"
        expected = (["city"], ["topic"], object())
        monkeypatch.setattr(web_app, "load_config", lambda db_path: expected)

        web_app._reload_runtime_config()

        assert app_state.cities == ["city"]
        assert app_state.topics == ["topic"]
        assert app_state.pipeline_cfg is expected[2]
    finally:
        app_state.db_path = old_db_path
        app_state.cities = old_cities
        app_state.topics = old_topics
        app_state.pipeline_cfg = old_pipeline_cfg


def test_config_editors_refuse_instead_of_losing_the_edit(tmp_path, monkeypatch):
    """`/app/config` ships in the image and is not persisted: a city or topic
    saved here was reverted by the next deploy (review, 2026-09-24).
    """
    import base64
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from scraper.web import app as web_app

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "cities.yaml").write_text("cities: []\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "CONFIG_DIR", config_dir)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:testpass").decode(),
            "Origin": "http://testserver"}
    with patch("scraper.web.app._ADMIN_PASSWORD", "testpass"):
        for name in ("cities", "topics", "settings"):
            r = TestClient(web_app.app).post(f"/admin/config/{name}", headers=auth,
                                             data={f"{name}_yaml": "x: 1\n"},
                                             follow_redirects=False)
            assert r.status_code in (302, 303) and "error" in r.headers["location"]
    assert (config_dir / "cities.yaml").read_text(encoding="utf-8") == "cities: []\n"


def test_progress_page_renders_and_polls_a_count(tmp_path, monkeypatch):
    """The page polled every cache entry (~207K rows) every 8 s; it now polls
    a count. Rendered here because nothing else loads this template.
    """
    import base64
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from scraper.cache import CacheManager
    from scraper.db import init_db
    from scraper.web import app as web_app
    from scraper.web.state import app_state

    db = tmp_path / "scraper.db"
    init_db(db)
    cache = CacheManager(db)
    cache.save_scraped("https://x.test/a", "text", "Pécs", "running")
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setattr(app_state, "cache_manager", cache)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:testpass").decode()}
    with patch("scraper.web.app._ADMIN_PASSWORD", "testpass"):
        client = TestClient(web_app.app)
        page = client.get("/admin/progress", headers=auth)
        assert page.status_code == 200 and "/admin/api/cache-count" in page.text
        assert client.get("/admin/api/cache-count", headers=auth).json() == {"count": 1}


def test_a_global_extraction_rule_needs_explicit_confirmation(tmp_path, monkeypatch):
    """Adding or removing one re-extracts every cached page; a stray click did
    it silently (review, 2026-09-24).
    """
    import base64
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from scraper.db import init_db
    from scraper.false_positives import get_false_positives
    from scraper.web import app as web_app
    from scraper.web.state import app_state

    db = tmp_path / "scraper.db"
    init_db(db)
    monkeypatch.setattr(app_state, "db_path", db)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:testpass").decode(),
            "Origin": "http://testserver"}
    with patch("scraper.web.app._ADMIN_PASSWORD", "testpass"):
        client = TestClient(web_app.app)
        first = client.post("/admin/prompts/nc-accept", headers=auth,
                            data={"rule_text": "Ne vedd fel az iskolákat."}).json()
        assert first == {"ok": False, "needs_confirm": True, "pages": 0}
        assert not get_false_positives(db)
        second = client.post("/admin/prompts/nc-accept", headers=auth,
                             data={"rule_text": "Ne vedd fel az iskolákat.",
                                   "confirm": "re-extract-all"}).json()
        assert second == {"ok": True}
    assert len(get_false_positives(db)) == 1
