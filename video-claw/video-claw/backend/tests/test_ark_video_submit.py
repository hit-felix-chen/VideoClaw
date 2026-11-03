"""Video confirm submits through the adapter only after the budget authorizes.

The vendor client is patched to explode. Tests inject a recording adapter,
or patch ``ArkVideoAdapter.submit`` when the Ark key is set. No test opens
a network connection.
"""

import pytest

from config import Config
from core.executor import GraphExecutor
from core.nodes.dispatch import submitted
from graph.dependency_graph import DependencyGraph
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.registry import ModelRegistry
from storage import AssetStore, Database, GraphStore, LedgerStore, ProjectStore
from storage.database import StorageError
from storage.ledger import AWAITING_USER, QUOTED, SKIPPED, SPENT

SEEDANCE = DEFAULT_REFERENCE_VIDEO_MODEL


class RecordingAdapter:
    """Fake video adapter. ``submit`` records the prepared request."""

    def __init__(self, result="task-seedance-1", error=None):
        self.calls = []
        self.result = result
        self.error = error

    def submit(self, spec, params, *, transport=None):
        recorded = dict(params)
        refs = recorded.get("reference_image_paths")
        if isinstance(refs, list):
            recorded["reference_image_paths"] = list(refs)
        self.calls.append(
            {
                "model_id": spec.id,
                "max_seconds": None if spec.duration is None else spec.duration.max_seconds,
                "params": recorded,
                "transport": transport,
            }
        )
        if self.error is not None:
            raise self.error
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
    def explode(*_args, **_kwargs):
        raise AssertionError("live video provider called")

    import models.video_seedance as video_seedance

    monkeypatch.setattr(
        video_seedance.SeedanceVideoClient, "generate_video", explode, raising=False
    )
    monkeypatch.setattr(video_seedance.SeedanceVideoClient, "_submit_task", explode, raising=False)
    submitted.clear()
    yield
    submitted.clear()


def _ready_video(conn, cap=200, shot_duration=30):
    project = ProjectStore.create(conn, "第1集", budget_cap=cap)
    project_id = project["id"]
    hero = AssetStore.create(conn, project_id=project_id, name="林夏", kind="character")
    AssetStore.create(conn, project_id=project_id, name="林夏二", kind="character")
    graph = DependencyGraph()
    graph.add_node(
        "video",
        kind="shot_segment",
        episode_id="ep1",
        metadata={
            "model_id": SEEDANCE,
            "resolution": "720p",
            "includes_video_input": False,
            "completion_tokens": 1_000_000,
            "shot_duration": shot_duration,
            "text": "@林夏 走进教室",
        },
    )
    GraphStore.save(conn, project_id, graph.to_dict())
    return project_id, hero


def _status(conn, project_id, node_id="video"):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).status.value


def _metadata(conn, project_id, node_id="video"):
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    return graph.node(node_id).metadata


def _video_rows(conn, project_id):
    return [row for row in LedgerStore.list(conn, project_id) if row["node_id"] == "video"]


def test_confirm_submits_the_seedance_reference_request_once(conn):
    project_id, hero = _ready_video(conn)
    adapter = RecordingAdapter()
    executor = GraphExecutor(conn, video_adapter=adapter)
    spec = ModelRegistry.get(SEEDANCE)
    assert spec.duration.max_seconds == 30

    held = executor.run_node(project_id, "video", {"duration": 99})
    assert held["status"] == "awaiting_user"
    assert held["gate"] == "confirm"
    assert held["call_confirmed"] is False
    assert held["spent"] is False
    assert held["provider_called"] is False
    assert held["quote"]["max"] == 70.0
    assert adapter.calls == []
    assert submitted == []
    assert _video_rows(conn, project_id) == []
    assert _status(conn, project_id) == "awaiting_user"

    done = executor.confirm(project_id, "video")
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["model_id"] == SEEDANCE
    assert call["max_seconds"] == 30
    params = call["params"]
    assert params["model_id"] == SEEDANCE
    assert params["generation_mode"] == "reference"
    assert params["ability"] == "reference_to_video"
    assert params["reference_image_paths"] == [hero["id"]]
    assert "林夏二" not in params["reference_image_paths"]
    assert params["duration"] == 30
    assert params["prompt"] == "@林夏 走进教室"
    assert params["save_path"].endswith(".mp4")
    assert call["transport"] is None

    assert done["status"] == "done"
    assert done["call_confirmed"] is True
    assert done["spent"] is True
    assert done["provider_called"] is True
    assert done["result_id"] == "task-seedance-1"
    stored = _metadata(conn, project_id)["result"]
    assert stored["model_id"] == SEEDANCE
    assert stored["generation_mode"] == "reference"
    assert stored["reference_image_paths"] == [hero["id"]]
    assert stored["duration"] == 30
    assert stored["result_id"] == "task-seedance-1"
    assert "provider_called" not in stored
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == SPENT
    assert rows[0]["kind"] == "video"
    assert rows[0]["actual_amount"] == 70.0
    assert _status(conn, project_id) == "done"
    assert [item["node_id"] for item in submitted] == ["video"]
    assert submitted[0]["generation_mode"] == "reference"
    assert submitted[0]["model_id"] == SEEDANCE


