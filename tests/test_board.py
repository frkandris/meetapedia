"""The engine-room board: the agent sessions message each other at /v1/board."""
import base64
from unittest.mock import patch

from fastapi.testclient import TestClient

from scraper.db import init_db
from scraper.web import app as web_app
from scraper.web.state import app_state


def _client(tmp_path, monkeypatch):
    db = tmp_path / "scraper.db"
    init_db(db)
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setenv("LOCAL_GPU_KEY", "gpu-secret")
    monkeypatch.setenv("CONTROL_API_KEY", "ops-secret")
    return TestClient(web_app.app)


def test_both_agents_can_post_and_read_with_keys_they_already_hold(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    gpu = {"Authorization": "Bearer gpu-secret"}
    ops = {"Authorization": "Bearer ops-secret"}

    assert client.get("/v1/board").status_code == 401
    assert client.get("/v1/board", headers={"Authorization": "Bearer nope"}).status_code == 401

    first = client.post("/v1/board/messages", headers=gpu,
                        json={"author": "gpu", "text": "kész a cserére"}).json()
    client.post("/v1/board/messages", headers=ops, json={"author": "szerver", "text": "worker áll"})
    client.put("/v1/board/state", headers=ops, json={"author": "szerver", "text": "mérés fut"})

    board = client.get(f"/v1/board?since={first['id']}", headers=gpu).json()
    assert [m["text"] for m in board["messages"]] == ["worker áll"]
    assert board["state"]["text"] == "mérés fut"


def test_the_api_refuses_unknown_authors_and_the_operators_name(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    gpu = {"Authorization": "Bearer gpu-secret"}
    for author in ("andras", "someone"):
        r = client.post("/v1/board/messages", headers=gpu, json={"author": author, "text": "x"})
        assert r.status_code == 400


def test_the_operator_reads_and_posts_from_the_admin_page(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:testpass").decode(),
            "Origin": "http://testserver"}
    with patch("scraper.web.app._ADMIN_PASSWORD", "testpass"):
        assert "Gépházi napló" in client.get("/admin/board", headers=auth).text
        assert client.post("/admin/api/board", headers=auth, data={"text": "Mehet."}).json()["ok"]
        data = client.get("/admin/api/board", headers=auth).json()
    assert [(m["author"], m["text"]) for m in data["messages"]] == [("andras", "Mehet.")]
