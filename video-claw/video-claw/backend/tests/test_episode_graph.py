"""One episode through the graph executor with fake node providers.

Script and storyboard run without a ledger row. Character and reference
images spend without a per-call confirm. The video node quotes Seedance
2.5, waits, and writes the ledger only after confirm. TTS spends without
waiting. References come from SegmentRefStore.resolve.
"""

import pytest

from core.executor import GraphExecutor
from core.nodes.dispatch import dispatch_node, submitted
from graph.dependency_graph import DependencyGraph
from graph.model import NodeStatus
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.registry import ModelRegistry
from models.middle.spec import ModelSpec, PriceQuote, PriceUnit
from storage import AssetStore, Database, GraphStore, LedgerStore, ProjectStore
from storage.ledger import SPENT

IMAGE = "r8-image-per-image"
TTS = "r8-tts-per-10k"
SEEDANCE = DEFAULT_REFERENCE_VIDEO_MODEL


def _register() -> None:
    ModelRegistry.add_spec(
        ModelSpec(
            id=IMAGE,
            name="R8 image",
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
                    source_url="https://example.com/r8-image-price",
                    source="https://example.com/r8-image-price",
                )
            ],
        )
    )
    ModelRegistry.add_spec(
        ModelSpec(
            id=TTS,
            name="R8 tts",
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
                    source_url="https://example.com/r8-tts-price",
                    source="https://example.com/r8-tts-price",
                )
            ],
        )
    )


def _unregister() -> None:
    ModelRegistry.remove_spec(IMAGE)
    ModelRegistry.remove_spec(TTS)


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


def _status(conn, project_id, node_id):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).status.value


def _result(conn, project_id, node_id):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).metadata.get("result")


