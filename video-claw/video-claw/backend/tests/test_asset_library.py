"""Asset library: inheritance, versions, segment bindings, stale quotes.

Old session and glob-version tests are left alone. These cover the R5 model only.
"""

import sqlite3

import pytest

from graph.dependency_graph import DependencyGraph
from storage import (
    AssetStore,
    Database,
    GraphStore,
    ImageStore,
    LookCycleError,
    LookStore,
    LookVersionNotFoundError,
    ProjectStore,
    RegenerationQuote,
    Schema,
    SegmentRefStore,
    StorageError,
)
from storage.staleness import image_node_id
from tests.support import SampleMedia


@pytest.fixture
def conn(tmp_path):
    connection = Database.connect(tmp_path / "data")
    try:
        yield connection
    finally:
        connection.close()


def _project(conn, name="片场"):
    return ProjectStore.create(conn, name)


def _asset(conn, project, name, kind="character", **kwargs):
    return AssetStore.create(
        conn,
        project_id=project["id"],
        name=name,
        kind=kind,
        **kwargs,
    )


def _chain(conn, project, depth, prompt="root prompt"):
    asset = _asset(conn, project, "链")
    root = LookStore.create(
        conn,
        asset_id=asset["id"],
        name="root",
        prompt=prompt,
        description="root desc",
        attributes={"palette": "red", "mood": "calm"},
    )
    parent = root
    looks = [root]
    for index in range(1, depth):
        parent = LookStore.create(
            conn,
            asset_id=asset["id"],
            name=f"level-{index}",
            parent_look_id=parent["id"],
        )
        looks.append(parent)
    return asset, looks


class TestKindsAndScopes:
    def test_every_kind_and_both_scopes(self, conn):
        project = _project(conn)
        for kind in Schema.ASSET_KINDS:
            created = _asset(conn, project, kind, kind=kind, tags=["库", kind])
            assert created["kind"] == kind
            assert created["scope"] == "project"
            assert created["project_id"] == project["id"]
        global_asset = AssetStore.create(conn, name="共享剧本", kind="script", scope="global")
        assert global_asset["scope"] == "global"
        assert global_asset["project_id"] is None
        with pytest.raises(StorageError):
            AssetStore.create(conn, name="bad", kind="scene", project_id=project["id"])
        with pytest.raises(StorageError):
            AssetStore.create(
                conn,
                name="both",
                kind="prop",
                scope="global",
                project_id=project["id"],
            )

    def test_promote_detaches_the_asset_from_the_project(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "林夏", tags=["主角"], description="女主", episode_id=None)
        look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="白衬衫")
        promoted = AssetStore.promote(conn, asset["id"])
        assert promoted["scope"] == "global"
        assert promoted["project_id"] is None
        assert promoted["tags"] == ["主角"]
        assert LookStore.get(conn, look["id"])["prompt"] == "白衬衫"
        assert ProjectStore.delete(conn, project["id"]) is True
        assert AssetStore.get(conn, asset["id"])["scope"] == "global"
        with pytest.raises(StorageError):
            AssetStore.promote(conn, asset["id"])

    def test_tag_search_is_exact_and_requires_every_tag(self, conn):
        project = _project(conn)
        other = _project(conn, "另一部")
        hero = _asset(conn, project, "林夏", tags=["主角", "红"])
        _asset(conn, project, "路人", tags=["红"])
        _asset(conn, other, "外人", tags=["主角", "红"])
        global_hero = AssetStore.create(
            conn,
            name="模板主角",
            kind="character",
            scope="global",
            tags=["主角", "红"],
        )
        found = AssetStore.search(conn, tags=["红", "主角"], project_id=project["id"])
        assert {item["id"] for item in found} == {hero["id"], global_hero["id"]}
        project_only = AssetStore.search(
            conn,
            tags=["主角"],
            project_id=project["id"],
            include_global=False,
        )
        assert [item["id"] for item in project_only] == [hero["id"]]
        assert AssetStore.search(conn, tags=["没有"], project_id=project["id"]) == []
        with pytest.raises(StorageError):
            AssetStore.search(conn, tags=[])


