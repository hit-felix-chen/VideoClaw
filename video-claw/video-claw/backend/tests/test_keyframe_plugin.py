"""First frame and first-plus-last frame. Fake adapters only. No vendor HTTP."""

import pytest

from config import Config
from core.executor import GraphExecutor, _GateError, _NoVendorKey
from core.keyframes import LAST_WITHOUT_FIRST, MIXED_REFERENCES
from graph.dependency_graph import DependencyGraph
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from storage import AssetStore, Database, GraphStore, LedgerStore, ProjectStore

SEEDANCE = DEFAULT_REFERENCE_VIDEO_MODEL
RESULT_URL = "https://cdn.example/keyframe.mp4"
NO_FIRST = "wan2.7-r2v"
NO_END = "wan2.6-i2v-flash"


class RecordingAdapter:
    def __init__(self, result=RESULT_URL):
        self.calls = []
        self.result = result

    def submit(self, spec, params, *, transport=None):
        recorded = dict(params)
        refs = recorded.get("reference_image_paths")
        if isinstance(refs, list):
            recorded["reference_image_paths"] = list(refs)
        self.calls.append({"model_id": spec.id, "params": recorded, "transport": transport})
        return self.result


@pytest.fixture
def conn(tmp_path):
    connection = Database.connect(tmp_path / "data")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _no_live_video(monkeypatch):
    import models.video_seedance as video_seedance

    def explode(*_args, **_kwargs):
        raise AssertionError("live http")

    monkeypatch.setattr(video_seedance.requests, "post", explode)
    monkeypatch.setattr(video_seedance.requests, "get", explode)
    monkeypatch.setattr(Config, "ARK_API_KEY", "")
    monkeypatch.setattr(Config, "DASHSCOPE_API_KEY", "")
    monkeypatch.setattr(Config, "KLING_API_KEY", "")


def _project(conn, model_id=SEEDANCE, text="她转身离开"):
    project = ProjectStore.create(conn, "第1集", budget_cap=200)
    project_id = project["id"]
    hero = AssetStore.create(conn, project_id=project_id, name="林夏", kind="character")
    graph = DependencyGraph()
    graph.add_node(
        "video",
        kind="shot_segment",
        episode_id="ep1",
        metadata={
            "model_id": model_id,
            "resolution": "720p",
            "includes_video_input": False,
            "completion_tokens": 1_000_000,
            "shot_duration": 6,
            "text": text,
        },
    )
    GraphStore.save(conn, project_id, graph.to_dict())
    return project_id, hero


def _portrait(conn, asset_id, payload=None) -> str:
    from storage import BlobStore, ImageStore
    from tests.support import SampleMedia

    data = SampleMedia.png_bytes() if payload is None else payload
    blob = BlobStore.put(conn, data, mime="image/png", kind="image")
    ImageStore.create(
        conn,
        asset_id=asset_id,
        blob_hash=blob["hash"],
        source="uploaded",
        prompt="帧",
    )
    return blob["path"]


def _metadata(conn, project_id, node_id="video"):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).metadata


def _rows(conn, project_id):
    return list(LedgerStore.list(conn, project_id))


def test_seedance_content_uses_official_first_and_last_roles(tmp_path):
    from models.video_seedance import SeedanceVideoClient
    from tests.support import SampleMedia

    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(SampleMedia.png_bytes())
    last.write_bytes(SampleMedia.png_bytes() + b"\x00")
    content = SeedanceVideoClient.build_content("她离开", image_path=str(first), last_image_path=str(last))
    assert content[0] == {"type": "text", "text": "她离开"}
    assert content[1]["type"] == "image_url"
    assert content[1]["role"] == "first_frame"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["type"] == "image_url"
    assert content[2]["role"] == "last_frame"
    assert set(content[2]) == {"type", "image_url", "role"}
    referenced = SeedanceVideoClient.build_content(
        "参考",
        image_path=str(first),
        last_image_path=str(last),
        reference_image_paths=[str(first)],
    )
    assert [item.get("role") for item in referenced if item.get("type") == "image_url"] == ["reference_image"]


def test_first_frame_submits_a_library_file_after_confirm(conn):
    project_id, hero = _project(conn)
    portrait = _portrait(conn, hero["id"])
    adapter = RecordingAdapter()
    executor = GraphExecutor(conn, video_adapter=adapter)
    held = executor.run_node(project_id, "video", {"first_frame_asset_id": hero["id"], "last_frame_asset_id": ""})
    assert held["status"] == "awaiting_user"
    assert held["gate"] == "confirm"
    assert held["quote"]["max"] == 70.0
    assert held["spent"] is False
    assert adapter.calls == []
    assert _rows(conn, project_id) == []
    stored = _metadata(conn, project_id)
    assert stored["first_frame_asset_id"] == hero["id"]
    assert "last_frame_asset_id" not in stored
    assert stored["pending_call"]["ratio"] == "adaptive"

    done = executor.confirm(project_id, "video")
    assert done["provider_called"] is True
    assert done["spent"] is True
    assert done["result_id"] == RESULT_URL
    assert len(adapter.calls) == 1
    params = adapter.calls[0]["params"]
    assert params["generation_mode"] == "first_frame"
    assert params["ability"] == "first_frame_i2v"
    assert params["image_path"] == portrait
    assert params["reference_image_paths"] == []
    assert params["ratio"] == "adaptive"
    assert params["video_ratio"] == "adaptive"
    assert hero["id"] not in params["image_path"]
    assert params.get("last_image_path") in (None, "")
    result = _metadata(conn, project_id)["result"]
    assert result["first_frame_asset_id"] == hero["id"]
    assert "image_path" not in result
    assert result["generation_mode"] == "first_frame"
    assert _rows(conn, project_id)[0]["status"] == "spent"


