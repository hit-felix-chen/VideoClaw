"""Small fixtures shared by the backend suite."""

import pytest

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Working directory for agents that store files under a relative code/result tree."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    """Point result, temp, and sandbox storage at tmp_path."""
    from config import settings
    import api.routers.sandbox as sandbox

    saved = {
        "temp": settings.TEMP_DIR,
        "result": settings.RESULT_DIR,
        "sandbox_dir": sandbox.SANDBOX_DIR,
        "sandbox_history": sandbox.SANDBOX_HISTORY_FILE,
        "sandbox_tasks": dict(sandbox.SANDBOX_ACTIVE_TASKS),
    }

    result = tmp_path / "result"
    temp = tmp_path / "temp"
    sandbox_dir = tmp_path / "sandbox"
    for path in (result / "script", result / "image", result / "video", temp, sandbox_dir):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "TEMP_DIR", str(temp), raising=False)
    monkeypatch.setattr(settings, "RESULT_DIR", str(result), raising=False)
    sandbox.SANDBOX_DIR = str(sandbox_dir)
    sandbox.SANDBOX_HISTORY_FILE = str(sandbox_dir / "history.json")
    sandbox.SANDBOX_ACTIVE_TASKS.clear()

    yield {
        "root": tmp_path,
        "result": result,
        "temp": temp,
    }

    sandbox.SANDBOX_DIR = saved["sandbox_dir"]
    sandbox.SANDBOX_HISTORY_FILE = saved["sandbox_history"]
    sandbox.SANDBOX_ACTIVE_TASKS.clear()
    sandbox.SANDBOX_ACTIVE_TASKS.update(saved["sandbox_tasks"])


@pytest.fixture
def client(isolated_runtime):
    """HTTP client. Sandbox tests stub model clients themselves."""
    from fastapi.testclient import TestClient

    from api.app import app

    with TestClient(app) as test_client:
        yield test_client