class TestInheritance:
    def test_chain_has_no_depth_cap_and_leaf_follows_root(self, conn):
        project = _project(conn)
        _asset_row, looks = _chain(conn, project, 12)
        leaf = looks[-1]
        assert leaf["prompt"] == "root prompt"
        assert leaf["description"] == "root desc"
        assert leaf["attributes"]["palette"] == "red"
        assert leaf["covered"]["prompt"] is False
        assert leaf["covered"]["attributes"] == []
        edited = LookStore.update(
            conn,
            looks[0]["id"],
            prompt="night prompt",
            attributes={"palette": "blue", "mood": "calm"},
        )
        assert edited["version"] == 2
        assert edited["covered"]["attributes"] == ["mood", "palette"]
        refreshed = LookStore.get(conn, leaf["id"])
        assert refreshed["prompt"] == "night prompt"
        assert refreshed["attributes"] == {"palette": "blue", "mood": "calm"}
        assert refreshed["version"] == 2
        follow = LookStore.list_versions(conn, leaf["id"])
        assert follow[0]["reason"] == "follow"
        assert follow[0]["prompt"] == "night prompt"
        assert follow[1]["reason"] == "create"

    def test_covered_fields_stay_when_the_parent_changes(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "林夏")
        parent = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="基",
            prompt="parent",
            description="parent desc",
            attributes={"palette": "red", "mood": "calm"},
        )
        child = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="夜",
            parent_look_id=parent["id"],
            prompt="own prompt",
            attributes={"mood": "tense"},
        )
        assert child["prompt"] == "own prompt"
        assert child["description"] == "parent desc"
        assert child["attributes"] == {"palette": "red", "mood": "tense"}
        assert child["covered"]["prompt"] is True
        assert child["covered"]["description"] is False
        assert child["covered"]["attributes"] == ["mood"]
        LookStore.update(
            conn,
            parent["id"],
            prompt="parent changed",
            description="desc changed",
            attributes={"palette": "blue", "mood": "wild"},
        )
        refreshed = LookStore.get(conn, child["id"])
        assert refreshed["prompt"] == "own prompt"
        assert refreshed["description"] == "desc changed"
        assert refreshed["attributes"]["palette"] == "blue"
        assert refreshed["attributes"]["mood"] == "tense"
        versions = LookStore.list_versions(conn, child["id"])
        assert versions[0]["reason"] == "follow"
        assert versions[0]["description"] == "desc changed"
        assert versions[0]["prompt"] == "own prompt"

    def test_fully_covered_child_does_not_gain_a_follow_version(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "场景")
        parent = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day", description="sun")
        child = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="夜",
            parent_look_id=parent["id"],
            prompt="night",
            description="moon",
        )
        LookStore.update(conn, parent["id"], prompt="noon", description="bright")
        assert LookStore.get(conn, child["id"])["version"] == 1
        assert LookStore.get(conn, child["id"])["prompt"] == "night"

    def test_parent_cycle_is_rejected_and_leaves_the_row(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "环")
        first = LookStore.create(conn, asset_id=asset["id"], name="a", prompt="a")
        second = LookStore.create(
            conn, asset_id=asset["id"], name="b", parent_look_id=first["id"], prompt="b"
        )
        third = LookStore.create(
            conn, asset_id=asset["id"], name="c", parent_look_id=second["id"], prompt="c"
        )
        with pytest.raises(LookCycleError) as caught:
            LookStore.update(conn, first["id"], parent_look_id=third["id"])
        assert first["id"] in caught.value.cycle
        assert third["id"] in caught.value.cycle
        assert LookStore.get(conn, first["id"])["parent_look_id"] is None
        assert LookStore.get(conn, first["id"])["version"] == 1
        with pytest.raises(LookCycleError):
            LookStore.update(conn, second["id"], parent_look_id=second["id"])

    def test_rollback_restores_a_snapshot_and_descendants_follow(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "林夏")
        parent = LookStore.create(conn, asset_id=asset["id"], name="基", prompt="day")
        child = LookStore.create(conn, asset_id=asset["id"], name="夜", parent_look_id=parent["id"])
        owned = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="定妆",
            parent_look_id=parent["id"],
            prompt="fixed",
        )
        LookStore.update(conn, parent["id"], prompt="night")
        assert LookStore.get(conn, child["id"])["prompt"] == "night"
        assert LookStore.get(conn, owned["id"])["prompt"] == "fixed"
        restored = LookStore.rollback(conn, parent["id"], 1)
        assert restored["prompt"] == "day"
        assert restored["version"] == 3
        assert LookStore.list_versions(conn, parent["id"])[0]["reason"] == "rollback"
        assert LookStore.get(conn, child["id"])["prompt"] == "day"
        assert LookStore.list_versions(conn, child["id"])[0]["reason"] == "follow"
        assert LookStore.get(conn, owned["id"])["prompt"] == "fixed"
        assert LookStore.get(conn, owned["id"])["version"] == 1
        forward = LookStore.rollback(conn, parent["id"], 2)
        assert forward["prompt"] == "night"
        assert LookStore.get(conn, child["id"])["prompt"] == "night"
        with pytest.raises(LookVersionNotFoundError):
            LookStore.rollback(conn, parent["id"], 99)

    def test_uncover_lets_a_field_follow_again(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "林夏")
        parent = LookStore.create(conn, asset_id=asset["id"], name="基", prompt="day")
        child = LookStore.create(
            conn,
            asset_id=asset["id"],
            name="夜",
            parent_look_id=parent["id"],
            prompt="own",
        )
        LookStore.update(conn, parent["id"], prompt="night")
        assert LookStore.get(conn, child["id"])["prompt"] == "own"
        released = LookStore.update(conn, child["id"], prompt=None)
        assert released["covered"]["prompt"] is False
        assert released["prompt"] == "night"

    def test_delete_parent_clears_inherited_fields(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "景")
        parent = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="sun")
        child = LookStore.create(conn, asset_id=asset["id"], name="暮", parent_look_id=parent["id"])
        assert LookStore.delete(conn, parent["id"]) is True
        left = LookStore.get(conn, child["id"])
        assert left["parent_look_id"] is None
        assert left["prompt"] is None
        assert LookStore.list_versions(conn, child["id"])[0]["reason"] == "follow"


