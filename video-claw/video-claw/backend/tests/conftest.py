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
    """Point session, result, temp, and sandbox storage at tmp_path.

    The process-wide engine and settings objects are restored after the test.
    """
    from api.dependencies import workflow_engine
    from config import settings
    import api.routers.sandbox as sandbox

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
    }

    session_dir = tmp_path / "sessions"
    result = tmp_path / "result"
    temp = tmp_path / "temp"
    sandbox_dir = tmp_path / "sandbox"
    for path in (session_dir, result / "script", result / "image", result / "video", temp, sandbox_dir):
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


@pytest.fixture
def client(isolated_runtime, monkeypatch):
    """HTTP client. Model execution is stubbed so tests never call a provider."""
    from fastapi.testclient import TestClient

    from api.app import app

    WorkflowStubs.silence_execution(monkeypatch)
    with TestClient(app) as test_client:
        yield test_client
