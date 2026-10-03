"""IndexNow pushes changed URLs to Bing, whose index ChatGPT search reads."""
import pytest
from fastapi.testclient import TestClient

from scraper import indexnow
from scraper.web import app as web_app


def test_only_a_protocol_shaped_key_is_used():
    assert indexnow.valid_key(" abcd1234-EF ") == "abcd1234-EF"
    for bad in (None, "", "short", "has space here", "x" * 129, "semi;colon1"):
        assert indexnow.valid_key(bad) is None


def test_only_urls_changed_since_the_day_are_submitted():
    entries = [("https://k.com/a", "2026-10-02"), ("https://k.com/b", "2026-09-30"),
               ("https://k.com/c", None), ("https://k.com/d", "2026-10-03")]
    assert indexnow.changed_since(entries, "2026-10-02") == [
        "https://k.com/a", "https://k.com/d"]


@pytest.mark.asyncio
async def test_a_submission_names_the_host_key_and_key_file_and_respects_the_cap():
    sent = []

    class _Resp:
        status_code = 202
        text = ""

    async def post(url, json):
        sent.append((url, json))
        return _Resp()

    urls = [f"https://kozossegek.com/p{i}" for i in range(indexnow.MAX_URLS + 5)]
    statuses = await indexnow.submit("https://kozossegek.com", urls, "abcd1234", post=post)
    assert statuses == [202, 202]
    assert [len(j["urlList"]) for _, j in sent] == [indexnow.MAX_URLS, 5]
    url, body = sent[0]
    assert url == "https://api.indexnow.org/indexnow"
    assert body["host"] == "kozossegek.com" and body["key"] == "abcd1234"
    assert body["keyLocation"] == "https://kozossegek.com/indexnow-key.txt"


@pytest.mark.parametrize("key,status", [(None, 404), ("abcd1234efgh", 200)])
def test_the_key_file_exists_only_when_a_key_is_configured(monkeypatch, key, status):
    if key:
        monkeypatch.setenv("INDEXNOW_KEY", key)
    else:
        monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    r = TestClient(web_app.app).get("/indexnow-key.txt")
    assert r.status_code == status
    if key:
        assert r.text == key