class TestImagesAndSegments:
    def test_image_rows_are_the_versions_and_keep_provenance(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "林夏")
        look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
        from storage import BlobStore

        blob = BlobStore.put(conn, SampleMedia.png_bytes(), mime="image/png", kind="image")
        first = ImageStore.create(
            conn,
            asset_id=asset["id"],
            look_id=look["id"],
            blob_hash=blob["hash"],
            source="generated",
            model="seedream",
            prompt="portrait day",
            cost=0.4,
            width=8,
            height=8,
        )
        second = ImageStore.create(
            conn,
            asset_id=asset["id"],
            look_id=look["id"],
            source="uploaded",
            model=None,
            prompt=None,
            cost=None,
        )
        frame = ImageStore.create(
            conn,
            look_id=look["id"],
            source="extracted_frame",
            model="ffmpeg",
            prompt="last frame",
            cost=0,
        )
        listed = ImageStore.list(conn, look_id=look["id"])
        assert [item["id"] for item in listed] == [first["id"], second["id"], frame["id"]]
        assert first["id"] != second["id"]
        assert listed[0]["source"] == "generated"
        assert listed[0]["model"] == "seedream"
        assert listed[0]["prompt"] == "portrait day"
        assert listed[0]["cost"] == 0.4
        with pytest.raises(StorageError):
            ImageStore.create(conn, source="scraped")

    def test_segment_ref_binds_asset_and_look_and_resolves_mentions(self, conn):
        project = _project(conn)
        other = _project(conn, "别的项目")
        segment = _asset(conn, project, "第一场", kind="segment")
        hero = _asset(conn, project, "林夏", kind="character")
        day = LookStore.create(conn, asset_id=hero["id"], name="日", prompt="day")
        night = LookStore.create(conn, asset_id=hero["id"], name="夜", prompt="night")
        prop = _asset(conn, project, "伞", kind="prop")
        global_hero = AssetStore.create(conn, name="林夏", kind="character", scope="global")
        LookStore.create(conn, asset_id=global_hero["id"], name="日", prompt="global")
        other_hero = _asset(conn, other, "林夏")
        ref = SegmentRefStore.create(
            conn,
            segment_asset_id=segment["id"],
            asset_id=hero["id"],
            look_id=night["id"],
            role="character",
            alias="@林夏/夜",
        )
        assert ref["look_id"] == night["id"]
        assert SegmentRefStore.list(conn, segment["id"])[0]["id"] == ref["id"]
        with pytest.raises(StorageError):
            SegmentRefStore.create(
                conn,
                segment_asset_id=hero["id"],
                asset_id=prop["id"],
            )
        with pytest.raises(StorageError):
            SegmentRefStore.create(
                conn,
                segment_asset_id=segment["id"],
                asset_id=prop["id"],
                look_id=day["id"],
            )
        text = "开场 @林夏/夜 ，远景 @伞，未知 @没有/人。"
        resolved = SegmentRefStore.resolve(conn, text, project_id=project["id"])
        by_raw = {item["raw"]: item for item in resolved["mentions"]}
        assert by_raw["@林夏/夜"]["resolved"] is True
        assert by_raw["@林夏/夜"]["matches"][0]["asset_id"] == hero["id"]
        assert by_raw["@林夏/夜"]["matches"][0]["look_id"] == night["id"]
        assert by_raw["@林夏/夜"]["matches"][0]["asset_id"] != global_hero["id"]
        assert by_raw["@林夏/夜"]["matches"][0]["asset_id"] != other_hero["id"]
        assert by_raw["@伞"]["resolved"] is True
        assert by_raw["@伞"]["matches"][0]["look_id"] is None
        assert by_raw["@没有/人"]["resolved"] is False
        bare = SegmentRefStore.resolve(conn, "@林夏", project_id=project["id"])
        assert bare["mentions"][0]["resolved"] is True
        assert bare["mentions"][0]["matches"][0]["asset_id"] == hero["id"]
        global_only = SegmentRefStore.resolve(conn, "@模板", project_id=project["id"])
        assert global_only["mentions"][0]["resolved"] is False
        shared = AssetStore.create(conn, name="模板", kind="setting", scope="global")
        found_global = SegmentRefStore.resolve(conn, "@模板", project_id=project["id"])
        assert found_global["mentions"][0]["matches"][0]["asset_id"] == shared["id"]
        twin = _asset(conn, project, "伞", kind="prop")
        ambiguous = SegmentRefStore.resolve(conn, "@伞", project_id=project["id"])
        assert ambiguous["mentions"][0]["ambiguous"] is True
        assert {item["asset_id"] for item in ambiguous["mentions"][0]["matches"]} == {
            prop["id"],
            twin["id"],
        }


