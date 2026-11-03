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
RESULT_URL = "https://cdn.example/seedance.mp4"


class RecordingAdapter:
    """Fake video adapter. ``submit`` records the prepared request."""

    def __init__(self, result=RESULT_URL, error=None):
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


def _ready_video(conn, cap=200, shot_duration=30, text="@林夏 走进教室"):
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
            "text": text,
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


def _portrait(conn, asset_id, payload=None, look_id=None) -> str:
    from storage import BlobStore, ImageStore
    from tests.support import SampleMedia

    data = SampleMedia.png_bytes() if payload is None else payload
    blob = BlobStore.put(conn, data, mime="image/png", kind="image")
    ImageStore.create(
        conn,
        asset_id=asset_id,
        look_id=look_id,
        blob_hash=blob["hash"],
        source="uploaded",
        prompt="林夏",
    )
    return blob["path"]


def test_confirm_submits_the_seedance_reference_request_once(conn):
    project_id, hero = _ready_video(conn)
    portrait = _portrait(conn, hero["id"])
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
    assert params["reference_image_paths"] == [portrait]
    assert hero["id"] not in params["reference_image_paths"]
    assert "林夏二" not in "".join(params["reference_image_paths"])
    assert params["duration"] == 30
    assert params["prompt"] == "@林夏 走进教室"
    assert params["save_path"].endswith(".mp4")
    assert call["transport"] is None

    assert done["status"] == "done"
    assert done["call_confirmed"] is True
    assert done["spent"] is True
    assert done["provider_called"] is True
    assert done["result_id"] == RESULT_URL
    stored = _metadata(conn, project_id)["result"]
    assert stored["model_id"] == SEEDANCE
    assert stored["generation_mode"] == "reference"
    assert stored["reference_image_paths"] == [hero["id"]]
    assert stored["duration"] == 30
    assert stored["result_id"] == RESULT_URL
    assert stored["save_path"].endswith(".mp4")
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
    portrait = _portrait(conn, hero["id"])
    adapter = RecordingAdapter(result="https://cdn.example/after-raise.mp4")
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
    assert params["reference_image_paths"] == [portrait]
    assert params["duration"] == 30
    assert done["status"] == "done"
    assert done["provider_called"] is True
    assert done["spent"] is True
    assert done["result_id"] == "https://cdn.example/after-raise.mp4"
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == SPENT
    assert rows[0]["actual_amount"] == 70.0
    assert not any(row["status"] == QUOTED for row in rows)


@pytest.mark.parametrize("mode", ["raise", "empty", "error-dict", "bare-id"])
def test_submit_failure_does_not_spend_or_leave_a_quoted_row(conn, mode):
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    if mode == "raise":
        adapter = RecordingAdapter(error=RuntimeError("ark down"))
    elif mode == "empty":
        adapter = RecordingAdapter(result="")
    elif mode == "bare-id":
        adapter = RecordingAdapter(result="cgt-task-1")
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
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    adapter = RecordingAdapter(error=RuntimeError("ark down"))
    executor = GraphExecutor(conn, video_adapter=adapter)
    executor.run_node(project_id, "video")
    with pytest.raises(StorageError):
        executor.confirm(project_id, "video")
    assert _status(conn, project_id) == "awaiting_user"
    assert _metadata(conn, project_id)["submit_error"] == "ark down"

    adapter.error = None
    adapter.result = "https://cdn.example/retry.mp4"
    done = executor.confirm(project_id, "video")
    assert len(adapter.calls) == 2
    assert done["status"] == "done"
    assert done["provider_called"] is True
    assert done["spent"] is True
    assert done["result_id"] == "https://cdn.example/retry.mp4"
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
        return "https://cdn.example/ark.mp4"

    monkeypatch.setattr("models.middle.providers.ArkVideoAdapter.submit", fake_submit)
    monkeypatch.setattr(Config, "ARK_API_KEY", "unit-test-key")
    project_id, hero = _ready_video(conn)
    portrait = _portrait(conn, hero["id"])
    executor = GraphExecutor(conn)
    executor.run_node(project_id, "video")
    assert seen == []

    done = executor.confirm(project_id, "video")
    assert len(seen) == 1
    assert seen[0]["adapter"] == "ArkVideoAdapter"
    assert seen[0]["model_id"] == SEEDANCE
    assert seen[0]["mode"] == "reference"
    assert seen[0]["refs"] == [portrait]
    assert seen[0]["duration"] == 30
    assert done["provider_called"] is True
    assert done["status"] == "done"
    assert done["result_id"] == "https://cdn.example/ark.mp4"
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
    project_id, hero = _ready_video(conn)
    executor = GraphExecutor(conn)
    held = executor.run_node(project_id, "video")
    done = executor.confirm(project_id, "video")
    assert held["provider_called"] is False
    assert seen == []
    assert done["provider_called"] is False
    assert done["status"] == "done"
    assert done["spent"] is True
    assert "result_id" not in done
    assert _metadata(conn, project_id)["result"]["reference_image_paths"] == [hero["id"]]
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == SPENT


