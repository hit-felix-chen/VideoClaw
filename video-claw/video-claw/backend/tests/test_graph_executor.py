"""R7 graph executor: block, quote, confirm, budget, and single-node regenerate.

No test here calls a provider. Runners are local call lists.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.routers.library import get_library_connection, use_data_dir
from core.executor import GraphExecutor
from graph.dependency_graph import DependencyGraph
from graph.model import NodeStatus
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.pricing import QuoteRequest, quote
from models.middle.registry import ModelRegistry
from models.middle.spec import DurationLimit, ModelSpec, PriceQuote, PriceUnit
from storage import (
    AssetStore,
    Database,
    GraphStore,
    ImageStore,
    LedgerStore,
    LookStore,
    ProjectStore,
)
from storage.ledger import AWAITING_USER, SKIPPED, SPENT, UNAVAILABLE

VIDEO = "r7-video-per-second"
IMAGE = "r7-image-per-image"
SEEDANCE = DEFAULT_REFERENCE_VIDEO_MODEL


def _register() -> None:
    ModelRegistry.add_spec(
        ModelSpec(
            id=VIDEO,
            name="R7 video",
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
                    source_url="https://example.com/r7-video-price",
                    source="https://example.com/r7-video-price",
                )
            ],
        )
    )
    ModelRegistry.add_spec(
        ModelSpec(
            id=IMAGE,
            name="R7 image",
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
                    source_url="https://example.com/r7-image-price",
                    source="https://example.com/r7-image-price",
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


def _calls():
    seen = []

    def runner(call):
        seen.append(call["node_id"])
        return {"provider_called": False}

    return seen, runner


def _project(conn, cap=100):
    return ProjectStore.create(conn, "R7", budget_cap=cap)


def _save(conn, project_id, graph):
    GraphStore.save(conn, project_id, graph.to_dict())


def _chain(conn, project_id):
    graph = DependencyGraph()
    graph.add_node("script", kind="script")
    graph.add_node("shot", kind="shot_segment")
    graph.add_node("final", kind="final_video")
    graph.add_edge("script", "shot")
    graph.add_edge("shot", "final")
    for node_id in ("script", "shot", "final"):
        graph.transition(node_id, NodeStatus.RUNNING)
        graph.transition(node_id, NodeStatus.DONE)
    _save(conn, project_id, graph)
    return graph


def _ready_shot(conn, project_id):
    graph = DependencyGraph()
    graph.add_node("script", kind="script")
    graph.add_node("shot", kind="shot_segment")
    graph.add_edge("script", "shot")
    graph.transition("script", NodeStatus.RUNNING)
    graph.transition("script", NodeStatus.DONE)
    _save(conn, project_id, graph)


def _status(conn, project_id, node_id):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).status.value


def test_executor_module_does_not_import_a_provider():
    text = Path(__file__).resolve().parents[1].joinpath("core/executor.py").read_text(encoding="utf-8")
    for banned in ("video_seedance", "video_client", "image_client", "httpx", "requests"):
        assert banned not in text


def test_why_blocked_does_not_run_or_write_a_ledger(conn):
    project = _project(conn)
    graph = DependencyGraph()
    graph.add_node("script", kind="script")
    graph.add_node("shot", kind="shot_segment")
    graph.add_edge("script", "shot")
    _save(conn, project["id"], graph)
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    result = executor.run_node(project["id"], "shot", {"model_id": VIDEO, "duration": 99})
    assert result["status"] == "blocked"
    assert result["submitted"] is False
    assert result["spent"] is False
    assert result["why_blocked"]["executable"] is False
    assert result["why_blocked"]["reasons"][0]["id"] == "script"
    assert seen == []
    assert LedgerStore.list(conn, project["id"]) == []
    assert _status(conn, project["id"], "shot") == "pending"


def test_video_quote_writes_nothing_until_confirm(conn, priced, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("provider called")

    import models.video_seedance as video_seedance

    monkeypatch.setattr(video_seedance.SeedanceVideoClient, "generate_video", explode, raising=False)
    project = _project(conn, cap=20)
    _ready_shot(conn, project["id"])
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    held = executor.run_node(
        project["id"],
        "shot",
        {"model_id": VIDEO, "duration": 99},
    )
    assert held["status"] == "awaiting_user"
    assert held["gate"] == "confirm"
    assert held["call_confirmed"] is False
    assert held["submitted"] is False
    assert held["spent"] is False
    assert held["entry_id"] is None
    assert held["provider_called"] is False
    assert held["duration"]["default"] == 5
    assert held["quote"]["max"] == 5.0
    assert held["quote"]["max"] != 99
    assert seen == []
    assert LedgerStore.list(conn, project["id"]) == []
    assert _status(conn, project["id"], "shot") == "awaiting_user"

    done = executor.confirm(project["id"], "shot")
    assert done["status"] == "done"
    assert done["submitted"] is True
    assert done["spent"] is True
    assert done["call_confirmed"] is True
    assert done["provider_called"] is False
    assert seen == ["shot"]
    rows = LedgerStore.list(conn, project["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == SPENT
    assert rows[0]["actual_amount"] == 5.0
    assert rows[0]["quote_amount"] == 5.0


def test_video_over_budget_pauses_then_raise_continues(conn, priced):
    project = _project(conn, cap=4)
    _ready_shot(conn, project["id"])
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    held = executor.run_node(project["id"], "shot", {"model_id": VIDEO, "duration": 30})
    assert held["gate"] == "confirm"
    assert held["over_budget"] is True
    assert "是否上调" in held["message"]
    assert LedgerStore.list(conn, project["id"]) == []
    assert seen == []

    paused = executor.confirm(project["id"], "shot")
    assert paused["status"] == "awaiting_user"
    assert paused["gate"] == "budget"
    assert paused["submitted"] is False
    assert paused["spent"] is False
    assert paused["call_confirmed"] is True
    assert "是否上调" in paused["message"]
    assert seen == []
    paused_row = LedgerStore.get(conn, paused["entry_id"])
    assert paused_row["status"] == AWAITING_USER
    assert paused_row["actual_amount"] is None

    continued = executor.raise_budget(project["id"], paused["entry_id"], 9)
    assert continued["status"] == "done"
    assert continued["submitted"] is True
    assert continued["spent"] is True
    assert seen == ["shot"]
    spent = LedgerStore.get(conn, paused["entry_id"])
    assert spent["status"] == SPENT
    assert spent["actual_amount"] == 5.0


def test_decline_skips_without_running(conn, priced):
    project = _project(conn, cap=4)
    _ready_shot(conn, project["id"])
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    executor.run_node(project["id"], "shot", {"model_id": VIDEO})
    assert LedgerStore.list(conn, project["id"]) == []
    skipped = executor.decline_node(project["id"], "shot")
    assert skipped["status"] == "skipped"
    assert skipped["submitted"] is False
    assert skipped["spent"] is False
    assert seen == []
    assert LedgerStore.list(conn, project["id"]) == []
    assert _status(conn, project["id"], "shot") == "skipped"

    project_b = _project(conn, cap=4)
    _ready_shot(conn, project_b["id"])
    seen_b, runner_b = _calls()
    other = GraphExecutor(conn, runner_b)
    other.run_node(project_b["id"], "shot", {"model_id": VIDEO})
    paused = other.confirm(project_b["id"], "shot")
    declined = other.decline_budget(project_b["id"], paused["entry_id"])
    assert declined["status"] == SKIPPED
    assert declined["submitted"] is False
    assert seen_b == []
    row = LedgerStore.get(conn, paused["entry_id"])
    assert row["status"] == SKIPPED
    assert row["actual_amount"] is None
    assert _status(conn, project_b["id"], "shot") == "skipped"


def test_regenerate_stales_successors_and_does_not_rerun_them(conn, priced):
    project = _project(conn, cap=20)
    _chain(conn, project["id"])
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    held = executor.regenerate(project["id"], "shot", {"model_id": VIDEO, "duration": 99})
    assert held["regenerated"] == "shot"
    assert held["status"] == "awaiting_user"
    assert held["successor_status"]["final"] == "stale"
    assert seen == []
    assert LedgerStore.list(conn, project["id"]) == []
    assert _status(conn, project["id"], "final") == "stale"
    assert _status(conn, project["id"], "shot") == "awaiting_user"

    done = executor.confirm(project["id"], "shot")
    assert done["status"] == "done"
    assert done["successor_status"]["final"] == "stale"
    assert seen == ["shot"]
    assert _status(conn, project["id"], "final") == "stale"


def test_image_spends_without_per_call_confirm_and_uses_image_rows(conn, priced):
    project = _project(conn, cap=100)
    graph = DependencyGraph()
    graph.add_node("hero", kind="character")
    _save(conn, project["id"], graph)
    asset = AssetStore.create(
        conn,
        project_id=project["id"],
        name="林夏",
        kind="character",
        asset_id="char_1",
    )
    AssetStore.create(
        conn,
        project_id=project["id"],
        name="路人",
        kind="character",
        asset_id="char_10",
    )
    image = ImageStore.create(
        conn,
        asset_id=asset["id"],
        source="generated",
        model=IMAGE,
        prompt="林夏",
    )
    ImageStore.create(
        conn,
        asset_id="char_10",
        source="generated",
        model=IMAGE,
        prompt="路人",
    )
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    result = executor.run_node(
        project["id"],
        "hero",
        {"model_id": IMAGE, "asset_id": "char_1", "text": "参考 @林夏"},
    )
    assert result["status"] == "done"
    assert result["call_confirmed"] is False
    assert result["submitted"] is True
    assert result["spent"] is True
    assert result["gate"] == "none"
    assert seen == ["hero"]
    assert result["versions"] == [image["id"]]
    mention = result["references"]["mentions"][0]
    assert mention["resolved"] is True
    assert mention["matches"][0]["asset_id"] == "char_1"
    row = LedgerStore.list(conn, project["id"])[0]
    assert row["status"] == SPENT
    assert row["actual_amount"] == result["quote"]["max"]


def test_image_over_budget_raise_and_vlm_same_gate(conn, priced):
    project = _project(conn, cap=1)
    graph = DependencyGraph()
    graph.add_node("hero", kind="keyframe")
    graph.add_node("voice", kind="tts")
    _save(conn, project["id"], graph)
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    paused = executor.run_node(project["id"], "hero", {"model_id": IMAGE})
    assert paused["status"] == "awaiting_user"
    assert paused["gate"] == "budget"
    assert paused["call_confirmed"] is False
    assert paused["submitted"] is False
    assert seen == []
    assert LedgerStore.get(conn, paused["entry_id"])["actual_amount"] is None

    continued = executor.raise_budget(project["id"], paused["entry_id"], 100)
    assert continued["submitted"] is True
    assert continued["spent"] is True
    assert seen == ["hero"]

    unknown = executor.run_node(project["id"], "voice", {"model_id": "edge-tts", "modality": "tts"})
    assert unknown["status"] == "unavailable"
    assert unknown["submitted"] is False
    assert unknown["quote"]["min"] is None
    assert unknown["quote"]["max"] is None
    assert "edge-tts" in unknown["quote"]["unknown_models"]
    assert seen == ["hero"]
    unknown_row = [row for row in LedgerStore.list(conn, project["id"]) if row["model"] == "edge-tts"][0]
    assert unknown_row["status"] == UNAVAILABLE
    assert unknown_row["quote_amount"] is None
    assert unknown_row["actual_amount"] is None


def test_unpriced_video_model_does_not_invent_a_duration_or_a_ledger_row(conn):
    project = _project(conn, cap=100)
    _ready_shot(conn, project["id"])
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    result = executor.run_node(
        project["id"],
        "shot",
        {"model_id": SEEDANCE, "duration": 10},
    )
    assert result["status"] == "unavailable"
    assert result["submitted"] is False
    assert result["quote"]["min"] is None
    assert result["quote"]["max"] is None
    assert result["duration"]["default"] == -1
    assert result["duration"]["min"] == 4
    assert seen == []
    assert LedgerStore.list(conn, project["id"]) == []
    assert _status(conn, project["id"], "shot") == "pending"


def test_look_impact_uses_price_book_and_confirm_does_not_spend_or_rerun(conn, priced):
    project = _project(conn)
    asset = AssetStore.create(conn, project_id=project["id"], name="林夏", kind="character")
    look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
    ImageStore.create(
        conn,
        look_id=look["id"],
        asset_id=asset["id"],
        source="generated",
        model="old",
        prompt="day",
        cost=9,
    )
    segment = AssetStore.create(conn, project_id=project["id"], name="镜头", kind="segment")
    graph = DependencyGraph()
    graph.add_node(asset["id"], kind="character")
    graph.add_node(segment["id"], kind="segment")
    graph.add_edge(asset["id"], segment["id"], blocks=False)
    graph.transition(segment["id"], NodeStatus.RUNNING)
    graph.transition(segment["id"], NodeStatus.DONE)
    _save(conn, project["id"], graph)
    LookStore.update(conn, look["id"], prompt="night")

    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    unknown = executor.impact(project["id"], look["id"], model="not-a-real-model")
    assert unknown["estimated_cost"] is None
    assert unknown["cost_known"] is False
    assert unknown["executed"] is False
    assert unknown["will_spend"] is False
    assert "not-a-real-model" in unknown["unknown_models"]
    assert LedgerStore.list(conn, project["id"]) == []

    direct = quote(QuoteRequest(model_id=IMAGE, modality="image", asset_count=1))
    priced_quote = executor.impact(project["id"], look["id"], model=IMAGE)
    assert priced_quote["price_source"] == "price_book"
    assert priced_quote["estimated_cost"] == direct["max"]
    assert priced_quote["estimated_cost"] != 9
    assert priced_quote["confirmed"] is False
    assert priced_quote["submitted"] is False
    before = len(LedgerStore.list(conn, project["id"]))
    accepted = executor.confirm_impact(
        project["id"],
        look["id"],
        priced_quote["confirm_token"],
        model=IMAGE,
    )
    assert accepted["confirmed"] is True
    assert accepted["executed"] is False
    assert accepted["will_spend"] is False
    assert accepted["spent"] is False
    assert len(LedgerStore.list(conn, project["id"])) == before
    assert seen == []
    assert _status(conn, project["id"], segment["id"]) == "stale"


@pytest.fixture
def api(tmp_path, client):
    data = tmp_path / "api"

    app.dependency_overrides[get_library_connection] = use_data_dir(data)
    try:
        yield client, data
    finally:
        app.dependency_overrides.pop(get_library_connection, None)


def test_v2_routes_quote_confirm_raise_decline_and_status(api, priced):
    client, data = api
    assert isinstance(client, TestClient)
    connection = Database.connect(data)
    try:
        project = _project(connection, cap=4)
        _ready_shot(connection, project["id"])
        project_id = project["id"]
    finally:
        connection.close()

    blocked = client.post(f"/api/v2/projects/{project_id}/nodes/missing/run", json={"model_id": VIDEO})
    assert blocked.status_code == 404

    held = client.post(
        f"/api/v2/projects/{project_id}/nodes/shot/run",
        json={"model_id": VIDEO, "duration": 99},
    )
    assert held.status_code == 200
    assert held.json()["gate"] == "confirm"
    assert held.json()["quote"]["max"] == 5.0
    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.status_code == 200
    assert status.json()["ledger"] == []
    assert "shot" in status.json()["awaiting_user"]

    paused = client.post(f"/api/v2/projects/{project_id}/nodes/shot/confirm")
    assert paused.status_code == 200
    assert paused.json()["gate"] == "budget"
    assert paused.json()["submitted"] is False
    entry_id = paused.json()["entry_id"]
    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.json()["ledger"][0]["status"] == AWAITING_USER
    assert status.json()["ledger"][0]["actual_amount"] is None

    still = client.post(
        f"/api/v2/projects/{project_id}/budget/{entry_id}/raise",
        json={"budget_cap": 4},
    )
    assert still.status_code == 200
    assert still.json()["submitted"] is False

    declined = client.post(f"/api/v2/projects/{project_id}/budget/{entry_id}/decline")
    assert declined.status_code == 200
    assert declined.json()["status"] == SKIPPED
    assert declined.json()["submitted"] is False
    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.json()["ledger"][0]["actual_amount"] is None
    assert status.json()["ledger"][0]["status"] == SKIPPED