class TestStaleQuote:
    def test_visual_change_marks_generated_outputs_and_quote_does_not_spend(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "林夏")
        look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
        segment = _asset(conn, project, "镜头", kind="segment")
        SegmentRefStore.create(
            conn,
            segment_asset_id=segment["id"],
            asset_id=asset["id"],
            look_id=look["id"],
        )
        generated = ImageStore.create(
            conn,
            look_id=look["id"],
            asset_id=asset["id"],
            source="generated",
            model="seedream",
            prompt="day",
            cost=0.5,
        )
        uploaded = ImageStore.create(
            conn,
            look_id=look["id"],
            asset_id=asset["id"],
            source="uploaded",
        )
        before = RegenerationQuote.build(conn, look["id"])
        assert before["artifact_count"] == 0
        assert before["requires_confirmation"] is False
        assert before["executed"] is False
        assert before["will_spend"] is False

        pinned = image_node_id("pinned-image")
        conn.execute(
            """
            INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
            VALUES (?, ?, 'done', 1, '2026-01-01T00:00:00.000000Z')
            """,
            (project["id"], pinned),
        )
        LookStore.update(conn, look["id"], name="日景")
        assert _status(conn, project["id"], image_node_id(generated["id"])) is None

        LookStore.update(conn, look["id"], prompt="night")
        assert _status(conn, project["id"], image_node_id(generated["id"])) == "stale"
        assert _status(conn, project["id"], image_node_id(uploaded["id"])) is None
        assert _status(conn, project["id"], segment["id"]) == "stale"
        assert _status(conn, project["id"], pinned) == "done"

        image_count = _count(conn, "image")
        quote = RegenerationQuote.build(conn, look["id"], model="seedance-2.5")
        assert quote["requires_confirmation"] is True
        assert quote["confirmed"] is False
        assert quote["executed"] is False
        assert quote["will_spend"] is False
        assert quote["estimated_cost"] is None
        assert quote["cost_known"] is False
        kinds = {item["kind"] for item in quote["artifacts"]}
        assert kinds == {"image", "segment"}
        assert uploaded["id"] not in {item["node_id"] for item in quote["artifacts"]}

        priced = RegenerationQuote.build(conn, look["id"], unit_cost=1.25)
        assert priced["estimated_cost"] == 2.5
        assert priced["cost_known"] is True
        with pytest.raises(StorageError):
            RegenerationQuote.confirm(conn, look["id"], "nope", unit_cost=1.25)
        accepted = RegenerationQuote.confirm(
            conn,
            look["id"],
            priced["confirm_token"],
            unit_cost=1.25,
        )
        assert accepted["confirmed"] is True
        assert accepted["executed"] is False
        assert _count(conn, "image") == image_count
        assert ImageStore.get(conn, generated["id"])["prompt"] == "day"

    def test_look_change_revises_a_saved_r6_graph(self, conn):
        project = _project(conn)
        asset = _asset(conn, project, "林夏")
        look = LookStore.create(conn, asset_id=asset["id"], name="日", prompt="day")
        segment = _asset(conn, project, "镜头", kind="segment")
        SegmentRefStore.create(
            conn,
            segment_asset_id=segment["id"],
            asset_id=asset["id"],
            look_id=look["id"],
        )
        graph = DependencyGraph()
        graph.add_node(asset["id"], kind="character", label="林夏")
        graph.add_node(segment["id"], kind="segment", label="镜头")
        graph.add_edge(asset["id"], segment["id"], blocks=False)
        graph.transition(segment["id"], "running")
        graph.transition(segment["id"], "done")
        GraphStore.save(conn, project["id"], graph.to_dict())

        LookStore.update(conn, look["id"], prompt="night")

        restored = DependencyGraph.from_dict(GraphStore.load(conn, project["id"]))
        assert restored.node(asset["id"]).status.value == "pending"
        assert restored.node(segment["id"]).status.value == "stale"
        assert _status(conn, project["id"], segment["id"]) == "stale"

    def test_covered_migration_can_be_retried(self, tmp_path):
        data = tmp_path / "data"
        first = Database.connect(data)
        first.close()
        raw = sqlite3.connect(data / "videoclaw.db")
        try:
            raw.execute("DELETE FROM schema_version WHERE version = ?", (Schema.VERSION,))
            raw.commit()
        finally:
            raw.close()
        reopened = Database.connect(data)
        try:
            assert Schema.current(reopened) == Schema.VERSION
            columns = [row[1] for row in reopened.execute("PRAGMA table_info(look)")]
            assert columns.count("covered_json") == 1
            assert Schema.migrate(reopened) == Schema.VERSION
        finally:
            reopened.close()


def _status(conn, project_id, node_id):
    row = conn.execute(
        "SELECT status FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, node_id),
    ).fetchone()
    if row is None:
        return None
    return row["status"]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
