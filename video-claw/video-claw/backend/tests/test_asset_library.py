# -*- coding: utf-8 -*-
"""Global asset library: CRUD, import stamp, stage-2 skip, regenerate resolution, cleanup."""

import asyncio
import importlib.util
import json
import os

import pytest
from fastapi import HTTPException

from api.schemas.project import AssetImportRequest, AssetUpdateRequest
from assets.library import AssetLibrary
from config import settings
from core.agents.character_agent import CharacterDesignerAgent
from core.agents.reference_agent import ReferenceGeneratorAgent
from core.agents.video_agent import VideoDirectorAgent
from core.orchestrator import WorkflowEngine

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSETS_ROUTE = os.path.join(_BACKEND_DIR, "api", "routers", "assets.py")
_spec = importlib.util.spec_from_file_location("asset_routes_under_test", _ASSETS_ROUTE)
assets_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assets_api)
list_assets = assets_api.list_assets
import_asset = assets_api.import_asset
update_asset = assets_api.update_asset
delete_asset = assets_api.delete_asset


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = tmp_path / "code"
    (code / "data").mkdir(parents=True)
    (code / "result").mkdir(parents=True)
    monkeypatch.setattr(settings, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "CODE_DIR", str(code))
    monkeypatch.setattr(settings, "RESULT_DIR", str(code / "result"))
    monkeypatch.setattr(settings, "SESSION_DIR", str(code / "data" / "sessions"))
    return tmp_path


@pytest.fixture
def engine(isolated_dirs):
    workflow = WorkflowEngine()
    workflow.sessions.clear()
    workflow._stop_events.clear()
    workflow._active_sessions.clear()
    workflow._session_dir = str(isolated_dirs / "sessions")
    os.makedirs(workflow._session_dir, exist_ok=True)
    return workflow


