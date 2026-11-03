# -*- coding: utf-8 -*-
"""Dependency graph: cycles, readiness, staleness, episodes, and snapshots."""

import json
from pathlib import Path

import pytest

from graph import (
    AssetKind,
    CycleError,
    DependencyGraph,
    DuplicateIdError,
    EdgeKind,
    EdgeNotFoundError,
    EpisodeNotFoundError,
    GraphError,
    InvalidStructureError,
    InvalidTransitionError,
    NodeNotFoundError,
    NodeStatus,
    NotReadyError,
)


class GraphFixture:
    """Builders for short-drama graphs. Helpers stay on the class."""

    @staticmethod
    def produce(graph: DependencyGraph, *node_ids: str) -> None:
        for node_id in node_ids:
            graph.transition(node_id, NodeStatus.RUNNING)
            graph.transition(node_id, NodeStatus.DONE)

    @staticmethod
    def episode() -> DependencyGraph:
        """One episode: script, series looks, two shots, final video. Episode 2 waits on it."""
        graph = DependencyGraph()
        graph.add_node("ep1-script", kind=AssetKind.SCRIPT, episode_id="ep1")
        graph.add_node("hero", kind=AssetKind.CHARACTER, metadata={"role": "lead"})
        graph.add_node("tavern", kind=AssetKind.SCENE)
        graph.add_node("ep1-shot1", kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")
        graph.add_node("ep1-shot2", kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")
        graph.add_node("ep1-final", kind=AssetKind.FINAL_VIDEO, episode_id="ep1")
        graph.add_node("ep2-script", kind=AssetKind.SCRIPT, episode_id="ep2")
        graph.add_edge("ep1-script", "hero")
        graph.add_edge("ep1-script", "tavern")
        graph.add_edge("hero", "ep1-shot1")
        graph.add_edge("tavern", "ep1-shot1")
        graph.add_edge("hero", "ep1-shot2")
        graph.add_edge("tavern", "ep1-shot2")
        graph.add_edge("ep1-shot1", "ep1-shot2", kind=EdgeKind.CONTINUITY)
        graph.add_edge("ep1-shot1", "ep1-final")
        graph.add_edge("ep1-shot2", "ep1-final")
        graph.add_episode_dependency("ep1", "ep2")
        return graph


def test_executable_nodes_follow_partial_completion():
    graph = GraphFixture.episode()
    assert graph.executable_nodes() == ["ep1-script"]
    assert graph.ready_nodes() == ["ep1-script"]
    assert graph.node("ep1-script").status == NodeStatus.PENDING

    GraphFixture.produce(graph, "ep1-script")
    assert graph.executable_nodes() == ["hero", "tavern"]
    assert "ep2-script" not in graph.executable_nodes()

    GraphFixture.produce(graph, "hero")
    assert graph.executable_nodes() == ["tavern"]
    GraphFixture.produce(graph, "tavern")
    assert graph.executable_nodes() == ["ep1-shot1"]

    GraphFixture.produce(graph, "ep1-shot1")
    assert graph.executable_nodes() == ["ep1-shot2"]
    GraphFixture.produce(graph, "ep1-shot2")
    assert graph.executable_nodes() == ["ep1-final"]
    assert graph.is_episode_complete("ep1") is False

    GraphFixture.produce(graph, "ep1-final")
    assert graph.is_episode_complete("ep1") is True
    assert graph.executable_nodes() == ["ep2-script"]
    assert graph.why_blocked("ep1-final")["executable"] is False
    assert graph.why_blocked("ep2-script")["executable"] is True


def test_ready_is_computed_and_failed_nodes_can_retry():
    graph = DependencyGraph()
    graph.add_node("script", kind=AssetKind.SCRIPT, episode_id="ep1")
    assert "ready" not in {status.value for status in NodeStatus}
    graph.transition("script", NodeStatus.RUNNING)
    assert graph.executable_nodes() == []
    graph.transition("script", NodeStatus.FAILED)
    assert graph.executable_nodes() == ["script"]
    assert graph.why_blocked("script") == {
        "node_id": "script",
        "executable": True,
        "reasons": [],
    }
    GraphFixture.produce(graph, "script")
    assert graph.node("script").status == NodeStatus.DONE
    assert graph.node("script").version == 1


def test_direct_and_transitive_neighbors_and_depth():
    graph = GraphFixture.episode()
    assert graph.predecessors("ep1-shot2", depth=1) == ["hero", "tavern", "ep1-shot1"]
    assert graph.predecessors("ep1-shot2") == [
        "ep1-script",
        "hero",
        "tavern",
        "ep1-shot1",
    ]
    assert graph.successors("ep1-script", depth=1) == ["hero", "tavern"]
    assert graph.successors("hero") == ["ep1-shot1", "ep1-shot2", "ep1-final"]
    assert graph.predecessors("ep1-script", depth=0) == []
    assert "ep1-shot2" not in graph.predecessors("ep1-shot2")

    with pytest.raises(GraphError):
        graph.successors("hero", depth=-1)
    with pytest.raises(GraphError):
        graph.predecessors("hero", depth=True)  # type: ignore[arg-type]


def test_topological_order_puts_dependencies_first():
    graph = DependencyGraph()
    for node_id in ("a", "b", "c", "d"):
        graph.add_node(node_id, kind=AssetKind.SHOT_SEGMENT)
    graph.add_edge("a", "b")
    graph.add_edge("a", "c")
    graph.add_edge("b", "d")
    graph.add_edge("c", "d")
    order = graph.topological_order()
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")
    assert graph.plan() == order


def test_direct_and_indirect_cycles_are_rejected():
    graph = DependencyGraph()
    for node_id in ("a", "b", "c", "d"):
        graph.add_node(node_id, kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")

    with pytest.raises(CycleError) as self_loop:
        graph.add_edge("a", "a")
    assert self_loop.value.cycle == ["a", "a"]
    assert "a -> a" in str(self_loop.value)

    graph.add_edge("a", "b")
    with pytest.raises(CycleError) as back:
        graph.add_edge("b", "a")
    assert back.value.cycle == ["b", "a", "b"]

    graph.add_edge("b", "c")
    graph.add_edge("c", "d")
    with pytest.raises(CycleError) as indirect:
        graph.add_edge("d", "b")
    assert indirect.value.cycle == ["d", "b", "c", "d"]
    assert "adding edge from 'd' to 'b' would create a cycle: d -> b -> c -> d" in str(
        indirect.value
    )
    # A forward shortcut is not a cycle.
    graph.add_edge("a", "c", kind=EdgeKind.REF)
    assert graph.validate().ok is True


def test_nonblocking_reference_does_not_gate_or_order_episodes():
    graph = DependencyGraph()
    graph.add_node("look", kind=AssetKind.CHARACTER, episode_id="ep2")
    graph.add_node("shot", kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")
    graph.add_edge("look", "shot", kind=EdgeKind.REF, blocks=False)
    assert graph.executable_nodes() == ["look", "shot"]
    assert graph.episode_dependencies("ep1") == []
    GraphFixture.produce(graph, "shot")
    assert graph.node("shot").status == NodeStatus.DONE


def test_duplicate_ids_and_missing_endpoints_raise():
    graph = DependencyGraph()
    graph.add_node("a", kind=AssetKind.SCRIPT)
    with pytest.raises(DuplicateIdError):
        graph.add_node("a", kind=AssetKind.SCENE)
    graph.add_node("b", kind=AssetKind.SCENE)
    graph.add_edge("a", "b")
    with pytest.raises(DuplicateIdError):
        graph.add_edge("a", "b", kind=EdgeKind.INHERIT)
    with pytest.raises(NodeNotFoundError):
        graph.add_edge("a", "missing")
    with pytest.raises(EdgeNotFoundError):
        graph.remove_edge("b", "a")
    with pytest.raises(GraphError):
        graph.add_node(" ", kind=AssetKind.SCRIPT)
    with pytest.raises(GraphError):
        graph.add_node("ok", kind=AssetKind.SCRIPT, metadata={"bad": {1}})


def test_status_transitions_are_validated_and_done_records_inputs():
    graph = DependencyGraph()
    graph.add_node("script", kind=AssetKind.SCRIPT, episode_id="ep1")
    graph.add_node("hero", kind=AssetKind.CHARACTER)
    graph.add_edge("script", "hero")

    with pytest.raises(InvalidTransitionError) as denied:
        graph.transition("script", NodeStatus.DONE)
    assert "pending" in str(denied.value)
    assert "running" in str(denied.value)

    with pytest.raises(NotReadyError) as early:
        graph.transition("hero", NodeStatus.RUNNING)
    assert "script" in str(early.value)

    GraphFixture.produce(graph, "script")
    graph.transition("hero", NodeStatus.RUNNING)
    graph.transition("hero", NodeStatus.AWAITING_USER)
    assert graph.pending_confirmations() == ["hero"]
    assert graph.executable_nodes() == []
    graph.transition("hero", NodeStatus.DONE)
    hero = graph.node("hero")
    assert hero.version == 1
    assert hero.input_versions == {"script": 1}
    assert hero.status == NodeStatus.DONE

    copy = graph.node("hero")
    copy.metadata["mutated"] = True
    assert "mutated" not in graph.node("hero").metadata


def test_skipped_predecessor_does_not_satisfy_a_dependency():
    graph = DependencyGraph()
    graph.add_node("previz", kind=AssetKind.PREVIZ, episode_id="ep1")
    graph.add_node("shot", kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")
    graph.add_edge("previz", "shot", kind=EdgeKind.HARD)
    graph.transition("previz", NodeStatus.SKIPPED)
    assert graph.executable_nodes() == []
    graph.remove_edge("previz", "shot")
    assert graph.executable_nodes() == ["shot"]


def test_stale_propagates_downstream_and_not_upstream():
    graph = GraphFixture.episode()
    GraphFixture.produce(
        graph, "ep1-script", "hero", "tavern", "ep1-shot1", "ep1-shot2", "ep1-final"
    )
    changed = graph.revise("hero")
    assert graph.node("hero").status == NodeStatus.DONE
    assert graph.node("hero").version == 2
    assert changed == ["ep1-shot1", "ep1-shot2", "ep1-final"]
    assert graph.stale_nodes() == ["ep1-shot1", "ep1-shot2", "ep1-final"]
    assert graph.node("tavern").status == NodeStatus.DONE
    assert graph.node("ep1-script").status == NodeStatus.DONE
    # The stale shot can be regenerated. Later shots stay blocked on it.
    assert graph.executable_nodes() == ["ep1-shot1"]

    graph.transition("ep1-shot1", NodeStatus.RUNNING)
    graph.transition("ep1-shot1", NodeStatus.DONE)
    assert graph.node("ep1-shot1").version == 2
    assert graph.node("ep1-shot1").input_versions["hero"] == 2
    assert graph.executable_nodes() == ["ep1-shot2"]


def test_mark_stale_walks_through_pending_and_honors_pins():
    graph = DependencyGraph()
    for node_id in ("a", "b", "c"):
        graph.add_node(node_id, kind=AssetKind.SHOT_SEGMENT)
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    GraphFixture.produce(graph, "a", "b", "c")
    graph.transition("b", NodeStatus.PENDING)
    assert graph.node("c").status == NodeStatus.STALE

    restored = DependencyGraph.from_dict(
        {
            "nodes": [
                {"id": "a", "kind": "shot_segment", "status": "done", "version": 1},
                {"id": "b", "kind": "shot_segment", "status": "pending"},
                {"id": "c", "kind": "shot_segment", "status": "done", "version": 1},
                {"id": "shield", "kind": "shot_segment", "status": "done", "version": 1, "pinned": True},
                {"id": "hidden", "kind": "shot_segment", "status": "done", "version": 1},
                {"id": "side", "kind": "shot_segment", "status": "done", "version": 1},
            ],
            "edges": [
                {"predecessor": "a", "successor": "b"},
                {"predecessor": "b", "successor": "c"},
                {"predecessor": "a", "successor": "shield"},
                {"predecessor": "shield", "successor": "hidden"},
                {"predecessor": "a", "successor": "side"},
            ],
        }
    )
    changed = restored.invalidate("a")
    assert restored.node("a").status == NodeStatus.STALE
    assert restored.node("b").status == NodeStatus.PENDING
    assert restored.node("c").status == NodeStatus.STALE
    assert restored.node("shield").status == NodeStatus.DONE
    assert restored.node("hidden").status == NodeStatus.DONE
    assert restored.node("side").status == NodeStatus.STALE
    assert changed == ["a", "side", "c"]

    pinned_changed = restored.mark_stale("shield")
    assert restored.node("shield").status == NodeStatus.STALE
    assert restored.node("hidden").status == NodeStatus.STALE
    assert pinned_changed == ["shield", "hidden"]

    with pytest.raises(InvalidTransitionError):
        restored.mark_stale("b")


def test_impact_lists_affected_assets_without_prices():
    graph = GraphFixture.episode()
    GraphFixture.produce(graph, "ep1-script", "hero", "tavern", "ep1-shot1", "ep1-shot2")
    graph.pin("ep1-shot2")
    impact = graph.impact("hero")
    payload = impact.to_dict()
    assert payload["source_id"] == "hero"
    assert [item["id"] for item in payload["affected"]] == ["ep1-shot1", "ep1-shot2"]
    by_id = {item["id"]: item for item in payload["affected"]}
    assert by_id["ep1-shot1"]["would_stale"] is True
    assert by_id["ep1-shot2"]["pinned"] is True
    assert by_id["ep1-shot2"]["would_stale"] is False
    assert "ep1-final" not in by_id
    assert "price" not in payload
    assert "estimate" not in payload

    priced = impact.with_estimates(lambda node: {"redo": node["would_stale"]})
    assert priced["estimates"] == [
        {"id": "ep1-shot1", "estimate": {"redo": True}},
        {"id": "ep1-shot2", "estimate": {"redo": False}},
    ]
    assert graph.what_if("hero").to_dict() == payload
    assert graph.node("ep1-shot1").status == NodeStatus.DONE


def test_episode_queries_gate_and_transitive_dependencies():
    graph = GraphFixture.episode()
    assert graph.nodes_in_episode("ep1") == ["ep1-script", "ep1-shot1", "ep1-shot2", "ep1-final"]
    assert "hero" not in graph.nodes_in_episode("ep1")
    assert graph.episode_dependencies("ep2") == ["ep1"]
    assert graph.episode_dependencies("ep1") == []
    assert graph.is_episode_complete("ep2") is False
    blocked = graph.why_blocked("ep2-script")
    assert blocked["executable"] is False
    assert {"type": "episode", "episode_id": "ep1"} in blocked["reasons"]

    graph.add_node("ep3-script", kind=AssetKind.SCRIPT, episode_id="ep3")
    graph.add_episode_dependency("ep2", "ep3")
    assert graph.episode_dependencies("ep3", depth=1) == ["ep2"]
    assert graph.episode_dependencies("ep3") == ["ep1", "ep2"]

    with pytest.raises(EpisodeNotFoundError):
        graph.nodes_in_episode("missing")
    with pytest.raises(DuplicateIdError):
        graph.add_episode_dependency("ep1", "ep2")


def test_episode_cycles_from_group_edges_and_cross_episode_assets():
    graph = DependencyGraph()
    graph.add_node("a", kind=AssetKind.SCRIPT, episode_id="ep1")
    graph.add_node("b", kind=AssetKind.SCRIPT, episode_id="ep2")
    graph.add_episode_dependency("ep1", "ep2")
    with pytest.raises(CycleError) as episode_cycle:
        graph.add_edge("b", "a")
    assert episode_cycle.value.cycle == ["ep2", "ep1", "ep2"]
    assert "episode cycle" in str(episode_cycle.value)

    other = DependencyGraph()
    other.add_node("a", kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")
    other.add_node("b", kind=AssetKind.SHOT_SEGMENT, episode_id="ep2")
    other.add_edge("b", "a")
    assert other.episode_dependencies("ep1") == ["ep2"]
    with pytest.raises(CycleError) as reverse:
        other.add_episode_dependency("ep1", "ep2")
    assert reverse.value.cycle[0] == "ep1"
    assert reverse.value.cycle[-1] == "ep1"

    with pytest.raises(CycleError):
        other.add_episode_dependency("ep1", "ep1")


def test_continuity_refs_and_new_edge_stales_a_built_shot():
    graph = DependencyGraph()
    graph.add_node("shot1", kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")
    graph.add_node("shot2", kind=AssetKind.SHOT_SEGMENT, episode_id="ep1")
    GraphFixture.produce(graph, "shot1", "shot2")
    graph.add_edge("shot1", "shot2", kind=EdgeKind.CONTINUITY)
    assert graph.node("shot2").status == NodeStatus.STALE
    assert graph.node("shot1").status == NodeStatus.DONE
    assert graph.continuity_refs("shot2") == [
        {
            "predecessor": "shot1",
            "successor": "shot2",
            "kind": "continuity",
            "blocks": True,
        }
    ]
    assert graph.continuity_refs("shot1") == []


def test_withdrawing_a_done_asset_stales_downstream():
    graph = DependencyGraph()
    graph.add_node("script", kind=AssetKind.SCRIPT)
    graph.add_node("hero", kind=AssetKind.CHARACTER)
    graph.add_edge("script", "hero")
    GraphFixture.produce(graph, "script", "hero")
    graph.transition("script", NodeStatus.PENDING)
    assert graph.node("script").status == NodeStatus.PENDING
    assert graph.node("hero").status == NodeStatus.STALE
    with pytest.raises(NotReadyError):
        graph.transition("hero", NodeStatus.RUNNING)


def test_inspect_reports_dangling_edges_duplicates_and_cycles():
    dangling = DependencyGraph.inspect(
        {
            "nodes": [{"id": "a", "kind": "script"}],
            "edges": [{"predecessor": "a", "successor": "missing"}],
        }
    )
    assert dangling.ok is False
    assert "dangling_edge" in dangling.codes()

    duplicate = DependencyGraph.inspect(
        {
            "nodes": [
                {"id": "a", "kind": "script"},
                {"id": "a", "kind": "scene"},
            ],
            "edges": [
                {"predecessor": "a", "successor": "a"},
                {"predecessor": "a", "successor": "a"},
            ],
        }
    )
    assert "duplicate_node" in duplicate.codes()
    assert "duplicate_edge" in duplicate.codes()
    assert "cycle" in duplicate.codes()

    episode_cycle = DependencyGraph.inspect(
        {
            "nodes": [
                {"id": "a", "kind": "script", "episode_id": "ep1"},
                {"id": "b", "kind": "script", "episode_id": "ep2"},
            ],
            "edges": [{"predecessor": "b", "successor": "a"}],
            "episode_dependencies": [{"predecessor": "ep1", "successor": "ep2"}],
        }
    )
    assert "episode_cycle" in episode_cycle.codes()

    with pytest.raises(InvalidStructureError) as raised:
        DependencyGraph.from_dict(
            {
                "nodes": [{"id": "a", "kind": "script"}],
                "edges": [{"predecessor": "missing", "successor": "a"}],
            }
        )
    assert "dangling_edge" in raised.value.report.codes()


def test_snapshot_round_trip_and_diff():
    graph = GraphFixture.episode()
    graph.pin("ep1-shot2")
    GraphFixture.produce(graph, "ep1-script", "hero", "tavern", "ep1-shot1")
    graph.revise("hero")
    before = graph.snapshot()
    encoded = json.loads(json.dumps(before))
    restored = DependencyGraph.from_dict(encoded)
    assert restored.to_dict() == before
    assert restored.stale_nodes() == graph.stale_nodes()
    assert restored.executable_nodes() == graph.executable_nodes()
    assert restored.episode_dependencies("ep2") == ["ep1"]
    assert restored.node("hero").metadata == {"role": "lead"}
    assert restored.node("ep1-shot2").pinned is True
    assert restored.validate().ok is True

    other = DependencyGraph.from_dict(json.loads(json.dumps(before)))
    GraphFixture.produce(other, "ep1-shot1")
    changes = graph.diff(other)
    assert changes["nodes_added"] == []
    assert changes["edges_added"] == []
    assert {"id": "ep1-shot1", "before": "stale", "after": "done"} in changes["status_changes"]
    assert any(item["id"] == "ep1-shot1" for item in changes["version_changes"])


def test_subgraph_lineage_and_plugin_removal():
    graph = GraphFixture.episode()
    graph.add_node("previz", kind=AssetKind.PREVIZ, episode_id="ep1")
    graph.add_edge("ep1-script", "previz")
    graph.add_edge("previz", "ep1-shot1")
    assert "previz" in graph.predecessors("ep1-shot1", depth=1)

    lineage = graph.lineage("ep1-shot2")
    assert lineage[0]["id"] == "ep1-script"
    assert lineage[-1]["id"] == "ep1-shot2"
    assert "input_versions" in lineage[-1]

    piece = graph.subgraph(["ep1-shot1", "ep1-shot2", "ep1-final"])
    assert piece.topological_order() == ["ep1-shot1", "ep1-shot2", "ep1-final"]
    assert len(piece.to_dict()["edges"]) == 3
    piece.transition("ep1-shot1", NodeStatus.RUNNING)
    assert graph.node("ep1-shot1").status == NodeStatus.PENDING

    graph.remove_node("previz")
    assert "previz" not in graph
    assert "previz" not in graph.predecessors("ep1-shot1")
    assert graph.validate().ok is True


def test_package_does_not_touch_legacy_workflow_or_io():
    root = Path(__file__).resolve().parents[1] / "graph"
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py")))
    for banned in (
        "import pipelines",
        "from pipelines",
        "import session",
        "orchestrator",
        "open(",
    ):
        assert banned not in text
    assert AssetKind.DRAFT_VIDEO == "draft_video"
    assert EdgeKind.INHERIT == "inherit"


def test_cancelling_a_run_does_not_stale_downstream():
    graph = DependencyGraph()
    graph.add_node("script", kind=AssetKind.SCRIPT)
    graph.add_node("hero", kind=AssetKind.CHARACTER)
    graph.add_edge("script", "hero")
    GraphFixture.produce(graph, "script")
    graph.transition("hero", NodeStatus.RUNNING)
    graph.transition("hero", NodeStatus.PENDING)
    assert graph.node("hero").status == NodeStatus.PENDING
    assert graph.node("script").status == NodeStatus.DONE
    assert graph.stale_nodes() == []