def test_one_episode_quotes_video_and_resolves_references_without_globbing(conn, priced, tmp_path):
    submitted.clear()
    project = ProjectStore.create(conn, "第1集", budget_cap=200)
    project_id = project["id"]
    hero = AssetStore.create(conn, project_id=project_id, name="林夏", kind="character")
    AssetStore.create(conn, project_id=project_id, name="林夏二", kind="character")
    stray = tmp_path / "林夏_extra.png"
    stray.write_bytes(b"png")

    graph = DependencyGraph()
    graph.add_node("script", kind="script", episode_id="ep1", metadata={"text": "林夏走进教室"})
    graph.add_node("storyboard", kind="storyboard", episode_id="ep1")
    graph.add_node("character", kind="character", episode_id="ep1", metadata={"model_id": IMAGE})
    graph.add_node("reference", kind="keyframe", episode_id="ep1", metadata={"model_id": IMAGE})
    graph.add_node(
        "video",
        kind="shot_segment",
        episode_id="ep1",
        metadata={
            "model_id": SEEDANCE,
            "resolution": "720p",
            "includes_video_input": False,
            "completion_tokens": 1_000_000,
            "shot_duration": 6,
            "text": "@林夏 走进教室",
        },
    )
    graph.add_node(
        "tts",
        kind="tts",
        episode_id="ep1",
        metadata={"model_id": TTS, "quantity": 10000, "unit": PriceUnit.PER_10K_CHARACTERS, "text": "林夏"},
    )
    graph.add_edge("script", "storyboard")
    graph.add_edge("storyboard", "character")
    graph.add_edge("storyboard", "reference")
    graph.add_edge("character", "video")
    graph.add_edge("reference", "video")
    graph.add_edge("video", "tts")
    GraphStore.save(conn, project_id, graph.to_dict())

    executor = GraphExecutor(conn, allow_offline_video=True)
    assert executor._runner is dispatch_node or executor._runner.__name__ == "_default_runner"

    script = executor.run_node(project_id, "script")
    assert script["status"] == "done"
    assert script["spent"] is False
    assert script["call_confirmed"] is False
    assert script["result"]["kind"] == "script"
    assert script["result"]["episode_number"] == 1
    assert _status(conn, project_id, "script") == NodeStatus.DONE.value

    storyboard = executor.run_node(project_id, "storyboard")
    assert storyboard["status"] == "done"
    assert storyboard["spent"] is False
    segment = storyboard["result"]["segments"][0]
    assert segment["total_duration"] == sum(shot["duration"] for shot in segment["shots"])

    blocked = executor.run_node(project_id, "video", {"duration": 99})
    assert blocked["status"] == "blocked"
    assert blocked["why_blocked"]["executable"] is False

    character = executor.run_node(project_id, "character", {"model_id": IMAGE})
    reference = executor.run_node(project_id, "reference", {"model_id": IMAGE})
    for node_id, body in (("character", character), ("reference", reference)):
        assert body["status"] == "done", node_id
        assert body["gate"] == "none"
        assert body["call_confirmed"] is False
        assert body["spent"] is True
        assert body["provider_called"] is False
    image_rows = [row for row in LedgerStore.list(conn, project_id) if row["kind"] == "image"]
    assert [row["node_id"] for row in image_rows] == ["character", "reference"]
    assert all(row["status"] == SPENT for row in image_rows)

    held = executor.run_node(project_id, "video", {"duration": 99})
    assert held["status"] == "awaiting_user"
    assert held["gate"] == "confirm"
    assert held["call_confirmed"] is False
    assert held["submitted"] is False
    assert held["spent"] is False
    assert held["entry_id"] is None
    assert held["quote"]["max"] == 70.0
    assert held["quote"]["max"] != 99
    assert held["duration"]["default"] == -1
    assert held["duration"]["min"] == 4
    assert held["duration"]["max"] == 30
    assert _status(conn, project_id, "video") == "awaiting_user"
    assert [row for row in LedgerStore.list(conn, project_id) if row["node_id"] == "video"] == []
    assert _status(conn, project_id, "tts") == "pending"
    tts_blocked = executor.run_node(project_id, "tts", {"model_id": TTS, "quantity": 10000})
    assert tts_blocked["status"] == "blocked"
    assert tts_blocked["why_blocked"]["reasons"][0]["id"] == "video"

    done = executor.confirm(project_id, "video")
    assert done["status"] == "done"
    assert done["call_confirmed"] is True
    assert done["spent"] is True
    assert done["provider_called"] is False
    prepared = _result(conn, project_id, "video")
    assert prepared["model_id"] == SEEDANCE
    assert prepared["ability"] == "reference_to_video"
    assert prepared["generation_mode"] == "reference"
    assert prepared["duration"] == 6
    assert prepared["reference_image_paths"] == [hero["id"]]
    assert prepared["truncated"] is False
    assert str(stray) not in prepared["reference_image_paths"]
    assert "林夏二" not in prepared["reference_image_paths"]
    video_rows = [row for row in LedgerStore.list(conn, project_id) if row["node_id"] == "video"]
    assert len(video_rows) == 1
    assert video_rows[0]["status"] == SPENT
    assert video_rows[0]["actual_amount"] == 70.0
    assert video_rows[0]["kind"] == "video"

    spoken = executor.run_node(
        project_id,
        "tts",
        {"model_id": TTS, "quantity": 10000, "unit": PriceUnit.PER_10K_CHARACTERS},
    )
    assert spoken["status"] == "done"
    assert spoken["gate"] == "none"
    assert spoken["call_confirmed"] is False
    assert spoken["spent"] is True
    tts_rows = [row for row in LedgerStore.list(conn, project_id) if row["node_id"] == "tts"]
    assert len(tts_rows) == 1
    assert tts_rows[0]["status"] == SPENT
    assert tts_rows[0]["kind"] == "tts"

    assert [item["node_id"] for item in submitted] == [
        "script",
        "storyboard",
        "character",
        "reference",
        "video",
        "tts",
    ]
    assert submitted[4]["generation_mode"] == "reference"
    assert submitted[4]["model_id"] == SEEDANCE
    assert all(item["modality"] != "video" or item["generation_mode"] == "reference" for item in submitted)
