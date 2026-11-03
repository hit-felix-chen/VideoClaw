"""HTTP API for one v2 episode: graph, prompt, models, stop, delete, retry.

Video submit goes through a recording adapter. The Seedance HTTP client is
patched to fail if a test reaches it.
"""

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.routers.library import get_library_connection
from core.executor import GraphExecutor
from graph.dependency_graph import DependencyGraph
from graph.model import NodeStatus
from models.middle.registry import ModelRegistry
from models.middle.spec import ModelSpec, PriceQuote, PriceUnit
from storage import Database, GraphStore

IMAGE = "r9-image-per-image"
TTS = "r9-tts-per-10k"
RESULT_URL = "https://cdn.example/episode.mp4"


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    def submit(self, spec, params, *, transport=None):
        recorded = dict(params)
        self.calls.append({"model_id": spec.id, "params": recorded, "transport": transport})
        return RESULT_URL


@pytest.fixture
def priced():
    ModelRegistry.add_spec(
        ModelSpec(
            id=IMAGE,
            name="R9 image",
            provider="fixture",
            types=["t2i"],
            output="image",
            currency="CNY",
            prices=[
                PriceQuote(
                    model_id=IMAGE,
                    amount=1.0,
                    currency="CNY",
                    unit=PriceUnit.PER_IMAGE,
                    verified=True,
                    resolution="720p",
                    source_url="https://example.com/r9-image-price",
                    source="https://example.com/r9-image-price",
                )
            ],
        )
    )
    ModelRegistry.add_spec(
        ModelSpec(
            id=TTS,
            name="R9 tts",
            provider="fixture",
            types=["tts"],
            output="audio",
            currency="CNY",
            prices=[
                PriceQuote(
                    model_id=TTS,
                    amount=1.0,
                    currency="CNY",
                    unit=PriceUnit.PER_10K_CHARACTERS,
                    verified=True,
                    source_url="https://example.com/r9-tts-price",
                    source="https://example.com/r9-tts-price",
                )
            ],
        )
    )
    try:
        yield
    finally:
        ModelRegistry.remove_spec(IMAGE)
        ModelRegistry.remove_spec(TTS)


@pytest.fixture
def api(tmp_path, client, monkeypatch):
    import models.video_seedance as video_seedance

    def explode(*_args, **_kwargs):
        raise AssertionError("live http")

    monkeypatch.setattr(video_seedance.requests, "post", explode)
    monkeypatch.setattr(video_seedance.requests, "get", explode)
    data = tmp_path / "episode-api"

    def override():
        connection = Database.connect(data)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_library_connection] = override
    adapter = RecordingAdapter()

    def factory(conn):
        return GraphExecutor(conn, video_adapter=adapter)

    monkeypatch.setattr("api.routers.executor._executor", factory)
    try:
        yield client, data, adapter
    finally:
        app.dependency_overrides.pop(get_library_connection, None)


def _node(status, node_id):
    return next(node for node in status["nodes"] if node["id"] == node_id)


def _create(client, **extra):
    body = {"name": "第1集", "idea": "林夏走进教室", "budget_cap": 500, "image_model": IMAGE, "tts_model": TTS}
    body.update(extra)
    created = client.post("/api/v2/projects", json=body)
    assert created.status_code == 200, created.text
    return created.json()


def _run_until_video(client, project_id):
    for node_id in ("script", "storyboard", "character", "reference"):
        response = client.post(f"/api/v2/projects/{project_id}/nodes/{node_id}/run", json={})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "done"
        assert response.json()["provider_called"] is False
    held = client.post(f"/api/v2/projects/{project_id}/nodes/video/run", json={})
    assert held.status_code == 200, held.text
    return held.json()


def test_startup_recovers_stale_video_claims(monkeypatch):
    calls = []

    def fake(conn):
        calls.append(conn)
        return ["video"]

    monkeypatch.setattr(GraphExecutor, "recover_stale_submit_claims", staticmethod(fake))
    with TestClient(app) as started:
        assert started.get("/api/health").status_code == 200
    assert calls


def test_status_reports_ready_node_state_without_changing_the_graph(api):
    client, data, _adapter = api
    project_id = _create(client)["project"]["id"]
    connection = Database.connect(data)
    try:
        connection.execute(
            """
            INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
            VALUES (?, 'script', 'ready', 0, '2026-01-01T00:00:00Z')
            """,
            (project_id,),
        )
        connection.commit()
    finally:
        connection.close()
    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.status_code == 200
    script = _node(status.json(), "script")
    assert script["status"] == "pending"
    assert script["node_state"] == "ready"


def test_client_fixture_is_the_app(client):
    assert isinstance(client, TestClient)