def test_submit_waits_until_authorize_then_uses_the_same_request(conn):
    project_id, hero = _ready_video(conn, cap=10)
    adapter = RecordingAdapter(result="task-after-raise")
    executor = GraphExecutor(conn, video_adapter=adapter)

    held = executor.run_node(project_id, "video", {"duration": 15})
    assert held["gate"] == "confirm"
    assert held["over_budget"] is True
    assert adapter.calls == []

    paused = executor.confirm(project_id, "video")
    assert paused["status"] == "awaiting_user"
    assert paused["gate"] == "budget"
    assert paused["spent"] is False
    assert paused["provider_called"] is False
    assert adapter.calls == []
    paused_row = LedgerStore.get(conn, paused["entry_id"])
    assert paused_row["status"] == AWAITING_USER
    assert paused_row["actual_amount"] is None
    assert _status(conn, project_id) != "done"

    done = executor.raise_budget(project_id, paused["entry_id"], 80)
    assert len(adapter.calls) == 1
    params = adapter.calls[0]["params"]
    assert adapter.calls[0]["model_id"] == SEEDANCE
    assert params["generation_mode"] == "reference"
    assert params["reference_image_paths"] == [hero["id"]]
    assert params["duration"] == 30
    assert done["status"] == "done"
    assert done["provider_called"] is True
    assert done["spent"] is True
    assert done["result_id"] == "task-after-raise"
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == SPENT
    assert rows[0]["actual_amount"] == 70.0
    assert not any(row["status"] == QUOTED for row in rows)


@pytest.mark.parametrize("mode", ["raise", "empty", "error-dict"])
def test_submit_failure_does_not_spend_or_leave_a_quoted_row(conn, mode):
    project_id, _hero = _ready_video(conn)
    if mode == "raise":
        adapter = RecordingAdapter(error=RuntimeError("ark down"))
    elif mode == "empty":
        adapter = RecordingAdapter(result="")
    else:
        adapter = RecordingAdapter(result={"error": "rejected"})
    executor = GraphExecutor(conn, video_adapter=adapter)
    executor.run_node(project_id, "video")
    assert adapter.calls == []

    with pytest.raises(StorageError) as failed:
        executor.confirm(project_id, "video")

    assert "video submit failed" in str(failed.value)
    if mode == "raise":
        assert "ark down" in str(failed.value)
    else:
        assert "returned no result" in str(failed.value)
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["model_id"] == SEEDANCE
    assert adapter.calls[0]["params"]["generation_mode"] == "reference"
    assert _status(conn, project_id) == "awaiting_user"
    assert _status(conn, project_id) != "done"
    metadata = _metadata(conn, project_id)
    assert metadata.get("result") is None
    assert metadata.get("pending_call")
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == SKIPPED
    assert rows[0]["actual_amount"] is None
    assert not any(row["status"] in {SPENT, QUOTED} for row in LedgerStore.list(conn, project_id))


def test_failed_submit_can_be_confirmed_again(conn):
    project_id, _hero = _ready_video(conn)
    adapter = RecordingAdapter(error=RuntimeError("ark down"))
    executor = GraphExecutor(conn, video_adapter=adapter)
    executor.run_node(project_id, "video")
    with pytest.raises(StorageError):
        executor.confirm(project_id, "video")
    assert _status(conn, project_id) == "awaiting_user"
    assert _metadata(conn, project_id)["submit_error"] == "ark down"

    adapter.error = None
    adapter.result = "task-seedance-2"
    done = executor.confirm(project_id, "video")
    assert len(adapter.calls) == 2
    assert done["status"] == "done"
    assert done["provider_called"] is True
    assert done["spent"] is True
    assert done["result_id"] == "task-seedance-2"
    assert _metadata(conn, project_id).get("submit_error") is None
    rows = _video_rows(conn, project_id)
    skipped = [row for row in rows if row["status"] == SKIPPED]
    spent = [row for row in rows if row["status"] == SPENT]
    assert len(skipped) == 1
    assert skipped[0]["actual_amount"] is None
    assert len(spent) == 1
    assert spent[0]["actual_amount"] == 70.0
    assert not any(row["status"] == QUOTED for row in rows)


def test_configured_ark_key_calls_ark_video_adapter(conn, monkeypatch):
    seen = []

    def fake_submit(self, spec, params, *, transport=None):
        seen.append(
            {
                "adapter": type(self).__name__,
                "model_id": spec.id,
                "mode": params.get("generation_mode"),
                "refs": list(params.get("reference_image_paths") or []),
                "duration": params.get("duration"),
            }
        )
        return "ark-task-9"

    monkeypatch.setattr("models.middle.providers.ArkVideoAdapter.submit", fake_submit)
    monkeypatch.setattr(Config, "ARK_API_KEY", "unit-test-key")
    project_id, hero = _ready_video(conn)
    executor = GraphExecutor(conn)
    executor.run_node(project_id, "video")
    assert seen == []

    done = executor.confirm(project_id, "video")
    assert len(seen) == 1
    assert seen[0]["adapter"] == "ArkVideoAdapter"
    assert seen[0]["model_id"] == SEEDANCE
    assert seen[0]["mode"] == "reference"
    assert seen[0]["refs"] == [hero["id"]]
    assert seen[0]["duration"] == 30
    assert done["provider_called"] is True
    assert done["status"] == "done"
    assert done["result_id"] == "ark-task-9"
    assert done["spent"] is True
    assert len(_video_rows(conn, project_id)) == 1
    assert _video_rows(conn, project_id)[0]["status"] == SPENT


def test_missing_ark_key_does_not_call_the_vendor(conn, monkeypatch):
    seen = []

    def fake_submit(self, spec, params, *, transport=None):
        seen.append(spec.id)
        return "should-not-run"

    monkeypatch.setattr("models.middle.providers.ArkVideoAdapter.submit", fake_submit)
    monkeypatch.setattr(Config, "ARK_API_KEY", "")
    project_id, _hero = _ready_video(conn)
    executor = GraphExecutor(conn)
    held = executor.run_node(project_id, "video")
    done = executor.confirm(project_id, "video")
    assert held["provider_called"] is False
    assert seen == []
    assert done["provider_called"] is False
    assert done["status"] == "done"
    assert done["spent"] is True
    assert "result_id" not in done
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == SPENT
