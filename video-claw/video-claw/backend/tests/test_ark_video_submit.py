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
    import models.video_seedance as video_seedance

    def explode(*_args, **_kwargs):
        raise AssertionError("live http")

    monkeypatch.setattr(video_seedance.requests, "post", explode)
    monkeypatch.setattr(video_seedance.requests, "get", explode)
    submitted.clear()
    GraphExecutor.save_probe = None
    yield
    submitted.clear()
    GraphExecutor.save_probe = None


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
    assert "ark down" in _metadata(conn, project_id)["submit_error"]
    assert _metadata(conn, project_id)["submit_dispatched"] is True
    assert "retry_video_submit" in _metadata(conn, project_id)["submit_error"]

    with pytest.raises(StorageError):
        executor.confirm(project_id, "video")
    assert len(adapter.calls) == 1

    adapter.error = None
    adapter.result = "https://cdn.example/retry.mp4"
    done = executor.retry_video_submit(project_id, "video")
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
    project_id, _hero = _ready_video(conn)
    executor = GraphExecutor(conn)
    held = executor.run_node(project_id, "video")
    assert held["provider_called"] is False
    assert held["status"] == "awaiting_user"
    from core.executor import _NoVendorKey

    with pytest.raises(_NoVendorKey) as refused:
        executor.confirm(project_id, "video")
    assert str(refused.value) == "未配置视频厂商密钥，不会提交也不会入账"
    assert seen == []
    assert _status(conn, project_id) == "awaiting_user"
    assert _metadata(conn, project_id).get("submit_dispatched") is None
    assert _video_rows(conn, project_id) == []


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
    assert failed.value.__cause__ is None
    assert "unauthorized ***" in _metadata(conn, project_id)["submit_error"]
    assert _metadata(conn, project_id)["submit_dispatched"] is True
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


def _leave_quoted(conn, project_id, amount=70.0):
    """A reserved quote left behind by a confirm that was killed mid-submit."""
    return LedgerStore.add(
        conn,
        project_id,
        kind="video",
        status=QUOTED,
        model=SEEDANCE,
        node_id="video",
        quote_amount=amount,
        currency="CNY",
    )


def _stamp_running(conn, project_id, *, dispatched=False, vendor_task_id=None):
    from graph.model import NodeStatus

    graph = DependencyGraph.from_dict(GraphStore.load(conn, project_id))
    if graph.node("video").status != NodeStatus.RUNNING:
        graph.transition("video", NodeStatus.RUNNING)
    if dispatched or vendor_task_id:
        graph.update_metadata("video", submit_dispatched=True)
    if vendor_task_id:
        graph.update_metadata("video", vendor_task_id=vendor_task_id)
    GraphStore.save(conn, project_id, graph.to_dict())
    conn.execute(
        """
        UPDATE node_state SET status = ?, updated_at = ?
        WHERE project_id = ? AND node_id = ?
        """,
        ("running", "2000-01-01T00:00:00.000000Z", project_id, "video"),
    )


def test_stale_submit_claim_can_be_confirmed_again(conn):
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    quoted = _leave_quoted(conn, project_id)
    _stamp_running(conn, project_id, dispatched=True)
    released = GraphExecutor.recover_stale_submit_claims(conn)
    assert released == ["video"]
    assert _status(conn, project_id) == "awaiting_user"
    assert _metadata(conn, project_id)["submit_dispatched"] is True
    assert "may have reached the vendor" in _metadata(conn, project_id)["submit_error"]
    still_quoted = _video_rows(conn, project_id)
    assert len(still_quoted) == 1
    assert still_quoted[0]["id"] == quoted["id"]
    assert still_quoted[0]["status"] == QUOTED

    adapter = RecordingAdapter()
    with pytest.raises(StorageError) as blocked:
        GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert adapter.calls == []
    assert "may have reached the vendor" in str(blocked.value)
    assert _status(conn, project_id) == "awaiting_user"
    assert not any(row["status"] == SPENT for row in _video_rows(conn, project_id))

    done = GraphExecutor(conn, video_adapter=adapter).retry_video_submit(project_id, "video")
    assert len(adapter.calls) == 1
    assert done["provider_called"] is True
    assert done["status"] == "done"
    assert _metadata(conn, project_id).get("submit_dispatched") is None
    rows = _video_rows(conn, project_id)
    spent = [row for row in rows if row["status"] == SPENT]
    assert len(spent) == 1
    assert spent[0]["id"] == quoted["id"]
    assert not any(row["status"] == QUOTED for row in rows)


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