def test_shot_duration_outside_the_model_range_submits_auto(conn):
    project_id, hero = _ready_video(conn, shot_duration=99)
    _portrait(conn, hero["id"])
    adapter = RecordingAdapter()
    executor = GraphExecutor(conn, video_adapter=adapter)
    held = executor.run_node(project_id, "video", {"duration": 99})
    assert held["quote"]["max"] == 70.0
    done = executor.confirm(project_id, "video")
    assert done["provider_called"] is True
    assert adapter.calls[0]["params"]["duration"] == -1
    assert adapter.calls[0]["max_seconds"] == 30


def test_missing_reference_file_skips_the_quote_and_does_not_submit(conn):
    project_id, _hero = _ready_video(conn)
    adapter = RecordingAdapter()
    executor = GraphExecutor(conn, video_adapter=adapter)
    executor.run_node(project_id, "video")
    with pytest.raises(StorageError) as failed:
        executor.confirm(project_id, "video")
    assert "reference image file is missing" in str(failed.value)
    assert adapter.calls == []
    assert _status(conn, project_id) == "awaiting_user"
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == SKIPPED
    assert rows[0]["actual_amount"] is None
    assert not any(row["status"] in {SPENT, QUOTED} for row in rows)

    other, other_hero = _ready_video(conn)
    path = _portrait(conn, other_hero["id"])
    from pathlib import Path

    Path(path).unlink()
    other_adapter = RecordingAdapter()
    other_executor = GraphExecutor(conn, video_adapter=other_adapter)
    other_executor.run_node(other, "video")
    with pytest.raises(StorageError) as missing_file:
        other_executor.confirm(other, "video")
    assert "reference image file is missing" in str(missing_file.value)
    assert other_adapter.calls == []
    assert _status(conn, other) == "awaiting_user"
    assert _video_rows(conn, other)[0]["status"] == SKIPPED


def test_mentioned_look_image_is_preferred_over_the_asset_image(conn):
    from storage import LookStore
    from tests.support import SampleMedia

    project_id, hero = _ready_video(conn, text="@林夏/夜 走进教室")
    look = LookStore.create(conn, asset_id=hero["id"], name="夜", prompt="night")
    asset_path = _portrait(conn, hero["id"], payload=SampleMedia.png_bytes() + b"asset")
    look_path = _portrait(
        conn,
        hero["id"],
        payload=SampleMedia.png_bytes() + b"look",
        look_id=look["id"],
    )
    adapter = RecordingAdapter()
    executor = GraphExecutor(conn, video_adapter=adapter)
    executor.run_node(project_id, "video")
    done = executor.confirm(project_id, "video")
    assert done["provider_called"] is True
    assert adapter.calls[0]["params"]["reference_image_paths"] == [look_path]
    assert asset_path not in adapter.calls[0]["params"]["reference_image_paths"]
    assert _metadata(conn, project_id)["result"]["reference_image_paths"] == [hero["id"]]


def test_submit_error_redacts_the_ark_key(conn, monkeypatch):
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    secret = "ark-live-key-should-not-leak"
    monkeypatch.setattr(Config, "ARK_API_KEY", secret)
    adapter = RecordingAdapter(error=RuntimeError(f"unauthorized {secret}"))
    executor = GraphExecutor(conn, video_adapter=adapter)
    executor.run_node(project_id, "video")
    with pytest.raises(StorageError) as failed:
        executor.confirm(project_id, "video")
    assert secret not in str(failed.value)
    assert "***" in str(failed.value)
    assert _metadata(conn, project_id)["submit_error"] == "unauthorized ***"
    assert secret not in _metadata(conn, project_id)["submit_error"]


