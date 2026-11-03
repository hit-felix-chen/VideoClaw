"""Video nodes refuse to authorize when no adapter and no provider key.

Image nodes still settle offline. ``allow_offline_video=True`` is the
explicit in-process opt-in. No test calls a vendor.
"""

import logging
import threading

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.routers.library import get_library_connection, use_data_dir
from core.executor import NO_VENDOR_MESSAGE, GraphExecutor, _ClaimLost, _NoVendorKey
from graph.dependency_graph import DependencyGraph
from graph.model import NodeStatus
from models.middle.registry import ModelRegistry
from models.middle.spec import DurationLimit, ModelSpec, PriceQuote, PriceUnit
from storage import AssetStore, Database, GraphStore, LedgerStore, LookStore, ProjectStore

VIDEO = "r9b-video-per-second"
IMAGE = "r9b-image-per-image"


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    def submit(self, spec, params, *, transport=None):
        self.calls.append(dict(params))
        return "https://cdn.example/r9b.mp4"


def _register() -> None:
    ModelRegistry.add_spec(
        ModelSpec(
            id=VIDEO,
            name="R9b video",
            provider="fixture",
            types=["video"],
            output="video",
            currency="CNY",
            duration=DurationLimit(
                min_seconds=4,
                max_seconds=15,
                integer=True,
                default=5,
                verified=True,
            ),
            prices=[
                PriceQuote(
                    model_id=VIDEO,
                    amount=1.0,
                    currency="CNY",
                    unit=PriceUnit.PER_SECOND,
                    verified=True,
                    source_url="https://example.com/r9b-video-price",
                    source="https://example.com/r9b-video-price",
                )
            ],
        )
    )
    ModelRegistry.add_spec(
        ModelSpec(
            id=IMAGE,
            name="R9b image",
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
                    source_url="https://example.com/r9b-image-price",
                    source="https://example.com/r9b-image-price",
                )
            ],
        )
    )


def _unregister() -> None:
    ModelRegistry.remove_spec(VIDEO)
    ModelRegistry.remove_spec(IMAGE)


@pytest.fixture
def priced():
    _register()
    try:
        yield
    finally:
        _unregister()


@pytest.fixture
def conn(tmp_path):
    connection = Database.connect(tmp_path / "data")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def api(tmp_path, client):
    data = tmp_path / "api"
    app.dependency_overrides[get_library_connection] = use_data_dir(data)
    try:
        yield client, data
    finally:
        app.dependency_overrides.pop(get_library_connection, None)


def _done(graph, *node_ids):
    for node_id in node_ids:
        graph.transition(node_id, NodeStatus.RUNNING)
        graph.transition(node_id, NodeStatus.DONE)


def _video_impact(conn, cap=100):
    project = ProjectStore.create(conn, "无密钥", budget_cap=cap)
    asset = AssetStore.create(conn, project_id=project["id"], name="林夏", kind="character")
    look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
    graph = DependencyGraph()
    graph.add_node(asset["id"], kind="character")
    graph.add_node("shot", kind="shot_segment", metadata={"model_id": VIDEO, "resolution": "720p"})
    graph.add_edge(asset["id"], "shot")
    _done(graph, asset["id"], "shot")
    GraphStore.save(conn, project["id"], graph.to_dict())
    LookStore.update(conn, look["id"], prompt="night")
    return project, look


def _metadata(conn, project_id, node_id="shot"):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).metadata


def _status(conn, project_id, node_id="shot"):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).status.value


def test_client_is_the_app(client):
    assert isinstance(client, TestClient)


def test_impact_confirm_and_regenerate_without_a_vendor_are_409(api, priced, monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module.Config, "ARK_API_KEY", "", raising=False)
    monkeypatch.setattr(config_module.Config, "DASHSCOPE_API_KEY", "", raising=False)
    monkeypatch.setattr(config_module.Config, "KLING_API_KEY", "", raising=False)
    client, data = api
    connection = Database.connect(data)
    try:
        project, look = _video_impact(connection, cap=100)
        project_id = project["id"]
        look_id = look["id"]
    finally:
        connection.close()

    quoted = client.post(
        f"/api/v2/projects/{project_id}/looks/{look_id}/impact",
        json={"model": VIDEO},
    )
    assert quoted.status_code == 200, quoted.text
    token = quoted.json()["confirm_token"]
    assert quoted.json()["impact_nodes"] == ["shot"]

    confirmed = client.post(
        f"/api/v2/projects/{project_id}/looks/{look_id}/impact/confirm",
        json={"confirm_token": token, "model": VIDEO},
    )
    assert confirmed.status_code == 409
    assert confirmed.json()["detail"] == NO_VENDOR_MESSAGE

    regenerated = client.post(
        f"/api/v2/projects/{project_id}/impact/regenerate",
        json={"confirm_token": token, "node_ids": ["shot"], "model_id": VIDEO},
    )
    assert regenerated.status_code == 409
    assert regenerated.json()["detail"] == NO_VENDOR_MESSAGE

    connection = Database.connect(data)
    try:
        assert LedgerStore.list(connection, project_id) == []
        assert _metadata(connection, project_id).get("submit_dispatched") is None
        assert _status(connection, project_id) == "stale"
    finally:
        connection.close()