def test_dispatched_marker_blocks_submit(conn):
    project_id, _hero = _ready_video(conn)
    GraphExecutor(conn).run_node(project_id, "video")
    _stamp_running(conn, project_id, dispatched=True)
    adapter = RecordingAdapter()
    with pytest.raises(StorageError) as blocked:
        GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert adapter.calls == []
    assert "retry_video_submit" in str(blocked.value)
    assert _metadata(conn, project_id)["submit_dispatched"] is True
    assert _status(conn, project_id) == "awaiting_user"


def test_shrunk_ttl_does_not_let_a_second_confirm_submit(conn, monkeypatch):
    import threading
    import time

    import core.executor as executor_module

    monkeypatch.setattr(executor_module, "VIDEO_SUBMIT_CLAIM_TTL_SECONDS", 0)
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    data_dir = Database.data_dir_of(conn)
    calls = []
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    class SlowAdapter:
        def submit(self, spec, params, *, transport=None):
            with lock:
                calls.append(spec.id)
            entered.set()
            assert release.wait(5)
            raise RuntimeError("first attempt failed after the claim went stale")

    def worker():
        connection = Database.connect(data_dir)
        try:
            GraphExecutor(connection, video_adapter=SlowAdapter()).confirm(project_id, "video")
        except StorageError:
            pass
        finally:
            connection.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(5)
    time.sleep(0.05)
    adapter = RecordingAdapter()
    with pytest.raises(StorageError) as blocked:
        GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert adapter.calls == []
    assert calls == [SEEDANCE]
    assert "may have reached the vendor" in str(blocked.value)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert calls == [SEEDANCE]
    assert _status(conn, project_id) == "awaiting_user"
    assert _metadata(conn, project_id)["submit_dispatched"] is True


def test_old_thread_failure_does_not_overwrite_done(conn):
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    done = GraphExecutor(conn, video_adapter=RecordingAdapter()).confirm(project_id, "video")
    assert done["status"] == "done"
    payload = GraphStore.load(conn, project_id)
    for node in payload["nodes"]:
        if node["id"] == "video":
            node["status"] = "running"
    stale = DependencyGraph.from_dict(payload)
    executor = GraphExecutor(conn)
    executor._claim_stamps["video"] = "1999-01-01T00:00:00.000000Z"
    executor._release_submit_claim(project_id, stale, "video")
    assert _status(conn, project_id) == "done"
    assert _metadata(conn, project_id)["result"]["result_id"] == RESULT_URL


def test_ready_quote_is_claimed_instead_of_left_dangling(conn):
    from models.middle.budget import ProjectBudget

    project_id, hero = _ready_video(conn, cap=10)
    _portrait(conn, hero["id"])
    paused = GraphExecutor(conn).run_node(project_id, "video")
    assert paused["gate"] == "confirm"
    held = GraphExecutor(conn, allow_offline_video=True).confirm(project_id, "video")
    assert held["gate"] == "budget"
    ProjectBudget.raise_cap(conn, project_id, held["entry_id"], 80)
    state = conn.execute(
        "SELECT status FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, "video"),
    ).fetchone()
    assert state["status"] == "ready"
    adapter = RecordingAdapter()
    done = GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert len(adapter.calls) == 1
    assert done["status"] == "done"
    assert done["spent"] is True
    spent = [row for row in _video_rows(conn, project_id) if row["status"] == SPENT]
    assert len(spent) == 1
    assert spent[0]["actual_amount"] == 70.0


def test_poll_deadline_is_under_the_claim_ttl_and_stops(monkeypatch):
    import time

    import models.video_seedance as video_seedance
    from core.executor import VIDEO_SUBMIT_CLAIM_TTL_SECONDS

    assert video_seedance.SUBMIT_DEADLINE_SECONDS == 20 * 60
    assert video_seedance.SUBMIT_DEADLINE_SECONDS < VIDEO_SUBMIT_CLAIM_TTL_SECONDS
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("poll got past the deadline")

    monkeypatch.setattr(video_seedance.requests, "get", fake_get)
    client = video_seedance.SeedanceVideoClient(api_key="x", base_url="https://example.invalid")
    with pytest.raises(TimeoutError):
        client._poll_until_done("task-1", deadline=time.monotonic() - 1)
    assert calls == []