def test_concurrent_confirms_submit_and_spend_once(conn):
    import threading
    import time

    project_id, hero = _ready_video(conn, cap=200)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    data_dir = Database.data_dir_of(conn)

    calls = []
    outcomes = []
    entered = threading.Event()
    release = threading.Event()
    start = threading.Barrier(2)
    lock = threading.Lock()

    class SlowAdapter:
        def submit(self, spec, params, *, transport=None):
            with lock:
                calls.append(spec.id)
            entered.set()
            assert release.wait(5)
            return RESULT_URL

    def worker():
        start.wait(5)
        connection = Database.connect(data_dir)
        try:
            GraphExecutor(connection, video_adapter=SlowAdapter()).confirm(project_id, "video")
            with lock:
                outcomes.append("ok")
        except StorageError as exc:
            with lock:
                outcomes.append(str(exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert entered.wait(5)
    deadline = time.time() + 2
    while time.time() < deadline:
        with lock:
            if len(outcomes) >= 1 and len(calls) == 1:
                break
        time.sleep(0.02)
    with lock:
        assert calls == [SEEDANCE]
        assert len(outcomes) == 1
        assert "already in progress" in outcomes[0]
    release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert outcomes.count("ok") == 1
    assert sum("already in progress" in item for item in outcomes) == 1
    rows = _video_rows(conn, project_id)
    spent = [row for row in rows if row["status"] == SPENT]
    assert len(spent) == 1
    assert spent[0]["actual_amount"] == 70.0
    assert sum(row["actual_amount"] or 0 for row in rows) == 70.0
    assert not any(row["status"] == QUOTED for row in rows)


def test_stale_submit_claim_can_be_confirmed_again(conn):
    from graph.model import NodeStatus

    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    graph.transition("video", NodeStatus.RUNNING)
    GraphStore.save(conn, project_id, graph.to_dict())
    conn.execute(
        """
        UPDATE node_state SET status = ?, updated_at = ?
        WHERE project_id = ? AND node_id = ?
        """,
        ("running", "2000-01-01T00:00:00.000000Z", project_id, "video"),
    )
    conn.commit()
    released = GraphExecutor.recover_stale_submit_claims(conn)
    assert released == ["video"]
    assert _status(conn, project_id) == "awaiting_user"

    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    graph.transition("video", NodeStatus.RUNNING)
    GraphStore.save(conn, project_id, graph.to_dict())
    conn.execute(
        """
        UPDATE node_state SET status = ?, updated_at = ?
        WHERE project_id = ? AND node_id = ?
        """,
        ("running", "2000-01-01T00:00:00.000000Z", project_id, "video"),
    )
    conn.commit()
    adapter = RecordingAdapter()
    done = GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert len(adapter.calls) == 1
    assert done["provider_called"] is True
    assert done["status"] == "done"
    spent = [row for row in _video_rows(conn, project_id) if row["status"] == SPENT]
    assert len(spent) == 1


def test_fresh_submit_claim_is_rejected(conn):
    from graph.model import NodeStatus

    project_id, _hero = _ready_video(conn)
    GraphExecutor(conn).run_node(project_id, "video")
    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    graph.transition("video", NodeStatus.RUNNING)
    GraphStore.save(conn, project_id, graph.to_dict())
    conn.execute(
        """
        UPDATE node_state SET status = ?, updated_at = ?
        WHERE project_id = ? AND node_id = ?
        """,
        ("running", "2099-01-01T00:00:00.000000Z", project_id, "video"),
    )
    conn.commit()
    adapter = RecordingAdapter()
    with pytest.raises(StorageError) as rejected:
        GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert "already in progress" in str(rejected.value)
    assert adapter.calls == []
    assert _video_rows(conn, project_id) == []


def test_impact_batch_provider_called_follows_the_video_node(conn):
    from graph.model import NodeStatus
    from storage import LookStore

    project = ProjectStore.create(conn, "impact", budget_cap=200)
    asset = AssetStore.create(conn, project_id=project["id"], name="林夏", kind="character")
    look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
    graph = DependencyGraph()
    graph.add_node(asset["id"], kind="character")
    graph.add_node(
        "shot",
        kind="shot_segment",
        metadata={
            "model_id": SEEDANCE,
            "resolution": "720p",
            "includes_video_input": False,
            "completion_tokens": 1_000_000,
            "shot_duration": 6,
            "text": "无参考",
        },
    )
    graph.add_edge(asset["id"], "shot")
    graph.transition(asset["id"], NodeStatus.RUNNING)
    graph.transition(asset["id"], NodeStatus.DONE)
    graph.transition("shot", NodeStatus.RUNNING)
    graph.transition("shot", NodeStatus.DONE)
    GraphStore.save(conn, project["id"], graph.to_dict())
    LookStore.update(conn, look["id"], prompt="night")
    adapter = RecordingAdapter()
    issued = GraphExecutor(conn, video_adapter=adapter).impact(project["id"], look["id"])
    assert "shot" in issued["impact_nodes"]
    done = GraphExecutor(conn, video_adapter=adapter).confirm_impact(
        project["id"],
        look["id"],
        issued["confirm_token"],
    )
    assert done["provider_called"] is True
    assert any(item.get("provider_called") is True for item in done["nodes"])
    assert len(adapter.calls) == 1
