"""SQLite project store: migrations, projects, graph JSON, and content-addressed blobs."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from storage import (
    AssetNotFoundError,
    AssetStore,
    BlobNotFoundError,
    BlobStore,
    Database,
    GraphStore,
    ImageStore,
    LookNotFoundError,
    LookStore,
    ProjectNotFoundError,
    ProjectStore,
    Schema,
    StorageError,
)
from storage.database import transaction
from tests.support import SampleMedia

PROJECT_COLUMNS = [
    "id",
    "name",
    "created_at",
    "updated_at",
    "budget_cap",
    "spent",
    "status",
    "graph_json",
]

TABLE_COLUMNS = {
    "schema_version": ["version", "name", "applied_at"],
    "project": PROJECT_COLUMNS,
    "episode": [
        "id",
        "project_id",
        "number",
        "title",
        "script_asset_id",
        "final_cut_asset_id",
        "created_at",
    ],
    "asset": [
        "id",
        "project_id",
        "scope",
        "kind",
        "name",
        "description",
        "tags_json",
        "episode_id",
        "created_at",
        "updated_at",
    ],
    "look": [
        "id",
        "asset_id",
        "name",
        "parent_look_id",
        "description",
        "prompt",
        "attributes_json",
        "created_at",
        "updated_at",
    ],
    "look_version": [
        "id",
        "look_id",
        "version",
        "parent_look_id",
        "name",
        "description",
        "prompt",
        "attributes_json",
        "reason",
        "created_at",
    ],
    "blob": ["hash", "mime", "kind", "ext", "size", "refcount", "created_at"],
    "image": [
        "id",
        "asset_id",
        "look_id",
        "blob_hash",
        "source",
        "model",
        "prompt",
        "cost",
        "width",
        "height",
        "created_at",
    ],
    "look_selection": ["look_id", "image_id", "selected_at"],
    "segment_ref": [
        "id",
        "segment_asset_id",
        "asset_id",
        "look_id",
        "role",
        "alias",
        "created_at",
    ],
    "edge": [
        "id",
        "project_id",
        "src_id",
        "dst_id",
        "edge_type",
        "payload_json",
        "created_at",
    ],
    "node_state": [
        "project_id",
        "node_id",
        "status",
        "input_fingerprint",
        "pinned",
        "updated_at",
    ],
    "ledger": [
        "id",
        "project_id",
        "node_id",
        "kind",
        "model",
        "quote_amount",
        "actual_amount",
        "currency",
        "confirmed",
        "status",
        "detail_json",
        "created_at",
    ],
}


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    conn = Database.connect(data)
    try:
        yield conn, data
    finally:
        conn.close()


class TestMigration:
    def test_fresh_database_is_wal_and_versioned(self, store):
        conn, data = store
        assert (data / "videoclaw.db").is_file()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Schema.VERSION
        assert Schema.current(conn) == 1
        assert Schema.applied_versions(conn) == [1]
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert set(Schema.TABLES) <= names
        assert "schema_version" in names
        for table, columns in TABLE_COLUMNS.items():
            assert _columns(conn, table) == columns

    def test_migrate_is_idempotent_on_an_existing_database(self, store):
        conn, data = store
        project = ProjectStore.create(conn, "Keep me", budget_cap=12.5, spent=1)
        conn.close()
        reopened = Database.connect(data)
        try:
            assert Schema.migrate(reopened) == 1
            assert Schema.migrate(reopened) == 1
            assert _count(reopened, "schema_version") == 1
            assert ProjectStore.open(reopened, project["id"])["name"] == "Keep me"
            assert ProjectStore.open(reopened, project["id"])["budget_cap"] == 12.5
        finally:
            reopened.close()

    def test_migrate_upgrades_tables_that_have_no_version_row(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        raw = sqlite3.connect(data / "videoclaw.db")
        try:
            for statement in Schema.MIGRATIONS[0][2]:
                raw.execute(statement)
            raw.execute(
                """
                INSERT INTO project (
                    id, name, created_at, updated_at, budget_cap, spent, status, graph_json
                ) VALUES ('projlegacy', 'Already there', '2026-01-01T00:00:00.000000Z',
                          '2026-01-01T00:00:00.000000Z', NULL, NULL, 'active', NULL)
                """
            )
            raw.commit()
        finally:
            raw.close()

        conn = Database.connect(data)
        try:
            assert Schema.current(conn) == 1
            assert _count(conn, "schema_version") == 1
            opened = ProjectStore.open(conn, "projlegacy")
            assert opened["name"] == "Already there"
            assert opened["budget_cap"] is None
            assert opened["spent"] is None
            assert Schema.migrate(conn) == 1
            assert _count(conn, "schema_version") == 1
            assert ProjectStore.open(conn, "projlegacy")["name"] == "Already there"
        finally:
            conn.close()

    def test_migrate_empty_database_file(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "videoclaw.db").write_bytes(b"")
        conn = Database.connect(data)
        try:
            assert Schema.current(conn) == 1
            assert ProjectStore.list(conn) == []
            assert _count(conn, "project") == 0
        finally:
            conn.close()


class TestProjects:
    def test_create_open_list_delete(self, store):
        conn, data = store
        first = ProjectStore.create(conn, " 第一集 ", status="active")
        second = ProjectStore.create(
            conn,
            "Sequel",
            budget_cap=None,
            spent=None,
            status="paused",
        )
        assert first["name"] == "第一集"
        assert first["budget_cap"] is None
        assert first["spent"] is None
        assert first["status"] == "active"
        assert "graph_json" not in first
        assert ProjectStore.open(conn, first["id"]) == first

        listed = ProjectStore.list(conn)
        assert {item["id"] for item in listed} == {first["id"], second["id"]}
        assert listed[0]["created_at"] >= listed[1]["created_at"]
        assert all("graph_json" not in item for item in listed)

        updated = ProjectStore.update(
            conn,
            first["id"],
            budget_cap=40,
            spent=0,
            status="completed",
        )
        assert updated["budget_cap"] == 40
        assert updated["spent"] == 0.0
        assert updated["status"] == "completed"
        cleared = ProjectStore.update(conn, first["id"], budget_cap=None, spent=None)
        assert cleared["budget_cap"] is None
        assert cleared["spent"] is None

        assert ProjectStore.delete(conn, first["id"]) is True
        assert ProjectStore.delete(conn, first["id"]) is False
        with pytest.raises(ProjectNotFoundError):
            ProjectStore.open(conn, first["id"])
        assert [item["id"] for item in ProjectStore.list(conn)] == [second["id"]]
        assert (data / "videoclaw.db").is_file()

    def test_rejects_empty_name_and_bad_amounts(self, store):
        conn, _data = store
        with pytest.raises(StorageError):
            ProjectStore.create(conn, "   ")
        with pytest.raises(StorageError):
            ProjectStore.create(conn, "Ok", budget_cap=-1)
        with pytest.raises(StorageError):
            ProjectStore.create(conn, "Ok", spent=True)
        project = ProjectStore.create(conn, "Ok")
        with pytest.raises(StorageError):
            ProjectStore.update(conn, project["id"])

    def test_leaves_legacy_directories_in_place(self, store):
        conn, data = store
        sessions = data / "sessions"
        sessions.mkdir()
        sentinel = sessions / "old.json"
        sentinel.write_text('{"id": "legacy"}', encoding="utf-8")
        results = data.parent / "result" / "image" / "legacy"
        results.mkdir(parents=True)
        marker = results / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        project = ProjectStore.create(conn, "Fresh")
        payload = b"not-a-session"
        BlobStore.put(conn, payload, mime="image/png", kind="image")
        assert ProjectStore.delete(conn, project["id"]) is True

        assert sentinel.read_text(encoding="utf-8") == '{"id": "legacy"}'
        assert marker.read_text(encoding="utf-8") == "keep"
        assert sessions.is_dir()
        assert data.is_dir()


class TestGraph:
    def test_round_trip_graph_json(self, store):
        conn, _data = store
        project = ProjectStore.create(conn, "Graph")
        graph = {
            "nodes": [
                {"id": "ep1", "status": "done", "label": "第一集"},
                {"id": "ep2", "status": "pending", "label": "第二集"},
            ],
            "edges": [
                {"src": "ep1", "dst": "ep2", "edge_type": "continuity", "note": None},
            ],
            "meta": {"depth": 2, "ok": True, "ratio": 1.5, "tags": ["a", "b"]},
        }
        assert GraphStore.load(conn, project["id"]) is None
        assert GraphStore.save(conn, project["id"], graph) == graph
        assert GraphStore.load(conn, project["id"]) == graph

        replacement = {"nodes": [], "edges": [], "replaced": True}
        assert GraphStore.save(conn, project["id"], replacement) == replacement
        assert GraphStore.load(conn, project["id"]) == replacement
        assert "graph_json" not in ProjectStore.open(conn, project["id"])

    def test_save_requires_a_project_and_an_object(self, store):
        conn, _data = store
        with pytest.raises(ProjectNotFoundError):
            GraphStore.save(conn, "missingproject", {"nodes": []})
        project = ProjectStore.create(conn, "Graph")
        with pytest.raises(StorageError):
            GraphStore.save(conn, project["id"], ["not", "an", "object"])
        ProjectStore.delete(conn, project["id"])
        with pytest.raises(ProjectNotFoundError):
            GraphStore.load(conn, project["id"])


class TestBlobs:
    def test_put_get_uses_hash_path(self, store):
        conn, data = store
        payload = SampleMedia.png_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        record = BlobStore.put(conn, payload, mime="image/png", kind="image")
        assert record["hash"] == digest
        assert record["mime"] == "image/png"
        assert record["kind"] == "image"
        assert record["ext"] == "png"
        assert record["size"] == len(payload)
        assert record["refcount"] == 1
        path = Path(record["path"])
        assert path == data / "blobs" / digest[:2] / f"{digest}.png"
        assert path.is_file()
        assert BlobStore.get(conn, digest) == payload
        assert "result" not in path.relative_to(data).parts

    def test_identical_bytes_are_stored_once(self, store):
        conn, data = store
        payload = b"same-bytes"
        other = b"other-bytes"
        first = BlobStore.put(conn, payload, mime="video/mp4", kind="video")
        second = BlobStore.put(conn, bytearray(payload), mime="video/mp4", kind="video")
        third = BlobStore.put(conn, other, mime="video/mp4", kind="video")
        assert first["hash"] == second["hash"]
        assert second["refcount"] == 2
        assert third["hash"] != first["hash"]
        files = [path for path in (data / "blobs").rglob("*") if path.is_file()]
        assert len(files) == 2
        assert BlobStore.get(conn, first["hash"]) == payload
        with pytest.raises(StorageError):
            BlobStore.put(conn, payload, mime="image/png", kind="image")
        assert BlobStore.info(conn, first["hash"])["refcount"] == 2

    def test_release_deletes_the_file_only_at_zero(self, store):
        conn, _data = store
        payload = b"counted"
        record = BlobStore.put(conn, payload, mime="application/octet-stream", kind="other")
        BlobStore.put(conn, payload, mime="application/octet-stream", kind="other")
        assert BlobStore.release(conn, record["hash"]) is False
        assert Path(record["path"]).is_file()
        assert BlobStore.info(conn, record["hash"])["refcount"] == 1
        assert BlobStore.release(conn, record["hash"]) is True
        assert not Path(record["path"]).exists()
        with pytest.raises(BlobNotFoundError):
            BlobStore.get(conn, record["hash"])

    def test_release_keeps_a_file_an_image_still_uses(self, store):
        conn, _data = store
        payload = SampleMedia.png_bytes()
        project = ProjectStore.create(conn, "Pics")
        blob = BlobStore.put(conn, payload, mime="image/png", kind="image")
        asset = AssetStore.create(
            conn,
            project_id=project["id"],
            name="Ava",
            kind="character",
        )
        look = LookStore.create(conn, asset_id=asset["id"], name="day")
        image = ImageStore.create(
            conn,
            asset_id=asset["id"],
            look_id=look["id"],
            blob_hash=blob["hash"],
            source="uploaded",
        )
        assert image["blob_hash"] == blob["hash"]
        assert BlobStore.info(conn, blob["hash"])["refcount"] == 2
        assert BlobStore.release(conn, blob["hash"]) is False
        assert BlobStore.info(conn, blob["hash"])["refcount"] == 1
        with pytest.raises(StorageError):
            BlobStore.release(conn, blob["hash"])
        assert Path(blob["path"]).is_file()
        assert BlobStore.get(conn, blob["hash"]) == payload
        assert ImageStore.delete(conn, image["id"]) is True
        assert not Path(blob["path"]).exists()

    def test_rolled_back_release_leaves_the_file(self, store):
        conn, _data = store
        payload = b"rollback"
        record = BlobStore.put(conn, payload, mime="text/plain", kind="text")
        with pytest.raises(RuntimeError):
            with transaction(conn):
                assert BlobStore.release(conn, record["hash"]) is True
                raise RuntimeError("boom")
        assert Path(record["path"]).is_file()
        assert BlobStore.get(conn, record["hash"]) == payload
        assert BlobStore.info(conn, record["hash"])["refcount"] == 1

    def test_delete_project_releases_only_its_image_refs(self, store):
        conn, _data = store
        payload = b"shared-picture"
        blob = BlobStore.put(conn, payload, mime="image/png", kind="image")
        first = ProjectStore.create(conn, "One")
        second = ProjectStore.create(conn, "Two")
        for project in (first, second):
            asset = AssetStore.create(
                conn,
                project_id=project["id"],
                name="Hero",
                kind="character",
            )
            ImageStore.create(
                conn,
                asset_id=asset["id"],
                blob_hash=blob["hash"],
                source="generated",
                model="seedream",
                prompt="portrait",
                cost=0.04,
            )
        assert BlobStore.info(conn, blob["hash"])["refcount"] == 3
        assert ProjectStore.delete(conn, first["id"]) is True
        assert BlobStore.info(conn, blob["hash"])["refcount"] == 2
        assert BlobStore.get(conn, blob["hash"]) == payload
        assert ProjectStore.delete(conn, second["id"]) is True
        assert BlobStore.info(conn, blob["hash"])["refcount"] == 1
        assert BlobStore.release(conn, blob["hash"]) is True
        assert not Path(blob["path"]).exists()


class TestAssetChain:
    def test_asset_look_image_and_versions(self, store):
        conn, _data = store
        project = ProjectStore.create(conn, "Library")
        global_asset = AssetStore.create(conn, name="Shared hero", kind="character", scope="global")
        asset = AssetStore.create(
            conn,
            project_id=project["id"],
            name="Ava",
            kind="character",
            description="lead",
            tags=["lead", "红"],
        )
        base = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="base",
            description="coat",
            prompt="red coat",
            attributes={"palette": "red"},
        )
        child = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="night",
            parent_look_id=base["id"],
        )
        assert base["version"] == 1
        assert child["parent_look_id"] == base["id"]
        edited = LookStore.update(conn, base["id"], prompt="red coat, night")
        assert edited["version"] == 2
        assert edited["prompt"] == "red coat, night"
        versions = LookStore.list_versions(conn, base["id"])
        assert [item["version"] for item in versions] == [2, 1]
        assert versions[0]["reason"] == "edit"
        assert versions[1]["reason"] == "create"

        blob = BlobStore.put(conn, SampleMedia.png_bytes(), mime="image/png", kind="image")
        image = ImageStore.create(
            conn,
            asset_id=asset["id"],
            look_id=base["id"],
            blob_hash=blob["hash"],
            source="generated",
            model="seedream",
            prompt="portrait",
            cost=0.5,
            width=1,
            height=1,
        )
        selection = LookStore.select_image(conn, base["id"], image["id"])
        assert selection["look_id"] == base["id"]
        assert selection["image_id"] == image["id"]
        assert ImageStore.list(conn, look_id=base["id"])[0]["id"] == image["id"]
        assert AssetStore.list(conn, project_id=project["id"])[0]["id"] == asset["id"]
        assert AssetStore.get(conn, asset["id"])["tags"] == ["lead", "红"]

        assert ProjectStore.delete(conn, project["id"]) is True
        with pytest.raises(AssetNotFoundError):
            AssetStore.get(conn, asset["id"])
        assert AssetStore.get(conn, global_asset["id"])["scope"] == "global"
        assert BlobStore.info(conn, blob["hash"])["refcount"] == 1

    def test_delete_parent_look_clears_child_parent(self, store):
        conn, _data = store
        project = ProjectStore.create(conn, "Looks")
        asset = AssetStore.create(conn, project_id=project["id"], name="Set", kind="setting")
        parent = LookStore.create(conn, asset_id=asset["id"], name="day")
        child = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="dusk",
            parent_look_id=parent["id"],
        )
        assert LookStore.delete(conn, parent["id"]) is True
        assert LookStore.get(conn, child["id"])["parent_look_id"] is None
        with pytest.raises(LookNotFoundError):
            LookStore.get(conn, parent["id"])

    def test_schema_accepts_documented_check_values(self, store):
        conn, _data = store
        project = ProjectStore.create(conn, "Checks", project_id="projchecks")
        for index, kind in enumerate(Schema.ASSET_KINDS):
            AssetStore.create(
                conn,
                project_id=project["id"],
                name=kind,
                kind=kind,
                asset_id=f"asset{index}",
            )
        AssetStore.create(conn, name="global-script", kind="script", scope="global", asset_id="assetglobal")
        now = "2026-01-02T00:00:00.000000Z"
        conn.execute(
            """
            INSERT INTO episode (id, project_id, number, title, script_asset_id, created_at)
            VALUES ('ep1', 'projchecks', 1, 'Pilot', 'asset0', ?)
            """,
            (now,),
        )
        for source in Schema.IMAGE_SOURCES:
            ImageStore.create(
                conn,
                asset_id="asset0",
                source=source,
                image_id=f"img{source.replace('_', '')}",
            )
        for edge_type in Schema.EDGE_TYPES:
            conn.execute(
                """
                INSERT INTO edge (id, project_id, src_id, dst_id, edge_type, created_at)
                VALUES (?, 'projchecks', 'asset0', 'asset1', ?, ?)
                """,
                (f"edge{edge_type}", edge_type, now),
            )
        for status in Schema.NODE_STATUSES:
            conn.execute(
                """
                INSERT INTO node_state (project_id, node_id, status, updated_at)
                VALUES ('projchecks', ?, ?, ?)
                """,
                (f"node-{status}", status, now),
            )
        segment_id = conn.execute(
            """
            SELECT id FROM asset
            WHERE project_id = 'projchecks' AND kind = 'segment'
            """
        ).fetchone()[0]
        character_id = conn.execute(
            """
            SELECT id FROM asset
            WHERE project_id = 'projchecks' AND kind = 'character'
            """
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO segment_ref (
                id, segment_asset_id, asset_id, look_id, role, alias, created_at
            ) VALUES ('ref1', ?, ?, NULL, 'character', '@Ava/day', ?)
            """,
            (segment_id, character_id, now),
        )
        conn.execute(
            """
            INSERT INTO ledger (
                id, project_id, node_id, kind, quote_amount, actual_amount, currency,
                confirmed, status, created_at
            ) VALUES ('led1', 'projchecks', 'node-done', 'video', 1.5, NULL, 'CNY', 0,
                      'quoted', ?)
            """,
            (now,),
        )
        assert _count(conn, "edge") == len(Schema.EDGE_TYPES)
        assert _count(conn, "node_state") == len(Schema.NODE_STATUSES)
        assert _count(conn, "segment_ref") == 1
        assert _count(conn, "ledger") == 1
        assert ProjectStore.delete(conn, project["id"]) is True
        assert _count(conn, "episode") == 0
        assert _count(conn, "edge") == 0
        assert _count(conn, "node_state") == 0
        assert _count(conn, "ledger") == 0
        assert _count(conn, "segment_ref") == 0
        assert AssetStore.get(conn, "assetglobal")["kind"] == "script"


class TestPackageBoundary:
    def test_storage_modules_do_not_import_legacy_packages(self):
        root = Path(__file__).resolve().parents[1] / "storage"
        for path in root.glob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                assert "pipelines" not in stripped
                assert "orchestrator" not in stripped
                assert "config_model" not in stripped
                assert "workflow" not in stripped
                assert stripped not in {"import session", "from session import SessionManager"}

    def test_default_data_dir_points_at_backend_code_data(self):
        path = Database.default_data_dir()
        assert path.parts[-3:] == ("backend", "code", "data")


class TestBlobPath:
    def test_path_for_matches_the_shard_convention(self, tmp_path):
        digest = "ab" + ("cd" * 31)
        assert len(digest) == 64
        path = BlobStore.path_for(tmp_path, digest, "png")
        assert path == tmp_path / "blobs" / "ab" / f"{digest}.png"
        with pytest.raises(StorageError):
            BlobStore.path_for(tmp_path, "abc", "png")
        with pytest.raises(StorageError):
            BlobStore.path_for(tmp_path, digest, "../png")