def test_first_and_last_frame_are_two_library_files(conn):
    project_id, hero = _project(conn)
    opening = _portrait(conn, hero["id"], payload=b"\x89PNG\r\nfirst")
    other = AssetStore.create(conn, project_id=project_id, name="教室", kind="setting")
    closing = _portrait(conn, other["id"], payload=b"\x89PNG\r\nlast!")
    adapter = RecordingAdapter()
    executor = GraphExecutor(conn, video_adapter=adapter)
    held = executor.run_node(
        project_id,
        "video",
        {"first_frame_asset_id": hero["id"], "last_frame_asset_id": other["id"]},
    )
    assert held["gate"] == "confirm"
    assert held["quote"]["max"] == 70.0
    done = executor.confirm(project_id, "video")
    assert done["provider_called"] is True
    params = adapter.calls[0]["params"]
    assert params["generation_mode"] == "start_end_frame"
    assert params["ability"] == "start_end_frame_i2v"
    assert params["image_path"] == opening
    assert params["last_image_path"] == closing
    assert params["reference_image_paths"] == []
    assert params["ratio"] == "adaptive"
    result = _metadata(conn, project_id)["result"]
    assert result["first_frame_asset_id"] == hero["id"]
    assert result["last_frame_asset_id"] == other["id"]
    assert opening not in str(result)
    assert closing not in str(result)


def test_missing_asset_and_unsupported_model_do_not_quote(conn):
    project_id, hero = _project(conn)
    _portrait(conn, hero["id"])
    executor = GraphExecutor(conn, video_adapter=RecordingAdapter())
    with pytest.raises(_GateError, match="首帧资产不在资产库中"):
        executor.run_node(project_id, "video", {"first_frame_asset_id": "not-an-asset"})
    assert _rows(conn, project_id) == []

    with pytest.raises(_GateError, match=LAST_WITHOUT_FIRST):
        executor.run_node(project_id, "video", {"first_frame_asset_id": "", "last_frame_asset_id": hero["id"]})

    reference_only = _project(conn, model_id=NO_FIRST, text="她离开")
    refused_id, refused_hero = reference_only
    _portrait(conn, refused_hero["id"])
    with pytest.raises(_GateError, match="不支持首帧生视频"):
        GraphExecutor(conn).run_node(refused_id, "video", {"first_frame_asset_id": refused_hero["id"]})
    assert _rows(conn, refused_id) == []

    no_end_id, no_end_hero = _project(conn, model_id=NO_END, text="她离开")
    end = AssetStore.create(conn, project_id=no_end_id, name="门口", kind="setting")
    _portrait(conn, no_end_hero["id"], payload=b"open-frame")
    _portrait(conn, end["id"], payload=b"end-frame!")
    with pytest.raises(_GateError, match="不支持首尾帧生视频"):
        GraphExecutor(conn).run_node(
            no_end_id,
            "video",
            {"first_frame_asset_id": no_end_hero["id"], "last_frame_asset_id": end["id"]},
        )
    assert _rows(conn, no_end_id) == []


def test_reference_mentions_cannot_mix_with_a_first_frame(conn):
    project_id, hero = _project(conn, text="@林夏 走进教室")
    _portrait(conn, hero["id"])
    with pytest.raises(_GateError, match=MIXED_REFERENCES):
        GraphExecutor(conn, video_adapter=RecordingAdapter()).run_node(
            project_id,
            "video",
            {"first_frame_asset_id": hero["id"]},
        )
    assert _rows(conn, project_id) == []


def test_no_key_still_refuses_before_authorize_and_offline_keeps_asset_ids(conn):
    project_id, hero = _project(conn)
    portrait = _portrait(conn, hero["id"])
    held = GraphExecutor(conn).run_node(project_id, "video", {"first_frame_asset_id": hero["id"]})
    assert held["gate"] == "confirm"
    assert _rows(conn, project_id) == []
    with pytest.raises(_NoVendorKey):
        GraphExecutor(conn).confirm(project_id, "video")
    assert _rows(conn, project_id) == []

    done = GraphExecutor(conn, allow_offline_video=True).confirm(project_id, "video")
    assert done["spent"] is True
    assert done["provider_called"] is False
    result = _metadata(conn, project_id)["result"]
    assert result["first_frame_asset_id"] == hero["id"]
    assert portrait not in str(result)
    assert result["provider_called"] is False


def test_a_capable_model_without_a_documented_price_still_has_no_number(conn):
    project_id, hero = _project(conn, model_id="wan2.7-i2v", text="她离开")
    _portrait(conn, hero["id"])
    held = GraphExecutor(conn).run_node(project_id, "video", {"first_frame_asset_id": hero["id"]})
    assert held["status"] == "unavailable"
    assert held["quote"]["min"] is None
    assert held["quote"]["max"] is None
    assert _rows(conn, project_id) == []


def test_a_model_without_a_documented_price_still_has_no_number(conn):
    project_id, _hero = _project(conn, model_id=NO_FIRST, text="她离开")
    held = GraphExecutor(conn).run_node(project_id, "video")
    assert held["status"] == "unavailable"
    assert held["quote"]["min"] is None
    assert held["quote"]["max"] is None
    assert _rows(conn, project_id) == []


def test_deleted_frame_file_is_refused_before_the_ledger_write(conn):
    project_id, hero = _project(conn)
    portrait = _portrait(conn, hero["id"])
    executor = GraphExecutor(conn, video_adapter=RecordingAdapter())
    executor.run_node(project_id, "video", {"first_frame_asset_id": hero["id"]})
    import os

    os.remove(portrait)
    with pytest.raises(_GateError, match="首帧资产不在资产库中"):
        executor.confirm(project_id, "video")
    assert _rows(conn, project_id) == []
