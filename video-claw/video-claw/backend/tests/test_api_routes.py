"""Request and response contracts for every API router.

Model calls are replaced. Pipeline tasks are not executed.
"""

import json

from api.logging_config import apply_access_log_setting, apply_log_level_setting
from core.orchestrator import WorkflowStage
from tests.support import ConfigState, FakeModelClients, ProjectPayloads, SampleMedia


class TestHealthAndStages:
    def test_root_and_health(self, client):
        root = client.get("/")
        assert root.status_code == 200
        assert root.json()["service"] == "Video-Claw"
        assert root.json()["health"] == "/api/health"

        health = client.get("/api/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        assert isinstance(body["timestamp"], float)

    def test_stage_catalog(self, client):
        response = client.get("/api/stages")
        assert response.status_code == 200
        stages = response.json()["stages"]
        assert [stage["id"] for stage in stages] == [
            "script_generation",
            "character_design",
            "storyboard",
            "reference_generation",
            "video_generation",
            "post_production",
        ]
        assert [stage["order"] for stage in stages] == [1, 2, 3, 4, 5, 6]


class TestWorkflowRoutes:
    def test_start_requires_models_and_persists_a_session(self, client, isolated_runtime):
        missing = ProjectPayloads.start_body()
        missing.pop("llm_model")
        rejected = client.post("/api/project/start", json=missing)
        assert rejected.status_code == 400
        assert "llm_model" in rejected.json()["detail"]

        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        assert created.status_code == 200
        body = created.json()
        session_id = body["session_id"]
        assert session_id.isdigit()
        assert body["params"]["llm_model"] == "deepseek-chat"
        assert body["params"]["episodes"] == 1
        assert (isolated_runtime["session_dir"] / f"{session_id}.json").is_file()

        listed = client.get("/api/sessions")
        assert listed.status_code == 200
        assert session_id in {item["id"] for item in listed.json()["sessions"]}

    def test_start_merges_an_uploaded_text_file_into_the_idea(self, client):
        uploaded = client.post(
            "/api/upload_file",
            files={"file": ("note.txt", "文件里的句子".encode("utf-8"), "text/plain")},
        )
        assert uploaded.status_code == 200
        created = client.post(
            "/api/project/start",
            json=ProjectPayloads.start_body(idea="开头", file_path=uploaded.json()["file_path"]),
        )
        assert created.status_code == 200
        assert "文件里的句子" in created.json()["params"]["idea"]

    def test_status_artifact_models_and_stop(self, client, isolated_runtime):
        missing = client.get("/api/project/missing/status")
        assert missing.status_code == 404
        assert client.get("/api/project/missing/status/from_disk").status_code == 404
        assert client.get("/api/project/missing/artifact/storyboard").status_code == 404

        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        session_id = created.json()["session_id"]

        status = client.get(f"/api/project/{session_id}/status")
        assert status.status_code == 200
        assert status.json()["session_id"] == session_id
        disk = client.get(f"/api/project/{session_id}/status/from_disk")
        assert disk.status_code == 200
        assert disk.json()["session_id"] == session_id

        updated = client.patch(
            f"/api/project/{session_id}/models",
            json={"style": "anime", "video_ratio": "16:9", "not_a_field": "nope"},
        )
        assert updated.status_code == 200
        assert updated.json()["meta"]["style"] == "anime"
        assert updated.json()["meta"]["video_ratio"] == "16:9"
        assert "not_a_field" not in updated.json()["meta"]

        patched = client.patch(
            f"/api/project/{session_id}/artifact/storyboard",
            json={"episodes": [{"episode_number": 1, "segments": []}]},
        )
        assert patched.status_code == 200
        artifact = client.get(f"/api/project/{session_id}/artifact/storyboard")
        assert artifact.status_code == 200
        assert artifact.json()["artifact"]["episodes"][0]["episode_number"] == 1

        stopped = client.post(f"/api/project/{session_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json() == {"status": "stopped", "session_id": session_id}

        unknown_model_patch = client.patch("/api/project/missing/models", json={"style": "anime"})
        assert unknown_model_patch.status_code == 404

    def test_execute_and_intervene_do_not_call_a_model(self, client, isolated_runtime):
        bare = isolated_runtime["engine"].get_or_create_state("bare")
        bare.session_id = "bare"
        rejected = client.post("/api/project/bare/execute/script_generation", json={})
        assert rejected.status_code == 400

        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        session_id = created.json()["session_id"]
        executed = client.post(f"/api/project/{session_id}/execute/script_generation", json={})
        assert executed.status_code == 200
        assert "text/event-stream" in executed.headers["content-type"]
        assert json.loads(executed.text) == {"type": "done", "ok": True}

        missing = client.post(
            "/api/project/missing/intervene",
            json={"stage": "script_generation", "modifications": {}},
        )
        assert missing.status_code == 404

        intervened = client.post(
            f"/api/project/{session_id}/intervene",
            json={"stage": "storyboard", "modifications": {"note": "keep"}},
        )
        assert intervened.status_code == 200
        assert "text/event-stream" in intervened.headers["content-type"]
        assert '"ok":true' in intervened.text.replace(" ", "")

    def test_scene_assets_and_image_upload(self, client, isolated_runtime):
        assert client.get("/api/project/missing/scene/1/assets").status_code == 404

        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        session_id = created.json()["session_id"]
        image = isolated_runtime["root"] / "ref.png"
        extra = isolated_runtime["root"] / "ref-v2.png"
        video = isolated_runtime["root"] / "clip.mp4"
        image.write_bytes(b"png")
        extra.write_bytes(b"png")
        video.write_bytes(b"mp4")
        state = isolated_runtime["engine"].sessions[session_id]
        state.artifacts["storyboard"] = {
            "shots": [{"scene_number": 1, "shot_id": "shot_a"}],
            "episodes": [{
                "segments": [
                    {"scene_number": 1, "segment_id": "seg_a"},
                    {"segment_number": 2, "segment_id": "seg_other"},
                ]
            }],
        }
        state.artifacts["reference_generation"] = {
            "scenes": [{"id": "shot_a", "selected": str(image), "versions": [str(image), str(extra)]}]
        }
        state.artifacts["video_generation"] = {
            "clips": [{"id": "seg_a", "selected": str(video), "versions": [str(video), str(extra)]}]
        }

        counted = client.get(f"/api/project/{session_id}/scene/1/assets")
        assert counted.status_code == 200
        assert counted.json() == {
            "scene_number": 1,
            "reference_images": 2,
            "videos": 1,
            "shot_count": 2,
        }

        uploaded = client.post(
            f"/api/project/{session_id}/artifact/character_design/upload_image",
            data={"item_type": "characters", "item_id": "hero"},
            files={"file": ("hero.png", SampleMedia.png_bytes(), "image/png")},
        )
        assert uploaded.status_code == 200
        body = uploaded.json()
        assert body["status"] == "ok"
        assert body["item_id"] == "hero"
        assert body["artifact"]["characters"][0]["status"] == "done"

        rejected = client.post(
            f"/api/project/{session_id}/artifact/character_design/upload_image",
            data={"item_type": "characters", "item_id": "hero"},
            files={"file": ("notes.txt", b"nope", "text/plain")},
        )
        assert rejected.status_code == 400

    def test_continue_advances_a_finished_script_stage(self, client, isolated_runtime):
        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        session_id = created.json()["session_id"]
        state = isolated_runtime["engine"].sessions[session_id]
        state.current_stage = WorkflowStage.SCRIPT_GENERATION
        state.artifacts["script_generation"] = {"title": "测试"}

        assert client.post("/api/project/missing/continue").status_code == 404
        continued = client.post(f"/api/project/{session_id}/continue")
        assert continued.status_code == 200
        assert continued.json()["status"] == "ready"
        assert continued.json()["next_stage"] == "character_design"

    def test_continue_from_a_new_project_advances_to_script_generation(self, client):
        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        session_id = created.json()["session_id"]
        continued = client.post(f"/api/project/{session_id}/continue")
        assert continued.status_code == 200
        body = continued.json()
        assert body["status"] == "ready"
        assert body["next_stage"] == "script_generation"


class TestSessionRoutes:
    def test_delete_session_and_orphan_route(self, client, isolated_runtime):
        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        session_id = created.json()["session_id"]
        result = isolated_runtime["result"]
        (result / "script" / f"{session_id}.json").write_text("{}", encoding="utf-8")
        (result / "script" / "orphan.json").write_text("{}", encoding="utf-8")
        (result / "image" / "orphan").mkdir()
        (result / "video" / session_id).mkdir()

        cleaned = client.delete("/api/sessions")
        assert cleaned.status_code == 200
        assert cleaned.json()["status"] == "cleaned"
        assert cleaned.json()["cleaned"]["scripts"] == ["orphan"]
        assert not (result / "script" / "orphan.json").exists()
        assert (result / "script" / f"{session_id}.json").exists()
        assert (result / "video" / session_id).is_dir()

        deleted = client.delete(f"/api/sessions/{session_id}")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"
        assert client.delete(f"/api/sessions/{session_id}").status_code == 404
        assert not (isolated_runtime["session_dir"] / f"{session_id}.json").exists()

    def test_delete_active_session_reports_a_conflict(self, client, isolated_runtime):
        created = client.post("/api/project/start", json=ProjectPayloads.start_body())
        session_id = created.json()["session_id"]
        isolated_runtime["engine"]._active_sessions.add(session_id)
        response = client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 409
        assert "active" in response.json()["detail"].lower()
        assert (isolated_runtime["session_dir"] / f"{session_id}.json").is_file()


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
            json={"model": "deepseek-chat", "prompt": "你好", "web_search": False},
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
            json={"model": "qwen3.5-plus", "prompt": "描述", "images": ["/tmp/a.png"]},
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


class TestPipelineRoutes:
    def test_catalogs_templates_and_task_lifecycle(self, client):
        pipelines = client.get("/api/pipelines")
        assert pipelines.status_code == 200
        assert [item["id"] for item in pipelines.json()["pipelines"]] == [
            "standard",
            "action_transfer",
            "digital_human",
        ]

        workflows = client.get("/api/pipelines/api-workflows", params={"media_type": "video"})
        assert workflows.status_code == 200
        assert isinstance(workflows.json()["workflows"], list)
        assert client.get("/api/pipelines/api-workflows", params={"media_type": "audio"}).status_code == 422

        models = client.get("/api/models", params={"model_type": "llm"})
        assert models.status_code == 200
        ids = {item["id"] for item in models.json()["models"]}
        assert "deepseek-chat" in ids
        sample = next(item for item in models.json()["models"] if item["id"] == "deepseek-chat")
        assert sample["provider"] == "deepseek"
        assert "text_generation" in sample["ability_types"]

        media = client.get("/api/models", params={"media_type": "image"})
        assert media.status_code == 200
        assert media.json()["models"]
        assert client.get("/api/models", params={"model_type": "nope"}).status_code == 422

        templates = client.get("/api/pipelines/standard/templates")
        assert templates.status_code == 200
        listed = templates.json()["templates"]
        assert listed
        first = listed[0]
        preview = client.get(first["preview_url"])
        assert preview.status_code == 200
        assert "text/html" in preview.headers["content-type"]
        assert "<" in preview.text
        assert client.get("/api/pipelines/standard/templates/no-size/missing.html/preview").status_code == 404

        assert client.post("/api/pipelines/standard/tasks", json={}).status_code == 422
        assert client.post("/api/pipelines/does-not-exist/tasks", json={"params": {}}).status_code == 404

        standard = client.post(
            "/api/pipelines/standard/tasks",
            json={"text": "一段旁白", "llm_model": "deepseek-chat", "image_model": "doubao-seedream-5-0-260128"},
        )
        assert standard.status_code == 200
        task_id = standard.json()["task_id"]
        assert standard.json()["pipeline"] == "standard"
        assert standard.json()["status"] == "pending"

        action = client.post(
            "/api/pipelines/action_transfer/tasks",
            json={
                "prompt_text": "模仿动作",
                "image_path": "/tmp/a.png",
                "video_path": "/tmp/a.mp4",
                "video_model": "wan2.7-i2v",
            },
        )
        assert action.status_code == 200
        assert action.json()["pipeline"] == "action_transfer"

        digital = client.post(
            "/api/pipelines/digital_human/tasks",
            json={
                "character_image_path": "/tmp/person.png",
                "llm_model": "deepseek-chat",
                "image_model": "doubao-seedream-5-0-260128",
                "video_model": "wan2.7-i2v",
            },
        )
        assert digital.status_code == 200
        assert digital.json()["pipeline"] == "digital_human"

        generic = client.post(
            "/api/pipelines/quick_create/tasks",
            json={"params": {"text": "别名"}},
        )
        assert generic.status_code == 200
        assert generic.json()["pipeline"] == "standard"

        fetched = client.get(f"/api/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["task_id"] == task_id
        listed_tasks = client.get("/api/tasks").json()["tasks"]
        assert task_id in {item["task_id"] for item in listed_tasks}

        with client.stream("GET", f"/api/tasks/{task_id}/events") as stream:
            assert stream.status_code == 200
            assert "text/event-stream" in stream.headers["content-type"]
            payload = ""
            for chunk in stream.iter_text():
                payload += chunk
                if "snapshot" in payload:
                    break
        assert "snapshot" in payload
        assert task_id in payload

        assert client.get("/api/tasks/missing-task").status_code == 404
        assert client.get("/api/tasks/missing-task/events").status_code == 404
        removed = client.delete(f"/api/tasks/{task_id}")
        assert removed.status_code == 200
        assert removed.json() == {"success": True}
        assert client.delete(f"/api/tasks/{task_id}").status_code == 404
        assert client.get(f"/api/tasks/{task_id}").status_code == 404
