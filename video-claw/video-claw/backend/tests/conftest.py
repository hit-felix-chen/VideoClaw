"""Small fixtures shared by the backend suite."""

import pytest

from tests.support import WorkflowStubs


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Working directory for agents that store files under a relative code/result tree."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    """Point session, result, temp, sandbox, and task storage at tmp_path.

    The process-wide engine and settings objects are restored after the test.
    """
    from api.dependencies import workflow_engine
    from config import settings
    import api.routers.sandbox as sandbox
    import pipelines.storage as storage

    saved = {
        "sessions": dict(workflow_engine.sessions),
        "session_dir": workflow_engine._session_dir,
        "stop_events": dict(workflow_engine._stop_events),
        "active": set(workflow_engine._active_sessions),
        "temp": settings.TEMP_DIR,
        "result": settings.RESULT_DIR,
        "sandbox_dir": sandbox.SANDBOX_DIR,
        "sandbox_history": sandbox.SANDBOX_HISTORY_FILE,
        "sandbox_tasks": dict(sandbox.SANDBOX_ACTIVE_TASKS),
        "task_data": storage.TASK_DATA_DIR,
        "task_result": storage.TASK_RESULT_DIR,
    }

    session_dir = tmp_path / "sessions"
    result = tmp_path / "result"
    temp = tmp_path / "temp"
    sandbox_dir = tmp_path / "sandbox"
    task_data = tmp_path / "tasks"
    task_result = tmp_path / "task-results"
    for path in (session_dir, result / "script", result / "image", result / "video", temp, sandbox_dir, task_data, task_result):
        path.mkdir(parents=True, exist_ok=True)

    workflow_engine.sessions.clear()
    workflow_engine._stop_events.clear()
    workflow_engine._active_sessions.clear()
    workflow_engine._session_dir = str(session_dir)
    monkeypatch.setattr(settings, "TEMP_DIR", str(temp), raising=False)
    monkeypatch.setattr(settings, "RESULT_DIR", str(result), raising=False)
    sandbox.SANDBOX_DIR = str(sandbox_dir)
    sandbox.SANDBOX_HISTORY_FILE = str(sandbox_dir / "history.json")
    sandbox.SANDBOX_ACTIVE_TASKS.clear()
    storage.TASK_DATA_DIR = str(task_data)
    storage.TASK_RESULT_DIR = str(task_result)

    yield {
        "root": tmp_path,
        "session_dir": session_dir,
        "result": result,
        "temp": temp,
        "engine": workflow_engine,
    }

    workflow_engine.sessions.clear()
    workflow_engine.sessions.update(saved["sessions"])
    workflow_engine._session_dir = saved["session_dir"]
    workflow_engine._stop_events.clear()
    workflow_engine._stop_events.update(saved["stop_events"])
    workflow_engine._active_sessions.clear()
    workflow_engine._active_sessions.update(saved["active"])
    sandbox.SANDBOX_DIR = saved["sandbox_dir"]
    sandbox.SANDBOX_HISTORY_FILE = saved["sandbox_history"]
    sandbox.SANDBOX_ACTIVE_TASKS.clear()
    sandbox.SANDBOX_ACTIVE_TASKS.update(saved["sandbox_tasks"])
    storage.TASK_DATA_DIR = saved["task_data"]
    storage.TASK_RESULT_DIR = saved["task_result"]


@pytest.fixture
def client(isolated_runtime, monkeypatch):
    """HTTP client. Pipeline background work is a no-op so tests never call a model."""
    from fastapi.testclient import TestClient

    from api.app import app

    async def noop_pipeline(task_id, pipeline, params):
        return None

    monkeypatch.setattr("api.routers.pipelines.run_pipeline_task", noop_pipeline)
    WorkflowStubs.silence_execution(monkeypatch)
    with TestClient(app) as test_client:
        yield test_client
