"""HTTP contracts for the asset library. No model calls."""

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.routers.library import get_library_connection
from storage import Database


@pytest.fixture
def library(tmp_path, client):
    data = tmp_path / "library"

    def override():
        conn = Database.connect(data)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_library_connection] = override
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_library_connection, None)


def test_client_fixture_is_the_app(client):
    assert isinstance(client, TestClient)


def test_asset_crud_promote_and_tag_search(library):
    created_project = library.post("/api/library/projects", json={"name": "第一部"})
    assert created_project.status_code == 200
    project_id = created_project.json()["id"]

    missing = library.post(
        "/api/library/assets",
        json={"name": "坏类型", "kind": "scene", "project_id": project_id},
    )
    assert missing.status_code == 422

    created = library.post(
        "/api/library/assets",
        json={
            "name": "林夏",
            "kind": "character",
            "project_id": project_id,
            "description": "女主",
            "tags": ["主角", "红"],
        },
    )
    assert created.status_code == 200
    asset_id = created.json()["id"]
    assert created.json()["scope"] == "project"

    listed = library.get("/api/library/assets", params={"project_id": project_id, "kind": "character"})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["assets"]] == [asset_id]

    renamed = library.patch(f"/api/library/assets/{asset_id}", json={"name": "林夏·改"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "林夏·改"

    searched = library.get(
        "/api/library/search",
        params=[("tag", "红"), ("tag", "主角"), ("project_id", project_id)],
    )
    assert searched.status_code == 200
    assert [item["id"] for item in searched.json()["assets"]] == [asset_id]
    assert library.get("/api/library/search", params={"tag": "没有"}).json()["assets"] == []

    promoted = library.post(f"/api/library/assets/{asset_id}/promote")
    assert promoted.status_code == 200
    assert promoted.json()["scope"] == "global"
    assert promoted.json()["project_id"] is None
    again = library.post(f"/api/library/assets/{asset_id}/promote")
    assert again.status_code == 400

    fetched = library.get(f"/api/library/assets/{asset_id}")
    assert fetched.status_code == 200
    assert fetched.json()["tags"] == ["主角", "红"]
    removed = library.delete(f"/api/library/assets/{asset_id}")
    assert removed.status_code == 200
    assert library.get(f"/api/library/assets/{asset_id}").status_code == 404


def test_look_inheritance_cycle_rollback_and_quote(library):
    project_id = library.post("/api/library/projects", json={"name": "继承"}).json()["id"]
    asset_id = library.post(
        "/api/library/assets",
        json={"name": "林夏", "kind": "character", "project_id": project_id},
    ).json()["id"]
    parent = library.post(
        f"/api/library/assets/{asset_id}/looks",
        json={"name": "基", "prompt": "day", "description": "coat", "attributes": {"palette": "red"}},
    )
    assert parent.status_code == 200
    parent_id = parent.json()["id"]
    child = library.post(
        f"/api/library/assets/{asset_id}/looks",
        json={"name": "夜", "parent_look_id": parent_id, "attributes": {"mood": "tense"}},
    )
    assert child.status_code == 200
    child_id = child.json()["id"]
    assert child.json()["prompt"] == "day"
    assert child.json()["covered"]["prompt"] is False
    assert child.json()["attributes"]["mood"] == "tense"
    assert child.json()["attributes"]["palette"] == "red"

    grandchild = library.post(
        f"/api/library/assets/{asset_id}/looks",
        json={"name": "更深", "parent_look_id": child_id},
    ).json()
    cycle = library.patch(
        f"/api/library/looks/{parent_id}",
        json={"parent_look_id": grandchild["id"]},
    )
    assert cycle.status_code == 409
    assert "cycle" in cycle.json()["detail"]

    image = library.post(
        "/api/library/images",
        json={
            "source": "generated",
            "asset_id": asset_id,
            "look_id": parent_id,
            "model": "seedream",
            "prompt": "day portrait",
            "cost": 0.2,
        },
    )
    assert image.status_code == 200
    assert image.json()["source"] == "generated"
    assert image.json()["cost"] == 0.2
    image_id = image.json()["id"]

    segment_id = library.post(
        "/api/library/assets",
        json={"name": "第一场", "kind": "segment", "project_id": project_id},
    ).json()["id"]
    bound = library.post(
        f"/api/library/segments/{segment_id}/refs",
        json={"asset_id": asset_id, "look_id": parent_id, "alias": "@林夏/基"},
    )
    assert bound.status_code == 200

    edited = library.patch(f"/api/library/looks/{parent_id}", json={"prompt": "night"})
    assert edited.status_code == 200
    assert edited.json()["version"] == 2
    assert library.get(f"/api/library/looks/{child_id}").json()["prompt"] == "night"
    assert library.get(f"/api/library/looks/{grandchild['id']}").json()["prompt"] == "night"
    versions = library.get(f"/api/library/looks/{child_id}/versions").json()["versions"]
    assert versions[0]["reason"] == "follow"

    quote = library.post(f"/api/library/looks/{parent_id}/regenerate-quote", json={"unit_cost": 0.2})
    assert quote.status_code == 200
    body = quote.json()
    assert body["requires_confirmation"] is True
    assert body["executed"] is False
    assert body["will_spend"] is False
    assert body["estimated_cost"] == 0.4
    assert body["artifact_count"] == 2
    images_before = library.get("/api/library/images", params={"look_id": parent_id}).json()["images"]
    rejected = library.post(
        f"/api/library/looks/{parent_id}/regenerate-confirm",
        json={"confirm_token": "wrong", "unit_cost": 0.2},
    )
    assert rejected.status_code == 400
    accepted = library.post(
        f"/api/library/looks/{parent_id}/regenerate-confirm",
        json={"confirm_token": body["confirm_token"], "unit_cost": 0.2},
    )
    assert accepted.status_code == 200
    assert accepted.json()["confirmed"] is True
    assert accepted.json()["executed"] is False
    images_after = library.get("/api/library/images", params={"look_id": parent_id}).json()["images"]
    assert [item["id"] for item in images_after] == [item["id"] for item in images_before]
    assert images_after[0]["id"] == image_id

    rolled = library.post(f"/api/library/looks/{parent_id}/rollback", json={"version": 1})
    assert rolled.status_code == 200
    assert rolled.json()["prompt"] == "day"
    assert library.get(f"/api/library/looks/{child_id}").json()["prompt"] == "day"

    resolved = library.post(
        "/api/library/resolve",
        json={"text": "参考 @林夏/夜 出场", "project_id": project_id},
    )
    assert resolved.status_code == 200
    mention = resolved.json()["mentions"][0]
    assert mention["resolved"] is True
    assert mention["matches"][0]["asset_id"] == asset_id
    assert mention["matches"][0]["look_id"] == child_id

    unknown = library.get("/api/library/looks/missing-look")
    assert unknown.status_code == 404