def test_create_project_stores_the_episode_graph(api):
    client, _data, adapter = api
    created = _create(client)
    ids = [node["id"] for node in created["nodes"]]
    assert ids[0] == "script"
    assert set(ids) == {"script", "storyboard", "character", "reference", "video", "tts"}
    assert ids.index("storyboard") < ids.index("character")
    assert ids.index("storyboard") < ids.index("reference")
    assert ids.index("character") < ids.index("video")
    assert ids.index("reference") < ids.index("video")
    assert ids.index("video") < ids.index("tts")
    pairs = {(edge["predecessor"], edge["successor"]) for edge in created["edges"]}
    assert ("script", "storyboard") in pairs
    assert ("character", "video") in pairs
    assert ("reference", "video") in pairs
    assert ("video", "tts") in pairs
    assert created["nodes"][0]["metadata"]["text"] == "林夏走进教室"
    assert created["episode_id"] == "ep1"
    project_id = created["project"]["id"]
    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.status_code == 200
    assert [node["id"] for node in status.json()["nodes"]] == ids
    assert adapter.calls == []


def test_prompt_and_model_updates_survive_reload(api, priced):
    client, _data, _adapter = api
    project_id = _create(client)["project"]["id"]
    saved = client.patch(
        f"/api/v2/projects/{project_id}/nodes/script/prompt",
        json={"text": "改过的开场"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["text"] == "改过的开场"
    status = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(status, "script")["metadata"]["text"] == "改过的开场"

    missing = client.patch(
        f"/api/v2/projects/{project_id}/nodes/missing/prompt",
        json={"text": "不会写入"},
    )
    assert missing.status_code == 404

    models = client.patch(
        f"/api/v2/projects/{project_id}/models",
        json={"llm_model": "llm-saved", "image_model": IMAGE},
    )
    assert models.status_code == 200, models.text
    updated = {item["node_id"]: item["model_id"] for item in models.json()["updated"]}
    assert updated["script"] == "llm-saved"
    assert updated["storyboard"] == "llm-saved"
    assert updated["character"] == IMAGE
    assert updated["reference"] == IMAGE
    status = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(status, "script")["metadata"]["model_id"] == "llm-saved"
    assert _node(status, "character")["metadata"]["model_id"] == IMAGE

    empty = client.patch(f"/api/v2/projects/{project_id}/models", json={})
    assert empty.status_code == 400


def test_stop_reports_what_actually_stopped(api):
    client, data, _adapter = api
    project_id = _create(client)["project"]["id"]
    idle = client.post(f"/api/v2/projects/{project_id}/stop")
    assert idle.status_code == 200, idle.text
    assert idle.json()["stopped"] == []
    assert idle.json()["refused"] == []
    assert "没有正在运行" in idle.json()["message"]

    connection = Database.connect(data)
    try:
        graph = DependencyGraph.from_dict(GraphStore.load(connection, project_id))
        graph.transition("script", NodeStatus.RUNNING)
        GraphStore.save(connection, project_id, graph.to_dict())
    finally:
        connection.close()

    stopped = client.post(f"/api/v2/projects/{project_id}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["stopped"] == ["script"]
    status = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(status, "script")["status"] == "failed"
    assert _node(status, "script")["metadata"]["stop_reason"] == "用户停止"

    connection = Database.connect(data)
    try:
        graph = DependencyGraph.from_dict(GraphStore.load(connection, project_id))
        graph.transition("script", NodeStatus.RUNNING)
        graph.update_metadata("script", submit_dispatched=True, vendor_task_id="task-held")
        GraphStore.save(connection, project_id, graph.to_dict())
    finally:
        connection.close()

    refused = client.post(f"/api/v2/projects/{project_id}/stop")
    assert refused.status_code == 409
    assert "不能把它标成已停止" in refused.json()["detail"]
    status = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(status, "script")["status"] == "running"
    assert _node(status, "script")["metadata"]["vendor_task_id"] == "task-held"


def test_delete_project_removes_the_row(api):
    client, _data, _adapter = api
    project_id = _create(client)["project"]["id"]
    deleted = client.delete(f"/api/v2/projects/{project_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted", "id": project_id}
    missing = client.get(f"/api/v2/projects/{project_id}/status")
    assert missing.status_code == 404
    again = client.delete(f"/api/v2/projects/{project_id}")
    assert again.status_code == 404
    assert again.json()["detail"] != "deleted"


def test_one_episode_confirm_submits_once_with_fake_adapter(api, priced):
    client, _data, adapter = api
    project_id = _create(client)["project"]["id"]
    _run_until_video(client, project_id)
    assert adapter.calls == []
    status = client.get(f"/api/v2/projects/{project_id}/status").json()
    video = _node(status, "video")
    assert video["status"] == "awaiting_user"
    assert video["quote"]["max"] == 70.0
    assert video["quote"]["currency"] == "CNY"

    prompt = client.patch(
        f"/api/v2/projects/{project_id}/nodes/video/prompt",
        json={"text": "林夏走进教室，回头"},
    )
    assert prompt.status_code == 200, prompt.text
    reloaded = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(reloaded, "video")["metadata"]["text"] == "林夏走进教室，回头"

    rejected = client.post(
        f"/api/v2/projects/{project_id}/nodes/video/confirm",
        json={"force_new": True},
    )
    assert rejected.status_code == 400
    assert "retry_video_submit" in rejected.json()["detail"]
    assert adapter.calls == []
    queried = client.post(f"/api/v2/projects/{project_id}/nodes/video/confirm?force_new=true")
    assert queried.status_code == 400
    assert adapter.calls == []

    done = client.post(
        f"/api/v2/projects/{project_id}/nodes/video/confirm",
        json={"force_new": False},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    assert done.json()["call_confirmed"] is True
    assert done.json()["provider_called"] is True
    assert done.json()["spent"] is True
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["params"]["prompt"] == "林夏走进教室，回头"
    assert "vendor_task_id" not in adapter.calls[0]["params"]

    spoken = client.post(f"/api/v2/projects/{project_id}/nodes/tts/run", json={})
    assert spoken.status_code == 200, spoken.text
    assert spoken.json()["status"] == "done"
    assert spoken.json()["provider_called"] is False
    final = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(final, "video")["status"] == "done"
    assert _node(final, "tts")["status"] == "done"
    assert len(adapter.calls) == 1


def test_budget_pause_offers_raise_or_decline(api, priced):
    client, _data, adapter = api
    # Image quotes reserve up to 6 per node. 30 covers both images and not the video.
    created = _create(client, budget_cap=30)
    project_id = created["project"]["id"]
    _run_until_video(client, project_id)
    paused = client.post(f"/api/v2/projects/{project_id}/nodes/video/confirm")
    assert paused.status_code == 200, paused.text
    assert paused.json()["gate"] == "budget"
    assert paused.json()["submitted"] is False
    assert paused.json()["provider_called"] is False
    assert adapter.calls == []
    entry_id = paused.json()["entry_id"]
    status = client.get(f"/api/v2/projects/{project_id}/status").json()
    video = _node(status, "video")
    assert video["status"] == "awaiting_user"
    assert video["budget_hold"] is True
    assert video["entry_id"] == entry_id

    raised = client.post(
        f"/api/v2/projects/{project_id}/budget/{entry_id}/raise",
        json={"budget_cap": 200},
    )
    assert raised.status_code == 200, raised.text
    assert raised.json()["status"] == "done"
    assert raised.json()["provider_called"] is True
    assert len(adapter.calls) == 1

    other = _create(client, name="预算不够就放弃", budget_cap=30)["project"]["id"]
    _run_until_video(client, other)
    held = client.post(f"/api/v2/projects/{other}/nodes/video/confirm")
    assert held.json()["gate"] == "budget"
    declined = client.post(f"/api/v2/projects/{other}/budget/{held.json()['entry_id']}/decline")
    assert declined.status_code == 200, declined.text
    assert declined.json()["submitted"] is False
    assert len(adapter.calls) == 1
    after = client.get(f"/api/v2/projects/{other}/status").json()
    assert _node(after, "video")["status"] == "skipped"


def test_retry_video_submit_defaults_force_new_false(api, priced):
    client, data, adapter = api
    project_id = _create(client)["project"]["id"]
    held = _run_until_video(client, project_id)
    assert held["gate"] == "confirm"
    connection = Database.connect(data)
    try:
        graph = DependencyGraph.from_dict(GraphStore.load(connection, project_id))
        graph.update_metadata("video", submit_dispatched=True, vendor_task_id="task-keep")
        GraphStore.save(connection, project_id, graph.to_dict())
    finally:
        connection.close()

    blocked = client.post(f"/api/v2/projects/{project_id}/nodes/video/confirm")
    assert blocked.status_code == 400
    assert adapter.calls == []
    forced = client.post(
        f"/api/v2/projects/{project_id}/nodes/video/confirm",
        json={"force_new": True},
    )
    assert forced.status_code == 400
    assert "retry_video_submit" in forced.json()["detail"]
    status = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(status, "video")["metadata"]["vendor_task_id"] == "task-keep"
    assert _node(status, "video")["metadata"]["submit_dispatched"] is True

    resumed = client.post(f"/api/v2/projects/{project_id}/nodes/video/retry_video_submit")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "done"
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["params"].get("vendor_task_id") == "task-keep"

    project_id = _create(client, name="新建一笔")["project"]["id"]
    _run_until_video(client, project_id)
    connection = Database.connect(data)
    try:
        graph = DependencyGraph.from_dict(GraphStore.load(connection, project_id))
        graph.update_metadata("video", submit_dispatched=True, vendor_task_id="task-old")
        GraphStore.save(connection, project_id, graph.to_dict())
    finally:
        connection.close()
    created = client.post(
        f"/api/v2/projects/{project_id}/nodes/video/retry_video_submit",
        json={"force_new": True},
    )
    assert created.status_code == 200, created.text
    assert created.json()["provider_called"] is True
    assert len(adapter.calls) == 2
    assert "vendor_task_id" not in adapter.calls[1]["params"]
    final = client.get(f"/api/v2/projects/{project_id}/status").json()
    assert _node(final, "video")["metadata"].get("vendor_task_id") in (None, "")
    assert _node(final, "video")["status"] == "done"