class _Http:
    def __init__(self, payload, status=200, stream_error=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = str(payload)
        self._payload = payload
        self._stream_error = stream_error

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.ok:
            return
        import requests

        error = requests.HTTPError(f"{self.status_code}")
        error.response = self
        raise error

    def iter_content(self, chunk_size=8192):
        if self._stream_error is not None:
            raise self._stream_error
        yield b"mp4"


def _install_seedance_http(monkeypatch, *, post, get, deadline=None):
    import models.video_seedance as video_seedance

    monkeypatch.setattr(video_seedance.requests, "post", post)
    monkeypatch.setattr(video_seedance.requests, "get", get)
    monkeypatch.setattr(video_seedance.time, "sleep", lambda *_args, **_kwargs: None)
    if deadline is not None:
        monkeypatch.setattr(video_seedance, "SUBMIT_DEADLINE_SECONDS", deadline)


def _count_ark_submit(monkeypatch):
    from models.middle.providers import ArkVideoAdapter

    calls = []
    original = ArkVideoAdapter.submit

    def wrapped(self, spec, params, *, transport=None):
        calls.append(params.get("vendor_task_id"))
        return original(self, spec, params, transport=transport)

    monkeypatch.setattr(ArkVideoAdapter, "submit", wrapped)
    return calls


def _confirm_real(conn, project_id):
    from config import Config

    previous = Config.ARK_API_KEY
    Config.ARK_API_KEY = "unit-test-key"
    try:
        return GraphExecutor(conn).confirm(project_id, "video")
    finally:
        Config.ARK_API_KEY = previous


def test_post_timeout_is_bounded_by_the_deadline(monkeypatch):
    import models.video_seedance as video_seedance

    seen = {}

    def fake_post(_url, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return _Http({"id": "task-bound"})

    def fake_get(_url, **kwargs):
        if kwargs.get("stream"):
            return _Http({})
        return _Http({"status": "succeeded", "content": {"video_url": RESULT_URL}})

    _install_seedance_http(monkeypatch, post=fake_post, get=fake_get, deadline=4)
    client = video_seedance.SeedanceVideoClient(api_key="x", base_url="https://example.invalid")
    client.timeout = 120
    client.generate_video("hello", None, "/tmp/seedance-bound.mp4", model=SEEDANCE, duration=5)
    assert seen["timeout"] <= 4
    assert seen["timeout"] < client.timeout


def test_uncertain_seedance_failures_keep_the_marker(conn, monkeypatch):
    import requests

    import models.video_seedance as video_seedance

    monkeypatch.setattr(Config, "ARK_API_KEY", "unit-test-key")
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    calls = _count_ark_submit(monkeypatch)
    posts = []

    def running_get(_url, **kwargs):
        if kwargs.get("stream"):
            return _Http({})
        return _Http({"status": "running"})

    def succeed_get(_url, **kwargs):
        if kwargs.get("stream"):
            return _Http({})
        return _Http({"status": "succeeded", "content": {"video_url": RESULT_URL}})

    scenarios = [
        (
            "poll-timeout",
            lambda: _install_seedance_http(
                monkeypatch,
                post=lambda _url, **kwargs: posts.append(kwargs.get("timeout")) or _Http({"id": "task-poll"}),
                get=running_get,
                deadline=0.2,
            ),
        ),
        (
            "post-read-timeout",
            lambda: _install_seedance_http(
                monkeypatch,
                post=lambda _url, **_kwargs: (_ for _ in ()).throw(requests.ReadTimeout("read timed out")),
                get=succeed_get,
            ),
        ),
        (
            "download-failure",
            lambda: _install_seedance_http(
                monkeypatch,
                post=lambda _url, **_kwargs: _Http({"id": "task-download"}),
                get=lambda _url, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
                if kwargs.get("stream")
                else _Http({"status": "succeeded", "content": {"video_url": RESULT_URL}}),
            ),
        ),
    ]
    # Run each scenario on its own project so a kept marker cannot leak across cases.
    first = True
    for name, install in scenarios:
        if not first:
            project_id, hero = _ready_video(conn)
            _portrait(conn, hero["id"])
            GraphExecutor(conn).run_node(project_id, "video")
        first = False
        posts.clear()
        before = len(calls)
        install()
        with pytest.raises(StorageError) as failed:
            GraphExecutor(conn).confirm(project_id, "video")
        assert name
        assert "retry_video_submit" in str(failed.value)
        meta = _metadata(conn, project_id)
        assert meta["submit_dispatched"] is True
        assert _status(conn, project_id) == "awaiting_user"
        assert len(calls) == before + 1
        with pytest.raises(StorageError):
            GraphExecutor(conn).confirm(project_id, "video")
        assert len(calls) == before + 1
        install()
        if name == "poll-timeout":
            monkeypatch.setattr(video_seedance, "SUBMIT_DEADLINE_SECONDS", 20 * 60)
            monkeypatch.setattr(video_seedance.requests, "get", succeed_get)
        elif name == "post-read-timeout":
            monkeypatch.setattr(
                video_seedance.requests,
                "post",
                lambda _url, **_kwargs: _Http({"id": "task-again"}),
            )
            monkeypatch.setattr(video_seedance.requests, "get", succeed_get)
        else:
            monkeypatch.setattr(video_seedance.requests, "get", succeed_get)
        if name == "poll-timeout":
            assert meta.get("vendor_task_id") == "task-poll"
        done = GraphExecutor(conn).retry_video_submit(project_id, "video")
        assert done["status"] == "done"
        assert len(calls) == before + 2
        if name == "poll-timeout":
            assert len(posts) == 1


@pytest.mark.parametrize("status_name", ["failed", "expired"])
def test_vendor_failed_keeps_the_marker_until_force_new(conn, monkeypatch, status_name):
    monkeypatch.setattr(Config, "ARK_API_KEY", "unit-test-key")
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    posts = []

    def fake_post(_url, **_kwargs):
        posts.append(1)
        return _Http({"id": f"task-{len(posts)}"})

    def fake_get(_url, **kwargs):
        if kwargs.get("stream"):
            return _Http({})
        if len(posts) == 1:
            return _Http({"status": status_name, "error": {"message": "rejected prompt"}})
        return _Http({"status": "succeeded", "content": {"video_url": RESULT_URL}})

    _install_seedance_http(monkeypatch, post=fake_post, get=fake_get)
    with pytest.raises(StorageError) as failed:
        GraphExecutor(conn).confirm(project_id, "video")
    assert str(failed.value) == (
        "vendor judged it failed; a manual retry_video_submit(force_new=True) "
        "is required to create a new one"
    )
    meta = _metadata(conn, project_id)
    assert meta["submit_dispatched"] is True
    assert meta["vendor_task_id"] == "task-1"
    assert _status(conn, project_id) == "awaiting_user"
    assert posts == [1]
    with pytest.raises(StorageError) as blocked:
        GraphExecutor(conn).confirm(project_id, "video")
    assert str(blocked.value) == str(failed.value)
    assert posts == [1]
    with pytest.raises(StorageError):
        GraphExecutor(conn).retry_video_submit(project_id, "video")
    assert posts == [1]
    assert _metadata(conn, project_id)["vendor_task_id"] == "task-1"
    assert _metadata(conn, project_id)["submit_dispatched"] is True
    done = GraphExecutor(conn).retry_video_submit(project_id, "video", force_new=True)
    assert done["status"] == "done"
    assert posts == [1, 1]
    assert _metadata(conn, project_id).get("submit_dispatched") is None


def test_other_node_save_does_not_double_spend(conn):
    import threading

    project = ProjectStore.create(conn, "并行", budget_cap=200)
    project_id = project["id"]
    hero = AssetStore.create(conn, project_id=project_id, name="林夏", kind="character")
    _portrait(conn, hero["id"])
    graph = DependencyGraph()
    graph.add_node("script", kind="script", metadata={"text": "开场"})
    graph.add_node(
        "video",
        kind="shot_segment",
        metadata={
            "model_id": SEEDANCE,
            "resolution": "720p",
            "includes_video_input": False,
            "completion_tokens": 1_000_000,
            "shot_duration": 6,
            "text": "@林夏 走进教室",
        },
    )
    GraphStore.save(conn, project_id, graph.to_dict())
    GraphExecutor(conn).run_node(project_id, "video")
    data_dir = Database.data_dir_of(conn)
    calls = []
    script_entered = threading.Event()
    video_in = threading.Event()
    release_video = threading.Event()

    def runner(call):
        if call.get("node_id") == "script":
            script_entered.set()
            assert video_in.wait(5)
            return {"provider_called": False, "kind": "script"}
        from core.nodes.dispatch import dispatch_node

        return dispatch_node(call)

    class Slow:
        def submit(self, spec, params, *, transport=None):
            calls.append(spec.id)
            video_in.set()
            assert release_video.wait(5)
            return RESULT_URL

    def run_script():
        connection = Database.connect(data_dir)
        try:
            GraphExecutor(connection, runner=runner).run_node(project_id, "script")
        finally:
            connection.close()

    def run_confirm():
        connection = Database.connect(data_dir)
        try:
            GraphExecutor(connection, runner=runner, video_adapter=Slow()).confirm(project_id, "video")
        finally:
            connection.close()

    script_thread = threading.Thread(target=run_script, daemon=True)
    video_thread = threading.Thread(target=run_confirm, daemon=True)
    script_thread.start()
    assert script_entered.wait(5)
    video_thread.start()
    assert video_in.wait(5)
    script_thread.join(5)
    assert not script_thread.is_alive()
    late = Database.connect(data_dir)
    try:
        with pytest.raises(StorageError):
            GraphExecutor(late, runner=runner, video_adapter=Slow()).confirm(project_id, "video")
    finally:
        late.close()
    assert calls == [SEEDANCE]
    release_video.set()
    video_thread.join(5)
    assert not video_thread.is_alive()
    assert calls == [SEEDANCE]
    spent = [row for row in _video_rows(conn, project_id) if row["status"] == SPENT]
    assert len(spent) == 1
    assert spent[0]["actual_amount"] == 70.0


def test_replaced_claim_stamp_does_not_spend_the_returned_url(conn):
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")

    class StampReplacer:
        def submit(self, spec, params, *, transport=None):
            conn.execute(
                """
                UPDATE node_state SET updated_at = ?
                WHERE project_id = ? AND node_id = ?
                """,
                ("2099-01-01T00:00:00.000000Z", project_id, "video"),
            )
            return RESULT_URL

    done = GraphExecutor(conn, video_adapter=StampReplacer()).confirm(project_id, "video")
    assert done["status"] == "awaiting_user"
    assert done["spent"] is False
    assert "manual check needed" in done["message"]
    assert done["result_id"] == RESULT_URL
    meta = _metadata(conn, project_id)
    assert meta["notes"] == "result reached vendor but state was overwritten; manual check needed"
    assert meta["result"]["result_id"] == RESULT_URL
    assert meta["submit_dispatched"] is True
    assert not any(row["status"] == SPENT for row in _video_rows(conn, project_id))
    assert _status(conn, project_id) == "awaiting_user"


@pytest.mark.parametrize(
    "label,poll_status",
    [("poll-400", 400), ("poll-404", 404)],
)
def test_poll_client_errors_keep_the_marker_and_block_confirm(conn, monkeypatch, label, poll_status):
    monkeypatch.setattr(Config, "ARK_API_KEY", "unit-test-key")
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    posts = []

    def fake_post(_url, **_kwargs):
        posts.append(label)
        return _Http({"id": f"task-{label}"})

    def fake_get(_url, **kwargs):
        if kwargs.get("stream"):
            return _Http({})
        return _Http({"error": label}, status=poll_status)

    _install_seedance_http(monkeypatch, post=fake_post, get=fake_get)
    with pytest.raises(StorageError):
        GraphExecutor(conn).confirm(project_id, "video")
    meta = _metadata(conn, project_id)
    assert meta["submit_dispatched"] is True
    assert meta["vendor_task_id"] == f"task-{label}"
    assert _status(conn, project_id) == "awaiting_user"
    with pytest.raises(StorageError):
        GraphExecutor(conn).confirm(project_id, "video")
    assert posts == [label]


def test_download_forbidden_keeps_the_marker_and_blocks_confirm(conn, monkeypatch):
    monkeypatch.setattr(Config, "ARK_API_KEY", "unit-test-key")
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    posts = []

    def fake_post(_url, **_kwargs):
        posts.append(1)
        return _Http({"id": "task-download-403"})

    def fake_get(_url, **kwargs):
        if kwargs.get("stream"):
            return _Http({}, status=403)
        return _Http({"status": "succeeded", "content": {"video_url": RESULT_URL}})

    _install_seedance_http(monkeypatch, post=fake_post, get=fake_get)
    with pytest.raises(StorageError):
        GraphExecutor(conn).confirm(project_id, "video")
    meta = _metadata(conn, project_id)
    assert meta["submit_dispatched"] is True
    assert meta["vendor_task_id"] == "task-download-403"
    with pytest.raises(StorageError):
        GraphExecutor(conn).confirm(project_id, "video")
    assert posts == [1]


@pytest.mark.parametrize(
    "code,body,task_id",
    [
        (400, {"id": "task-400", "error": "bad request"}, "task-400"),
        (409, {"error": "conflict"}, None),
        (499, {"id": "task-499"}, "task-499"),
    ],
)
def test_post_client_errors_keep_the_marker(conn, monkeypatch, code, body, task_id):
    monkeypatch.setattr(Config, "ARK_API_KEY", "unit-test-key")
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    posts = []

    def fake_post(_url, **_kwargs):
        posts.append(code)
        return _Http(body, status=code)

    _install_seedance_http(
        monkeypatch,
        post=fake_post,
        get=lambda _url, **_kwargs: _Http({"status": "succeeded", "content": {"video_url": RESULT_URL}}),
    )
    with pytest.raises(StorageError):
        GraphExecutor(conn).confirm(project_id, "video")
    meta = _metadata(conn, project_id)
    assert meta["submit_dispatched"] is True
    assert meta.get("vendor_task_id") == task_id
    assert _status(conn, project_id) == "awaiting_user"
    with pytest.raises(StorageError):
        GraphExecutor(conn).confirm(project_id, "video")
    assert posts == [code]


def test_save_probe_cannot_lose_another_connections_claim(conn):
    project_id, _hero = _ready_video(conn)
    stored = GraphStore.load(conn, project_id)
    stored["nodes"].append(
        {
            "id": "script",
            "kind": "script",
            "episode_id": None,
            "status": "pending",
            "version": 0,
            "input_versions": {},
            "pinned": False,
            "label": "",
            "metadata": {"text": "开场"},
        }
    )
    GraphStore.save(conn, project_id, stored)
    GraphExecutor(conn).run_node(project_id, "video")
    executor = GraphExecutor(conn)
    graph = executor._load(project_id)
    graph.update_metadata("script", text="改了")
    data_dir = Database.data_dir_of(conn)

    def probe(_executor):
        other = Database.connect(data_dir)
        try:
            document = GraphStore.load(other, project_id)
            for node in document["nodes"]:
                if node["id"] == "video":
                    node["status"] = "running"
                    node["metadata"]["submit_dispatched"] = True
                    node["metadata"]["vendor_task_id"] = "task-other"
            GraphStore.save(other, project_id, document)
            other.execute(
                """
                UPDATE node_state SET status = ?, updated_at = ?
                WHERE project_id = ? AND node_id = ?
                """,
                ("running", "2099-01-01T00:00:00.000000Z", project_id, "video"),
            )
        finally:
            other.close()

    GraphExecutor.save_probe = probe
    executor._save(project_id, graph)
    GraphExecutor.save_probe = None
    video = _metadata(conn, project_id, "video")
    assert video["submit_dispatched"] is True
    assert video["vendor_task_id"] == "task-other"
    assert _status(conn, project_id, "video") == "running"
    script = _metadata(conn, project_id, "script")
    assert script["text"] == "改了"
    state = conn.execute(
        "SELECT status FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, "video"),
    ).fetchone()
    assert state["status"] == "running"


def test_memory_running_does_not_wipe_a_stored_marker(conn):
    project_id, _hero = _ready_video(conn)
    GraphExecutor(conn).run_node(project_id, "video")
    executor = GraphExecutor(conn)
    memory = executor._load(project_id)
    document = GraphStore.load(conn, project_id)
    for node in document["nodes"]:
        if node["id"] == "video":
            node["metadata"]["submit_dispatched"] = True
            node["metadata"]["vendor_task_id"] = "task-db"
    GraphStore.save(conn, project_id, document)
    memory.transition("video", "running")
    executor._save(project_id, memory)
    meta = _metadata(conn, project_id)
    assert meta["submit_dispatched"] is True
    assert meta["vendor_task_id"] == "task-db"
    assert _status(conn, project_id) == "awaiting_user"


def test_retry_does_not_clear_the_marker_before_the_claim(conn):
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")

    class ResumeAdapter:
        def __init__(self):
            self.calls = []

        def submit(self, spec, params, *, transport=None):
            self.calls.append(params.get("vendor_task_id"))
            on_task = params.get("on_task")
            if not params.get("vendor_task_id"):
                on_task("task-1")
                raise RuntimeError("poll blew up")
            return RESULT_URL

    adapter = ResumeAdapter()
    with pytest.raises(StorageError):
        GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert _metadata(conn, project_id)["submit_dispatched"] is True
    assert _metadata(conn, project_id)["vendor_task_id"] == "task-1"
    seen = []
    original = GraphExecutor._save

    def tracing(self, project, graph, **kwargs):
        if project == project_id:
            meta = _metadata(self.conn, project)
            seen.append(
                {
                    "marker": meta.get("submit_dispatched"),
                    "task": meta.get("vendor_task_id"),
                    "touch_state": kwargs.get("touch_state", True),
                }
            )
        return original(self, project, graph, **kwargs)

    GraphExecutor._save = tracing
    try:
        done = GraphExecutor(conn, video_adapter=adapter).retry_video_submit(project_id, "video")
    finally:
        GraphExecutor._save = original
    assert done["status"] == "done"
    assert adapter.calls == [None, "task-1"]
    assert seen
    assert all(item["marker"] is True for item in seen)
    claim_writes = [item for item in seen if item["touch_state"] is False]
    assert claim_writes
    assert all(item["task"] == "task-1" for item in claim_writes)


def test_stored_url_survives_a_failed_spend_and_is_not_posted_again(conn, monkeypatch):
    from models.middle.budget import ProjectBudget

    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    adapter = RecordingAdapter()
    real = ProjectBudget.record_actual

    def boom(connection, project, entry_id, actual):
        monkeypatch.setattr(ProjectBudget, "record_actual", real)
        raise StorageError("ledger down")

    monkeypatch.setattr(ProjectBudget, "record_actual", boom)
    with pytest.raises(StorageError, match="ledger down"):
        GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    meta = _metadata(conn, project_id)
    assert meta["submit_dispatched"] is True
    assert meta["result_obtained"] == RESULT_URL
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == QUOTED
    assert rows[0]["actual_amount"] is None
    quoted_id = rows[0]["id"]
    assert not any(row["status"] == SPENT for row in rows)
    with pytest.raises(StorageError):
        GraphExecutor(conn, video_adapter=adapter).confirm(project_id, "video")
    assert len(adapter.calls) == 1
    done = GraphExecutor(conn, video_adapter=adapter).retry_video_submit(project_id, "video")
    assert done["status"] == "done"
    assert done["result_id"] == RESULT_URL
    assert len(adapter.calls) == 1
    rows = _video_rows(conn, project_id)
    assert [row["id"] for row in rows] == [quoted_id]
    assert rows[0]["status"] == SPENT
    assert rows[0]["actual_amount"] == 70.0
    assert not any(row["status"] == QUOTED for row in rows)


def test_raise_budget_refuses_when_the_marker_is_set(conn):
    project_id, _hero = _ready_video(conn, cap=10)
    GraphExecutor(conn).run_node(project_id, "video")
    held = GraphExecutor(conn, allow_offline_video=True).confirm(project_id, "video")
    assert held["gate"] == "budget"
    document = GraphStore.load(conn, project_id)
    for node in document["nodes"]:
        if node["id"] == "video":
            node["metadata"]["submit_dispatched"] = True
            node["metadata"]["vendor_task_id"] = "task-budget"
    GraphStore.save(conn, project_id, document)
    cap = conn.execute("SELECT budget_cap FROM project WHERE id = ?", (project_id,)).fetchone()[0]
    with pytest.raises(StorageError) as refused:
        GraphExecutor(conn, allow_offline_video=True).raise_budget(project_id, held["entry_id"], 500)
    assert "retry_video_submit" in str(refused.value)
    assert conn.execute("SELECT budget_cap FROM project WHERE id = ?", (project_id,)).fetchone()[0] == cap
    assert _metadata(conn, project_id)["submit_dispatched"] is True
    assert _metadata(conn, project_id)["vendor_task_id"] == "task-budget"
    assert _video_rows(conn, project_id)[0]["status"] == AWAITING_USER


class _NewPostAdapter:
    """Records each submit. A missing task id is a new POST."""

    def __init__(self):
        self.posts = []

    def submit(self, spec, params, *, transport=None):
        task_id = params.get("vendor_task_id")
        self.posts.append(task_id)
        on_task = params.get("on_task")
        if not task_id and callable(on_task):
            on_task("task-new")
        return RESULT_URL


def _stale_quoted_submit(conn):
    project_id, hero = _ready_video(conn)
    _portrait(conn, hero["id"])
    GraphExecutor(conn).run_node(project_id, "video")
    quoted = _leave_quoted(conn, project_id)
    _stamp_running(conn, project_id, vendor_task_id="task-old")
    return project_id, quoted


def test_force_new_on_a_stale_quote_authorizes_before_the_post(conn, monkeypatch):
    from models.middle.budget import ProjectBudget

    project_id, quoted = _stale_quoted_submit(conn)
    adapter = _NewPostAdapter()
    real = ProjectBudget.authorize

    def wrapped(connection, project, request, **kwargs):
        assert adapter.posts == []
        return real(connection, project, request, **kwargs)

    monkeypatch.setattr(ProjectBudget, "authorize", wrapped)
    done = GraphExecutor(conn, video_adapter=adapter).retry_video_submit(
        project_id,
        "video",
        force_new=True,
    )
    assert adapter.posts == [None]
    assert done["status"] == "done"
    assert done["spent"] is True
    assert "manual check needed" not in done["message"]
    rows = _video_rows(conn, project_id)
    spent = [row for row in rows if row["status"] == SPENT]
    skipped = [row for row in rows if row["status"] == SKIPPED]
    assert len(spent) == 1
    assert spent[0]["actual_amount"] == 70.0
    assert skipped[0]["id"] == quoted["id"]
    assert _metadata(conn, project_id).get("submit_dispatched") is None
    assert _metadata(conn, project_id).get("vendor_task_id") is None


def test_force_new_budget_pause_keeps_the_task_until_the_cap_is_raised(conn):
    project_id, _quoted = _stale_quoted_submit(conn)
    ProjectStore.update(conn, project_id, budget_cap=10)
    adapter = _NewPostAdapter()
    paused = GraphExecutor(conn, video_adapter=adapter).retry_video_submit(
        project_id,
        "video",
        force_new=True,
    )
    assert paused["gate"] == "budget"
    assert adapter.posts == []
    meta = _metadata(conn, project_id)
    assert meta["submit_dispatched"] is True
    assert meta["vendor_task_id"] == "task-old"
    assert meta["force_new_pending"] is True
    assert _status(conn, project_id) == "awaiting_user"
    assert not any(row["status"] == SPENT for row in _video_rows(conn, project_id))
    done = GraphExecutor(conn, video_adapter=adapter).raise_budget(
        project_id,
        paused["entry_id"],
        500,
    )
    assert done["status"] == "done"
    assert done["spent"] is True
    assert adapter.posts == [None]
    spent = [row for row in _video_rows(conn, project_id) if row["status"] == SPENT]
    assert len(spent) == 1
    assert spent[0]["actual_amount"] == 70.0
    assert _metadata(conn, project_id).get("submit_dispatched") is None


def test_stale_retry_without_force_new_spends_the_original_quote(conn, monkeypatch):
    from models.middle.budget import ProjectBudget

    project_id, quoted = _stale_quoted_submit(conn)

    def explode(*_args, **_kwargs):
        raise AssertionError("resume must not authorize a second quote")

    monkeypatch.setattr(ProjectBudget, "authorize", explode)
    adapter = _NewPostAdapter()
    done = GraphExecutor(conn, video_adapter=adapter).retry_video_submit(project_id, "video")
    assert done["status"] == "done"
    assert done["spent"] is True
    assert adapter.posts == ["task-old"]
    rows = _video_rows(conn, project_id)
    assert len(rows) == 1
    assert rows[0]["id"] == quoted["id"]
    assert rows[0]["status"] == SPENT
    assert rows[0]["actual_amount"] == 70.0


def test_raise_budget_refuses_a_task_id_without_the_marker(conn):
    project_id, _hero = _ready_video(conn, cap=10)
    GraphExecutor(conn).run_node(project_id, "video")
    held = GraphExecutor(conn, allow_offline_video=True).confirm(project_id, "video")
    assert held["gate"] == "budget"
    document = GraphStore.load(conn, project_id)
    for node in document["nodes"]:
        if node["id"] == "video":
            node["metadata"]["vendor_task_id"] = "task-only"
    GraphStore.save(conn, project_id, document)
    with pytest.raises(StorageError) as refused:
        GraphExecutor(conn, allow_offline_video=True).raise_budget(project_id, held["entry_id"], 500)
    assert "retry_video_submit" in str(refused.value)
    assert _metadata(conn, project_id)["vendor_task_id"] == "task-only"
    assert _video_rows(conn, project_id)[0]["status"] == AWAITING_USER


def test_seedance_error_body_redacts_the_api_key(conn, monkeypatch, caplog):
    import logging

    import models.video_seedance as video_seedance

    monkeypatch.setattr(Config, "ARK_API_KEY", "super-secret-key")

    def fake_post(_url, **_kwargs):
        return _Http({"error": "rejected super-secret-key"}, status=400)

    monkeypatch.setattr(video_seedance.requests, "post", fake_post)
    client = video_seedance.SeedanceVideoClient(
        api_key="super-secret-key",
        base_url="https://example.invalid",
    )
    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception):
            client._submit_task("hello", None, SEEDANCE, 5)
    assert "super-secret-key" not in caplog.text
    assert "***" in caplog.text
