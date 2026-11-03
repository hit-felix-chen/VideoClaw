"""Look-impact confirm regenerates the downstream set and writes the ledger.

Tokens live in SQLite. A bad token or a bad node list fails before any node
runs and before any ledger row is written.
"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.routers.library import get_library_connection, use_data_dir
from core.executor import GraphExecutor
from graph.dependency_graph import DependencyGraph
from graph.model import NodeStatus
from models.middle.pricing import QuoteRequest, quote
from models.middle.registry import ModelRegistry
from models.middle.spec import DurationLimit, ModelSpec, PriceQuote, PriceUnit
from storage import (
    AssetStore,
    Database,
    GraphStore,
    LedgerStore,
    LookStore,
    ProjectStore,
)
from storage.confirmations import IMPACT_TOKEN_TTL_SECONDS
from storage.database import StorageError
from storage.ledger import AWAITING_USER, QUOTED, SKIPPED, SPENT

VIDEO = "r7b-video-per-second"
IMAGE = "r7b-image-per-image"
VLM = "r7b-vlm-per-million"


def _register() -> None:
    ModelRegistry.add_spec(
        ModelSpec(
            id=VIDEO,
            name="R7b video",
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
                    source_url="https://example.com/r7b-video-price",
                    source="https://example.com/r7b-video-price",
                )
            ],
        )
    )
    ModelRegistry.add_spec(
        ModelSpec(
            id=IMAGE,
            name="R7b image",
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
                    source_url="https://example.com/r7b-image-price",
                    source="https://example.com/r7b-image-price",
                )
            ],
        )
    )
    ModelRegistry.add_spec(
        ModelSpec(
            id=VLM,
            name="R7b vlm",
            provider="fixture",
            types=["vlm"],
            output="text",
            currency="CNY",
            prices=[
                PriceQuote(
                    model_id=VLM,
                    amount=2.0,
                    currency="CNY",
                    unit=PriceUnit.PER_MILLION_TOKENS,
                    verified=True,
                    source_url="https://example.com/r7b-vlm-price",
                    source="https://example.com/r7b-vlm-price",
                )
            ],
        )
    )


def _unregister() -> None:
    ModelRegistry.remove_spec(VIDEO)
    ModelRegistry.remove_spec(IMAGE)
    ModelRegistry.remove_spec(VLM)


@pytest.fixture
def priced():
    _register()
    try:
        yield
    finally:
        _unregister()


@pytest.fixture
def opened(tmp_path):
    data = tmp_path / "data"
    connection = Database.connect(data)
    try:
        yield connection, data
    finally:
        connection.close()


def _calls():
    seen = []

    def runner(call):
        seen.append(call["node_id"])
        return {"provider_called": False}

    return seen, runner


def _done(graph, *node_ids):
    for node_id in node_ids:
        graph.transition(node_id, NodeStatus.RUNNING)
        graph.transition(node_id, NodeStatus.DONE)


def _status(conn, project_id, node_id):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).status.value


def _token_row(conn, token):
    return conn.execute(
        """
        SELECT project_id, look_id, asset_id, node_ids_json, issued_at,
               expires_at, consumed_at, result_json
        FROM impact_confirmation
        WHERE token = ?
        """,
        (token,),
    ).fetchone()


def _charges(rows):
    return [
        (
            row["id"],
            row["node_id"],
            row["kind"],
            row["status"],
            row["quote_amount"],
            row["actual_amount"],
        )
        for row in rows
    ]


def _chain(conn, cap=100):
    project = ProjectStore.create(conn, "R7b", budget_cap=cap)
    asset = AssetStore.create(conn, project_id=project["id"], name="林夏", kind="character")
    look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
    graph = DependencyGraph()
    graph.add_node(asset["id"], kind="character")
    graph.add_node("frame", kind="keyframe", metadata={"model_id": IMAGE})
    graph.add_node(
        "note",
        kind="vlm",
        metadata={"model_id": VLM, "completion_tokens": 1_000_000},
    )
    graph.add_node("shot", kind="shot_segment", metadata={"model_id": VIDEO})
    graph.add_node("final", kind="final_video", metadata={"model_id": VIDEO})
    graph.add_edge(asset["id"], "frame")
    graph.add_edge(asset["id"], "note")
    graph.add_edge("frame", "shot")
    graph.add_edge("shot", "final")
    _done(graph, asset["id"], "frame", "note", "shot", "final")
    GraphStore.save(conn, project["id"], graph.to_dict())
    LookStore.update(conn, look["id"], prompt="night")
    return project, look, asset


def _image_only(conn, cap=100):
    project = ProjectStore.create(conn, "R7b", budget_cap=cap)
    asset = AssetStore.create(conn, project_id=project["id"], name="林夏", kind="character")
    look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
    graph = DependencyGraph()
    graph.add_node(asset["id"], kind="character")
    graph.add_node("frame", kind="keyframe", metadata={"model_id": IMAGE})
    graph.add_node("other", kind="keyframe", metadata={"model_id": IMAGE})
    graph.add_edge(asset["id"], "frame")
    _done(graph, asset["id"], "frame", "other")
    GraphStore.save(conn, project["id"], graph.to_dict())
    LookStore.update(conn, look["id"], prompt="night")
    return project, look, asset


def _video_only(conn, cap):
    project = ProjectStore.create(conn, "R7b", budget_cap=cap)
    asset = AssetStore.create(conn, project_id=project["id"], name="林夏", kind="character")
    look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
    graph = DependencyGraph()
    graph.add_node(asset["id"], kind="character")
    graph.add_node("shot", kind="shot_segment", metadata={"model_id": VIDEO})
    graph.add_edge(asset["id"], "shot")
    _done(graph, asset["id"], "shot")
    GraphStore.save(conn, project["id"], graph.to_dict())
    LookStore.update(conn, look["id"], prompt="night")
    return project, look, asset


def _frame_and_note(conn, cap):
    """Independent image and VLM successors. Frame is ahead of note."""
    project = ProjectStore.create(conn, "R7b", budget_cap=cap)
    asset = AssetStore.create(conn, project_id=project["id"], name="林夏", kind="character")
    look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
    graph = DependencyGraph()
    graph.add_node(asset["id"], kind="character")
    graph.add_node("frame", kind="keyframe", metadata={"model_id": IMAGE})
    graph.add_node(
        "note",
        kind="vlm",
        metadata={"model_id": VLM, "completion_tokens": 1_000_000},
    )
    graph.add_edge(asset["id"], "frame")
    graph.add_edge(asset["id"], "note")
    _done(graph, asset["id"], "frame", "note")
    GraphStore.save(conn, project["id"], graph.to_dict())
    LookStore.update(conn, look["id"], prompt="night")
    return project, look, asset


def _quote_impact(conn, project_id, look_id):
    _seen, runner = _calls()
    issued = GraphExecutor(conn, runner).impact(project_id, look_id, model=IMAGE)
    return issued


def test_token_is_persisted_and_rejects_expiry_stale_impact_and_wrong_project(opened, priced):
    conn, data = opened
    project, look, asset = _image_only(conn)
    issued = _quote_impact(conn, project["id"], look["id"])
    token = issued["confirm_token"]
    assert issued["impact_nodes"] == ["frame"]
    assert issued["submitted"] is False
    assert LedgerStore.list(conn, project["id"]) == []

    row = _token_row(conn, token)
    assert row is not None
    assert row["project_id"] == project["id"]
    assert row["look_id"] == look["id"]
    assert row["asset_id"] == asset["id"]
    assert json.loads(row["node_ids_json"]) == ["frame"]
    assert row["consumed_at"] is None
    assert row["result_json"] is None
    assert row["expires_at"] > row["issued_at"]

    reopened = Database.connect(data)
    try:
        again = _token_row(reopened, token)
        assert again["project_id"] == project["id"]
        assert json.loads(again["node_ids_json"]) == ["frame"]
    finally:
        reopened.close()

    other = ProjectStore.create(conn, "Other", budget_cap=100)
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    with pytest.raises(StorageError) as wrong_project:
        executor.confirm_impact(other["id"], look["id"], token, model=IMAGE)
    assert "different project" in str(wrong_project.value)
    with pytest.raises(StorageError) as wrong_regenerate:
        executor.regenerate_impact(other["id"], token, ["frame"])
    assert "different project" in str(wrong_regenerate.value)

    conn.execute(
        "UPDATE impact_confirmation SET expires_at = ? WHERE token = ?",
        ("2000-01-01T00:00:00.000000Z", token),
    )
    with pytest.raises(StorageError) as expired:
        executor.confirm_impact(project["id"], look["id"], token, model=IMAGE)
    assert "expired" in str(expired.value)
    conn.execute(
        "UPDATE impact_confirmation SET expires_at = ? WHERE token = ?",
        ("2999-01-01T00:00:00.000000Z", token),
    )

    graph = DependencyGraph.from_dict(GraphStore.load(conn, project["id"]))
    graph.add_node("extra", kind="shot_segment", metadata={"model_id": VIDEO})
    graph.add_edge(asset["id"], "extra", blocks=False)
    graph.transition("extra", NodeStatus.RUNNING)
    graph.transition("extra", NodeStatus.DONE)
    GraphStore.save(conn, project["id"], graph.to_dict())
    with pytest.raises(StorageError) as stale:
        executor.confirm_impact(project["id"], look["id"], token, model=IMAGE)
    assert "stale" in str(stale.value)

    assert seen == []
    assert LedgerStore.list(conn, project["id"]) == []
    assert LedgerStore.list(conn, other["id"]) == []
    assert _token_row(conn, token)["consumed_at"] is None
    assert _status(conn, project["id"], "frame") == "stale"


def test_regenerate_rejects_bad_tokens_and_nodes_before_any_ledger_write(opened, priced):
    conn, _data = opened
    project, look, _asset = _image_only(conn)
    issued = _quote_impact(conn, project["id"], look["id"])
    token = issued["confirm_token"]
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)

    with pytest.raises(StorageError) as missing:
        executor.regenerate_impact(project["id"], "  ", ["frame"])
    assert "required" in str(missing.value)

    with pytest.raises(StorageError) as invalid:
        executor.regenerate_impact(project["id"], "not-a-real-token", ["frame"])
    assert "invalid" in str(invalid.value)

    with pytest.raises(StorageError) as empty:
        executor.regenerate_impact(project["id"], token, [])
    assert "node_ids is required" in str(empty.value)

    with pytest.raises(StorageError) as bad_nodes:
        executor.regenerate_impact(project["id"], token, ["frame", "other", "missing", "missing"])
    message = str(bad_nodes.value)
    assert "unknown node: missing" in message
    assert "node other is not in the confirmed impact set" in message

    assert seen == []
    assert LedgerStore.list(conn, project["id"]) == []
    assert _token_row(conn, token)["consumed_at"] is None
    assert _status(conn, project["id"], "frame") == "stale"

    done = executor.regenerate_impact(project["id"], token, ["frame"])
    assert done["spent"] is True
    assert seen == ["frame"]
    rows = LedgerStore.list(conn, project["id"])
    assert len(rows) == 1
    assert rows[0]["node_id"] == "frame"
    assert rows[0]["status"] == SPENT
    image_max = quote(QuoteRequest(model_id=IMAGE, modality="image", asset_count=1))["max"]
    assert rows[0]["actual_amount"] == image_max
    assert rows[0]["quote_amount"] == image_max

    with pytest.raises(StorageError) as reused:
        executor.regenerate_impact(project["id"], token, ["frame"])
    assert "already consumed" in str(reused.value)
    assert _charges(LedgerStore.list(conn, project["id"])) == _charges(rows)
    assert seen == ["frame"]


def test_confirm_regenerates_successors_and_writes_the_ledger_once(opened, priced):
    conn, data = opened
    project, look, asset = _chain(conn)
    issued = _quote_impact(conn, project["id"], look["id"])
    token = issued["confirm_token"]
    assert issued["impact_nodes"] == ["frame", "note", "shot", "final"]
    assert asset["id"] not in issued["impact_nodes"]
    assert LedgerStore.list(conn, project["id"]) == []

    image_max = quote(QuoteRequest(model_id=IMAGE, modality="image", asset_count=1))["max"]
    video_max = quote(QuoteRequest(model_id=VIDEO, modality="video", quantity=5))["max"]
    vlm_max = quote(QuoteRequest(model_id=VLM, modality="vlm", completion_tokens=1_000_000))["max"]
    assert image_max == 6.0
    assert video_max == 5.0
    assert vlm_max == 2.0

    seen, runner = _calls()
    reopened = Database.connect(data)
    try:
        done = GraphExecutor(reopened, runner).confirm_impact(
            project["id"],
            look["id"],
            token,
            model=IMAGE,
        )
    finally:
        reopened.close()

    assert done["confirmed"] is True
    assert done["executed"] is True
    assert done["spent"] is True
    assert done["provider_called"] is False
    assert seen == ["frame", "note", "shot", "final"]
    by_node = {item["node_id"]: item for item in done["nodes"]}
    assert by_node["frame"]["call_confirmed"] is False
    assert by_node["frame"]["spent"] is True
    assert by_node["frame"]["actual"] == image_max
    assert by_node["note"]["call_confirmed"] is False
    assert by_node["note"]["spent"] is True
    assert by_node["note"]["actual"] == vlm_max
    assert by_node["shot"]["call_confirmed"] is True
    assert by_node["shot"]["spent"] is True
    assert by_node["shot"]["actual"] == video_max
    assert by_node["final"]["call_confirmed"] is True
    assert by_node["final"]["actual"] == video_max
    assert _status(conn, project["id"], "frame") == "done"
    assert _status(conn, project["id"], "note") == "done"
    assert _status(conn, project["id"], "shot") == "done"
    assert _status(conn, project["id"], "final") == "done"
    assert _status(conn, project["id"], asset["id"]) == "done"

    rows = LedgerStore.list(conn, project["id"])
    assert [row["node_id"] for row in rows] == ["frame", "note", "shot", "final"]
    assert [row["kind"] for row in rows] == ["image", "vlm", "video", "video"]
    assert [row["actual_amount"] for row in rows] == [image_max, vlm_max, video_max, video_max]
    assert [row["quote_amount"] for row in rows] == [image_max, vlm_max, video_max, video_max]
    assert {row["status"] for row in rows} == {SPENT}
    first_charges = _charges(rows)

    again_seen, again_runner = _calls()
    third = Database.connect(data)
    try:
        repeated = GraphExecutor(third, again_runner).confirm_impact(
            project["id"],
            look["id"],
            token,
            model=IMAGE,
        )
    finally:
        third.close()
    assert again_seen == []
    assert repeated["nodes"][0]["entry_id"] == done["nodes"][0]["entry_id"]
    assert _charges(LedgerStore.list(conn, project["id"])) == first_charges

    with pytest.raises(StorageError) as reused:
        GraphExecutor(conn, again_runner).regenerate_impact(project["id"], token, ["frame", "shot"])
    assert "already consumed" in str(reused.value)
    assert again_seen == []
    assert _charges(LedgerStore.list(conn, project["id"])) == first_charges


def test_over_budget_confirm_pauses_and_does_not_skip_or_double_spend(opened, priced):
    conn, _data = opened
    project, look, _asset = _video_only(conn, cap=4)
    issued = _quote_impact(conn, project["id"], look["id"])
    token = issued["confirm_token"]
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    paused = executor.confirm_impact(project["id"], look["id"], token, model=VIDEO)
    assert seen == []
    node = paused["nodes"][0]
    assert node["node_id"] == "shot"
    assert node["status"] == "awaiting_user"
    assert node["gate"] == "budget"
    assert node["submitted"] is False
    assert node["spent"] is False
    assert node["status"] != "skipped"
    assert "是否上调" in node["message"]
    rows = LedgerStore.list(conn, project["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == AWAITING_USER
    assert rows[0]["actual_amount"] is None
    assert rows[0]["quote_amount"] == 5.0
    assert _status(conn, project["id"], "shot") == "awaiting_user"

    repeated = executor.confirm_impact(project["id"], look["id"], token, model=VIDEO)
    assert repeated["nodes"][0]["entry_id"] == node["entry_id"]
    assert seen == []
    assert _charges(LedgerStore.list(conn, project["id"])) == _charges(rows)

    continued = executor.raise_budget(project["id"], node["entry_id"], 9)
    assert continued["status"] == "done"
    assert continued["submitted"] is True
    assert continued["spent"] is True
    assert seen == ["shot"]
    spent = LedgerStore.list(conn, project["id"])
    assert len(spent) == 1
    assert spent[0]["id"] == rows[0]["id"]
    assert spent[0]["status"] == SPENT
    assert spent[0]["actual_amount"] == 5.0

    executor.confirm_impact(project["id"], look["id"], token, model=VIDEO)
    assert seen == ["shot"]
    assert _charges(LedgerStore.list(conn, project["id"])) == _charges(spent)


def test_budget_pause_stops_before_an_independent_later_node(opened, priced):
    conn, _data = opened
    project, look, _asset = _frame_and_note(conn, cap=4)
    issued = _quote_impact(conn, project["id"], look["id"])
    assert issued["impact_nodes"] == ["frame", "note"]
    seen, runner = _calls()
    paused = GraphExecutor(conn, runner).confirm_impact(
        project["id"],
        look["id"],
        issued["confirm_token"],
        model=IMAGE,
    )
    assert paused["halted"] is True
    assert [item["node_id"] for item in paused["nodes"]] == ["frame"]
    assert paused["nodes"][0]["status"] == "awaiting_user"
    assert paused["nodes"][0]["gate"] == "budget"
    assert paused["nodes"][0]["spent"] is False
    assert "是否上调" in paused["nodes"][0]["message"]
    assert seen == []
    rows = LedgerStore.list(conn, project["id"])
    assert [row["node_id"] for row in rows] == ["frame"]
    assert rows[0]["status"] == AWAITING_USER
    assert rows[0]["actual_amount"] is None
    assert rows[0]["quote_amount"] == 6.0
    assert not any(row["status"] == SPENT for row in rows)
    assert _status(conn, project["id"], "note") == "stale"


def test_runner_failure_releases_the_quote_and_a_second_confirm_returns_it(opened, priced):
    conn, _data = opened
    project, look, _asset = _frame_and_note(conn, cap=100)
    issued = _quote_impact(conn, project["id"], look["id"])
    token = issued["confirm_token"]
    image_max = quote(QuoteRequest(model_id=IMAGE, modality="image", asset_count=1))["max"]

    def runner(call):
        seen.append(call["node_id"])
        if call["node_id"] == "note":
            raise RuntimeError("provider exploded")
        return {"provider_called": False}

    seen = []
    executor = GraphExecutor(conn, runner)
    with pytest.raises(StorageError) as failed:
        executor.confirm_impact(project["id"], look["id"], token, model=IMAGE)
    assert "note" in str(failed.value)
    assert "provider exploded" in str(failed.value)

    rows = LedgerStore.list(conn, project["id"])
    by_node = {row["node_id"]: row for row in rows}
    assert set(by_node) == {"frame", "note"}
    assert by_node["frame"]["status"] == SPENT
    assert by_node["frame"]["actual_amount"] == image_max
    assert by_node["note"]["status"] == SKIPPED
    assert by_node["note"]["actual_amount"] is None
    assert not any(row["status"] == QUOTED for row in rows)
    stored = _token_row(conn, token)
    assert stored["consumed_at"] is not None
    saved = json.loads(stored["result_json"])
    assert saved["failed"] is True
    assert saved["failed_node_id"] == "note"
    assert saved["error"] == "provider exploded"
    assert saved["spent_node_ids"] == ["frame"]
    assert seen == ["frame", "note"]
    first_charges = _charges(rows)

    again_seen = []

    def quiet(call):
        again_seen.append(call["node_id"])
        raise AssertionError("second confirm ran a node")

    repeated = GraphExecutor(conn, quiet).confirm_impact(
        project["id"],
        look["id"],
        token,
        model=IMAGE,
    )
    assert again_seen == []
    assert repeated["failed"] is True
    assert repeated["failed_node_id"] == "note"
    assert repeated["error"] == "provider exploded"
    assert repeated["spent_node_ids"] == ["frame"]
    assert [item["node_id"] for item in repeated["nodes"]] == ["frame"]
    assert repeated["nodes"][0]["entry_id"] == by_node["frame"]["id"]
    assert _charges(LedgerStore.list(conn, project["id"])) == first_charges


def test_token_for_a_different_look_is_rejected_and_not_consumed(opened, priced):
    conn, _data = opened
    project, look, asset = _image_only(conn, cap=100)
    issued = _quote_impact(conn, project["id"], look["id"])
    token = issued["confirm_token"]
    other = LookStore.create(conn, asset_id=asset["id"], name="夜", prompt="other")
    seen, runner = _calls()
    executor = GraphExecutor(conn, runner)
    with pytest.raises(StorageError) as rejected:
        executor.confirm_impact(project["id"], other["id"], token, model=IMAGE)
    assert "does not match this look" in str(rejected.value)
    assert _token_row(conn, token)["consumed_at"] is None
    assert LedgerStore.list(conn, project["id"]) == []
    assert seen == []

    done = executor.confirm_impact(project["id"], look["id"], token, model=IMAGE)
    assert done["spent"] is True
    assert seen == ["frame"]
    rows = LedgerStore.list(conn, project["id"])
    assert len(rows) == 1
    assert rows[0]["node_id"] == "frame"
    assert rows[0]["status"] == SPENT
    assert _token_row(conn, token)["consumed_at"] is not None


def test_impact_token_ttl_is_1800_seconds(opened, priced):
    conn, _data = opened
    project, look, _asset = _image_only(conn)
    issued = _quote_impact(conn, project["id"], look["id"])
    assert IMPACT_TOKEN_TTL_SECONDS == 1800
    row = _token_row(conn, issued["confirm_token"])
    issued_at = datetime.strptime(row["issued_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    expires_at = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert (expires_at - issued_at).total_seconds() == 1800
    assert _token_row(conn, issued["confirm_token"])["consumed_at"] is None


@pytest.fixture
def api(tmp_path, client):
    data = tmp_path / "api"

    app.dependency_overrides[get_library_connection] = use_data_dir(data)
    try:
        yield client, data
    finally:
        app.dependency_overrides.pop(get_library_connection, None)


def test_v2_impact_confirm_route_regenerates_and_second_post_does_not_double_spend(api, priced):
    client, data = api
    assert isinstance(client, TestClient)
    connection = Database.connect(data)
    try:
        project, look, _asset = _video_only(connection, cap=20)
        project_id = project["id"]
        look_id = look["id"]
    finally:
        connection.close()

    quoted = client.post(
        f"/api/v2/projects/{project_id}/looks/{look_id}/impact",
        json={"model": VIDEO},
    )
    assert quoted.status_code == 200
    token = quoted.json()["confirm_token"]
    assert quoted.json()["impact_nodes"] == ["shot"]
    assert quoted.json()["spent"] is False

    rejected = client.post(
        f"/api/v2/projects/{project_id}/impact/regenerate",
        json={"confirm_token": token, "node_ids": ["missing"]},
    )
    assert rejected.status_code == 400
    assert "unknown node: missing" in rejected.json()["detail"]

    confirmed = client.post(
        f"/api/v2/projects/{project_id}/looks/{look_id}/impact/confirm",
        json={"confirm_token": token, "model": VIDEO},
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["spent"] is True
    assert body["nodes"][0]["node_id"] == "shot"
    assert body["nodes"][0]["call_confirmed"] is True
    assert body["nodes"][0]["actual"] == 5.0

    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.status_code == 200
    ledger = status.json()["ledger"]
    assert len(ledger) == 1
    assert ledger[0]["status"] == SPENT
    assert ledger[0]["actual_amount"] == 5.0

    again = client.post(
        f"/api/v2/projects/{project_id}/looks/{look_id}/impact/confirm",
        json={"confirm_token": token, "model": VIDEO},
    )
    assert again.status_code == 200
    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.json()["ledger"] == ledger

    reused = client.post(
        f"/api/v2/projects/{project_id}/impact/regenerate",
        json={"confirm_token": token, "node_ids": ["shot"]},
    )
    assert reused.status_code == 400
    assert "already consumed" in reused.json()["detail"]
    status = client.get(f"/api/v2/projects/{project_id}/status")
    assert status.json()["ledger"] == ledger