def test_in_process_video_entries_share_the_no_vendor_guard(conn, priced):
    project, look = _video_impact(conn, cap=100)
    issued = GraphExecutor(conn).impact(project["id"], look["id"], model=VIDEO)
    token = issued["confirm_token"]
    executor = GraphExecutor(conn)
    with pytest.raises(_NoVendorKey) as refused:
        executor.confirm_impact(project["id"], look["id"], token, model=VIDEO)
    assert str(refused.value) == NO_VENDOR_MESSAGE
    with pytest.raises(_NoVendorKey):
        executor.regenerate_impact(project["id"], token, ["shot"], {"model_id": VIDEO})
    assert LedgerStore.list(conn, project["id"]) == []
    assert _metadata(conn, project["id"]).get("submit_dispatched") is None

    direct = ProjectStore.create(conn, "直接确认", budget_cap=100)
    graph = DependencyGraph()
    graph.add_node("video", kind="shot_segment", metadata={"model_id": VIDEO})
    GraphStore.save(conn, direct["id"], graph.to_dict())
    plain = GraphExecutor(conn)
    held = plain.run_node(direct["id"], "video")
    assert held["gate"] == "confirm"
    with pytest.raises(_NoVendorKey):
        plain.confirm(direct["id"], "video")
    assert LedgerStore.list(conn, direct["id"]) == []
    assert _status(conn, direct["id"], "video") == "awaiting_user"


def test_allow_offline_video_still_books_and_images_do_not_need_it(conn, priced):
    project = ProjectStore.create(conn, "图像", budget_cap=100)
    graph = DependencyGraph()
    graph.add_node("hero", kind="character", metadata={"model_id": IMAGE, "resolution": "720p"})
    GraphStore.save(conn, project["id"], graph.to_dict())
    image = GraphExecutor(conn).run_node(project["id"], "hero")
    assert image["status"] == "done"
    assert image["spent"] is True
    assert image["provider_called"] is False
    stored = DependencyGraph.from_dict(GraphStore.load(conn, project["id"])).node("hero")
    assert stored.metadata["provider_called"] is False

    direct = ProjectStore.create(conn, "显式离线", budget_cap=100)
    graph = DependencyGraph()
    graph.add_node("video", kind="shot_segment", metadata={"model_id": VIDEO})
    GraphStore.save(conn, direct["id"], graph.to_dict())
    offline = GraphExecutor(conn, allow_offline_video=True)
    offline.run_node(direct["id"], "video")
    done = offline.confirm(direct["id"], "video")
    assert done["status"] == "done"
    assert done["spent"] is True
    assert done["provider_called"] is False
    assert LedgerStore.list(conn, direct["id"])[0]["status"] == "spent"


def test_claim_lost_returns_a_running_node_and_releases_the_quote(conn, priced, tmp_path):
    project = ProjectStore.create(conn, "认领丢失", budget_cap=100)
    graph = DependencyGraph()
    graph.add_node("video", kind="shot_segment", metadata={"model_id": VIDEO})
    GraphStore.save(conn, project["id"], graph.to_dict())
    project_id = project["id"]
    GraphExecutor(conn).run_node(project_id, "video")
    adapter = RecordingAdapter()
    entered = threading.Event()
    release = threading.Event()

    def runner(call):
        if call.get("node_id") == "video" or call.get("modality") == "video":
            entered.set()
            assert release.wait(5)
        return {"provider_called": False, "kind": call.get("kind")}

    box = {}

    def confirm():
        local = Database.connect(tmp_path / "data")
        try:
            GraphExecutor(local, runner=runner, video_adapter=adapter).confirm(project_id, "video")
        except Exception as exc:
            box["error"] = exc
        finally:
            local.close()

    thread = threading.Thread(target=confirm, daemon=True)
    thread.start()
    assert entered.wait(5)
    bumped = conn.execute(
        """
        UPDATE node_state
        SET updated_at = ?
        WHERE project_id = ? AND node_id = ? AND status = 'running'
        """,
        ("2099-01-01T00:00:00.000000Z", project_id, "video"),
    )
    assert bumped.rowcount == 1
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(box["error"], _ClaimLost)
    assert _status(conn, project_id, "video") == "awaiting_user"
    assert _metadata(conn, project_id, "video").get("submit_dispatched") is None
    assert all(row["status"] != "quoted" for row in LedgerStore.list(conn, project_id))
    assert adapter.calls == []
    continued = GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert continued["status"] == "done"
    assert continued["provider_called"] is True
    assert len(adapter.calls) == 1


def test_unwind_logs_when_the_awaiting_write_is_rejected(conn, caplog):
    project = ProjectStore.create(conn, "回写失败", budget_cap=100)
    project_id = project["id"]
    graph = DependencyGraph()
    graph.add_node("video", kind="shot_segment")
    graph.transition("video", NodeStatus.RUNNING)
    GraphStore.save(conn, project_id, graph.to_dict())
    conn.execute(
        """
        INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
        VALUES (?, 'video', 'running', 0, '2026-01-01T00:00:00.000000Z')
        """,
        (project_id,),
    )

    def probe(executor):
        executor.conn.execute(
            """
            UPDATE node_state SET updated_at = ?
            WHERE project_id = ? AND node_id = 'video'
            """,
            ("2099-01-01T00:00:00.000000Z", project_id),
        )

    GraphExecutor.save_probe = probe
    try:
        with caplog.at_level(logging.WARNING, logger="core.executor"):
            GraphExecutor(conn)._unwind_lost_claim(project_id, "video", None)
    finally:
        GraphExecutor.save_probe = None
    assert _status(conn, project_id, "video") == "running"
    assert "compare-and-save rejected" in caplog.text
    assert project_id in caplog.text