def _write(path, payload=b"img"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def _library_character(name="小明", payload=b"portrait"):
    source = _write("incoming/portrait.png", payload)
    return AssetLibrary.create_from_file(
        name=name,
        description="少年",
        asset_type="characters",
        source_path=source,
    )


def test_library_crud_and_ids_do_not_prefix_match(isolated_dirs):
    first = _library_character("小明", b"one")
    second_source = _write("incoming/room.png", b"room")
    second = AssetLibrary.create_from_file(
        name="教室",
        description="白天",
        asset_type="settings",
        source_path=second_source,
    )

    assert set(first) >= {"id", "name", "description", "selected", "versions"}
    assert first["type"] == "characters"
    assert second["type"] == "settings"
    assert len(first["id"]) == len(second["id"]) == 35
    assert not first["id"].startswith(second["id"])
    assert not second["id"].startswith(first["id"])
    assert os.path.isfile(os.path.join(settings.BASE_DIR, first["selected"]))
    assert "code/data/assets/" in first["selected"].replace("\\", "/")

    listed = AssetLibrary.list_assets()
    assert {item["id"] for item in listed} == {first["id"], second["id"]}

    newer = _write("incoming/portrait-v2.png", b"two")
    updated = AssetLibrary.update(first["id"], name="小明改", source_path=newer)
    assert updated["name"] == "小明改"
    assert updated["selected"] != first["selected"]
    assert first["selected"] in updated["versions"]
    assert AssetLibrary.file_exists(updated["selected"])
    assert AssetLibrary.bound_path({"library_id": first["id"]}) == updated["selected"]

    assert AssetLibrary.delete(first["id"]) is True
    assert AssetLibrary.get(first["id"]) is None
    assert AssetLibrary.delete(first["id"]) is False


def test_api_lists_patches_and_deletes(isolated_dirs):
    record = _library_character()
    listed = asyncio.run(list_assets())
    assert listed["assets"][0]["id"] == record["id"]

    renamed = asyncio.run(update_asset(record["id"], AssetUpdateRequest(name="新名字", description="新描述")))
    assert renamed["asset"]["name"] == "新名字"
    assert renamed["asset"]["description"] == "新描述"
    assert renamed["asset"]["selected"] == record["selected"]

    removed = asyncio.run(delete_asset(record["id"]))
    assert removed == {"status": "deleted", "id": record["id"]}
    assert AssetLibrary.get(record["id"]) is None

    with pytest.raises(HTTPException) as missing:
        asyncio.run(delete_asset(record["id"]))
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as invalid:
        asyncio.run(update_asset("../etc/passwd", AssetUpdateRequest(name="x")))
    assert invalid.value.status_code == 400


def test_import_stamps_library_id(isolated_dirs, engine, monkeypatch):
    monkeypatch.setattr(assets_api, "workflow_engine", engine)
    selected = "code/result/image/s1/Assets/characters/char_1.png"
    _write(selected, b"session-portrait")
    engine.create_session("s1", {"idea": "故事"})
    state = engine.sessions["s1"]
    state.artifacts["character_design"] = {
        "characters": [{
            "id": "char_1",
            "name": "小明",
            "description": "少年",
            "selected": selected,
            "versions": [selected],
        }, {
            "id": "char_empty",
            "name": "空",
            "description": "",
            "selected": "",
            "versions": [],
        }],
        "settings": [],
    }
    engine.save_session_to_disk("s1")

    result = asyncio.run(import_asset(AssetImportRequest(
        session_id="s1",
        item_type="characters",
        item_id="char_1",
    )))
    library_id = result["library_id"]
    assert result["artifact"]["characters"][0]["library_id"] == library_id
    assert state.artifacts["character_design"]["characters"][0]["library_id"] == library_id

    on_disk = json.loads((isolated_dirs / "sessions" / "s1.json").read_text(encoding="utf-8"))
    assert on_disk["artifacts"]["character_design"]["characters"][0]["library_id"] == library_id
    assert AssetLibrary.file_exists(AssetLibrary.get(library_id)["selected"])

    again = asyncio.run(import_asset(AssetImportRequest(
        session_id="s1",
        item_type="characters",
        item_id="char_1",
    )))
    assert again["library_id"] == library_id
    assert len(AssetLibrary.list_assets()) == 1

    with pytest.raises(HTTPException) as empty:
        asyncio.run(import_asset(AssetImportRequest(
            session_id="s1",
            item_type="characters",
            item_id="char_empty",
        )))
    assert empty.value.status_code == 400

    with pytest.raises(HTTPException) as missing:
        asyncio.run(import_asset(AssetImportRequest(
            session_id="missing",
            item_type="characters",
            item_id="char_1",
        )))
    assert missing.value.status_code == 404


def test_stage2_skips_resolvable_library_id_and_generates_when_file_missing(isolated_dirs):
    record = _library_character()
    agent = CharacterDesignerAgent()
    info = {"name": "小明", "description": "少年", "species": "人类", "library_id": record["id"]}
    chars = {"char_9": info}

    assert agent._asset_already_available("no-session", "characters", "char_9", info)
    assert agent._collect_generation_tasks("no-session", chars, {}) == []
    preview = agent._build_preview("no-session", chars, {})
    assert preview["characters"][0]["status"] == "done"
    assert preview["characters"][0]["selected"] == record["selected"]
    payload = agent._build_payload("no-session", chars, {})
    saved = payload["payload"]["characters"][0]
    assert saved["library_id"] == record["id"]
    assert saved["selected"] == record["selected"]
    assert saved["status"] == "done"

    os.remove(os.path.join(settings.BASE_DIR, record["selected"]))
    assert AssetLibrary.get(record["id"]) is not None
    assert not agent._asset_already_available("no-session", "characters", "char_9", info)
    tasks = agent._collect_generation_tasks("no-session", chars, {})
    assert tasks[0][0:2] == ("characters", "char_9")

    local = "code/result/image/sid/Assets/characters/char_1.png"
    _write(local, b"local")
    assert agent._asset_already_available("sid", "characters", "char_1", {"name": "小明"})
    listed = agent._list_versions("sid", "characters", "char_1")
    assert local in [item.replace("\\", "/") for item in listed]
    assert all("data/assets" not in item.replace("\\", "/") for item in listed)

    fresh = _library_character("小明", b"again")
    linked = {"char_9": {"name": "小明", "description": "少年", "species": ""}}
    agent._apply_library_links(linked, {}, {
        "_session_meta": {"library_asset_ids": [fresh["id"]]},
        "_session_artifacts": {},
    })
    assert linked["char_9"]["library_id"] == fresh["id"]
    assert agent._collect_generation_tasks("no-session", linked, {}) == []

    by_name = {"set_1": {"name": "教室", "description": "白天"}}
    agent._apply_library_links({}, by_name, {
        "_session_artifacts": {
            "character_design": {
                "characters": [],
                "settings": [{
                    "id": "other",
                    "name": "教室",
                    "library_id": AssetLibrary.create_from_file(
                        name="教室",
                        description="白天",
                        asset_type="settings",
                        source_path=_write("incoming/room2.png", b"room"),
                    )["id"],
                }],
            },
        },
    })
    assert by_name["set_1"]["library_id"]
    assert agent._asset_already_available("no-session", "settings", "set_1", by_name["set_1"])


def test_regenerate_resolves_current_library_path_without_touching_finished_video(isolated_dirs):
    record = _library_character("小明", b"v1")
    asset = {
        "id": "char_1",
        "name": "小明",
        "description": "少年",
        "selected": record["selected"],
        "versions": [record["selected"]],
        "library_id": record["id"],
    }
    assert VideoDirectorAgent._asset_selected_path(asset) == record["selected"]

    updated = AssetLibrary.update(
        record["id"],
        source_path=_write("incoming/portrait-v2.png", b"v2"),
    )
    assert asset["selected"] == record["selected"]
    assert os.path.isfile(os.path.join(settings.BASE_DIR, record["selected"]))
    assert updated["selected"] != record["selected"]
    assert VideoDirectorAgent._asset_selected_path(asset) == updated["selected"]

    asset_map = ReferenceGeneratorAgent._build_asset_map({
        "characters": [asset],
        "settings": [],
    })
    assert asset_map["characters"]["char_1"] == updated["selected"]

    video_path = "code/result/video/sid/seg1.mp4"
    _write(video_path, b"finished-video")
    before = open(video_path, "rb").read()
    AssetLibrary.update(record["id"], description="只改描述")
    assert open(video_path, "rb").read() == before
    versions = VideoDirectorAgent()._list_versions("sid", "seg1")
    assert versions
    assert all(item.replace("\\", "/").endswith("seg1.mp4") for item in versions)


def test_library_survives_cleanup_orphan_results_and_delete_session(isolated_dirs, engine):
    record = _library_character()
    library_file = os.path.join(settings.BASE_DIR, record["selected"])
    assert os.path.isfile(library_file)

    decoy = os.path.join(settings.RESULT_DIR, "image", "library")
    orphan = os.path.join(settings.RESULT_DIR, "image", "orphan_demo")
    _write(os.path.join(decoy, "gone.png"), b"decoy")
    _write(os.path.join(orphan, "gone.png"), b"orphan")

    engine.create_session("keep-me", {"idea": "故事"})
    report = engine.cleanup_orphan_results()
    assert "library" in report["cleaned"]["images"]
    assert "orphan_demo" in report["cleaned"]["images"]
    assert not os.path.exists(decoy)
    assert not os.path.exists(orphan)
    assert os.path.isfile(library_file)
    assert AssetLibrary.get(record["id"])["id"] == record["id"]

    session_image = os.path.join(settings.RESULT_DIR, "image", "keep-me")
    _write(os.path.join(session_image, "a.png"), b"session")
    assert engine.delete_session("keep-me") is True
    assert not os.path.exists(session_image)
    assert not os.path.exists(os.path.join(engine._session_dir, "keep-me.json"))
    assert os.path.isfile(library_file)
    assert AssetLibrary.get(record["id"])["selected"] == record["selected"]
