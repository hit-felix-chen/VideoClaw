"""Request and response contracts for the routers that remain.

The session, stage, and workflow routers are gone with the stage machine.
"""

from api.logging_config import apply_access_log_setting, apply_log_level_setting
from tests.support import ConfigState, FakeModelClients, SampleMedia


class TestHealth:
    def test_root_and_health(self, client):
        root = client.get("/")
        assert root.status_code == 200
        assert root.json()["service"] == "Video-Claw"
        assert root.json()["health"] == "/api/health"


class TestFileRoutes:
    def test_upload_file_and_media_and_clear_temp(self, client, isolated_runtime):
        rejected = client.post(
            "/api/upload_file",
            files={"file": ("picture.png", SampleMedia.png_bytes(), "image/png")},
        )
        assert rejected.status_code == 400

        accepted = client.post(
            "/api/upload_file",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert accepted.status_code == 200
        saved_name = accepted.json()["file_path"]
        assert (isolated_runtime["temp"] / saved_name).read_bytes() == b"hello"

        media_rejected = client.post(
            "/api/upload_media",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert media_rejected.status_code == 400

        media = client.post(
            "/api/upload_media",
            files={"file": ("frame.png", SampleMedia.png_bytes(), "image/png")},
        )
        assert media.status_code == 200
        assert media.json()["filename"] == "frame.png"
        nested = isolated_runtime["temp"] / "nested"
        nested.mkdir()
        (nested / "junk.bin").write_bytes(b"1234")

        cleared = client.delete("/api/cache/temp")
        assert cleared.status_code == 200
        body = cleared.json()
        assert body["status"] == "ok"
        assert body["deleted"] >= 1
        assert body["freed_bytes"] >= 4
        assert body["errors"] == []
        assert list(isolated_runtime["temp"].iterdir()) == []


class TestConfigRoutes:
    def test_get_config_shape(self, client):
        response = client.get("/api/config")
        assert response.status_code == 200
        body = response.json()
        assert "models" in body["config"]
        assert "server" in body["config"]
        assert body["path"].endswith("config.yaml")

    def test_put_config_round_trip_does_not_touch_the_real_file(self, client, isolated_runtime, monkeypatch):
        import api.routers.configuration as configuration
        import config as config_module

        real_path = config_module.CONFIG_PATH
        before = real_path.read_bytes() if real_path.exists() else None
        target = isolated_runtime["root"] / "config.yaml"
        snapshot = ConfigState.snapshot()
        monkeypatch.setattr(config_module, "CONFIG_PATH", target)
        monkeypatch.setattr(configuration, "CONFIG_PATH", target)
        try:
            response = client.put(
                "/api/config",
                json={"values": {"server": {"host": "127.0.0.1", "port": 8123, "log_level": "INFO"}}},
            )
            assert response.status_code == 200
            assert response.json()["config"]["server"]["port"] == 8123
            assert target.is_file()
            assert str(target) == response.json()["path"]
        finally:
            ConfigState.restore(snapshot)
            apply_log_level_setting()
            apply_access_log_setting()

        if before is None:
            assert not real_path.exists()
        else:
            assert real_path.read_bytes() == before


class TestSandboxRoutes:
    def test_history_and_validation(self, client):
        assert client.get("/api/sandbox/history").json()["records"] == []
        assert client.get("/api/sandbox/tasks").json() == {"success": True, "tasks": []}
        missing = client.get("/api/sandbox/history/does-not-exist")
        assert missing.json()["success"] is False
        assert client.delete("/api/sandbox/history/does-not-exist").json()["success"] is False
        assert client.post("/api/sandbox/llm", json={}).status_code == 422

    def test_each_tool_returns_the_mocked_result(self, client, monkeypatch):
        FakeModelClients.llm(monkeypatch, text="文字结果")
        llm = client.post(
            "/api/sandbox/llm",
            json={"model": "deepseek-flash", "prompt": "你好", "web_search": False},
        )
        assert llm.status_code == 200
        assert llm.json()["success"] is True
        assert llm.json()["result"] == "文字结果"
        record_id = llm.json()["record_id"]

        history = client.get("/api/sandbox/history").json()["records"]
        assert history[0]["id"] == record_id
        assert history[0]["tool"] == "llm"
        detail = client.get(f"/api/sandbox/history/{record_id}")
        assert detail.json()["success"] is True
        assert detail.json()["record"]["output"]["response"] == "文字结果"

        FakeModelClients.vlm(monkeypatch, text="看见了")
        vlm = client.post(
            "/api/sandbox/vlm",
            json={"model": "qwen3.7-plus", "prompt": "描述", "images": ["/tmp/a.png"]},
        )
        assert vlm.status_code == 200
        assert vlm.json()["result"] == "看见了"

        FakeModelClients.image(monkeypatch, paths=["result/sandbox/fake.png"])
        t2i = client.post(
            "/api/sandbox/t2i",
            json={"model": "doubao-seedream-5-0-260128", "prompt": "一只猫", "ratio": "1:1"},
        )
        assert t2i.status_code == 200
        assert t2i.json()["success"] is True
        assert t2i.json()["result"] == ["result/sandbox/fake.png"]

        i2i = client.post(
            "/api/sandbox/i2i",
            json={"model": "doubao-seedream-5-0-260128", "prompt": "改成夜景", "image": "result/a.png"},
        )
        assert i2i.status_code == 200
        assert i2i.json()["success"] is True

        FakeModelClients.video(monkeypatch)
        video = client.post(
            "/api/sandbox/video",
            json={"model": "wan2.7-i2v", "prompt": "走路", "image": "result/a.png", "duration": 5},
        )
        assert video.status_code == 200
        assert video.json()["success"] is True
        assert video.json()["video_path"]
        assert client.get("/api/sandbox/tasks").json()["tasks"] == []

        deleted = client.delete(f"/api/sandbox/history/{record_id}")
        assert deleted.json()["success"] is True
        assert client.get(f"/api/sandbox/history/{record_id}").json()["success"] is False

