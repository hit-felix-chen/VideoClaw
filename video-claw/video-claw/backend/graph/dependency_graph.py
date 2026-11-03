# -*- coding: utf-8 -*-
"""In-memory dependency graph whose nodes are production assets.

An edge points from the predecessor that must exist to the successor that
depends on it. A later shot may reference an earlier shot; that edge is
rejected when it would close a cycle. An episode is a group of assets, not a
node. Nothing is written to disk and nothing is priced.
"""

from __future__ import annotations

from collections import deque
from typing import Mapping, Sequence

from graph.errors import (
    CycleError,
    DuplicateIdError,
    EdgeNotFoundError,
    EpisodeNotFoundError,
    GraphError,
    InvalidStructureError,
    InvalidTransitionError,
    NodeNotFoundError,
    NotReadyError,
)
from graph.model import (
    AffectedNode,
    AssetEdge,
    AssetNode,
    EdgeKind,
    Identifiers,
    Impact,
    NodeStatus,
    StructureReport,
)


class GraphSearch:
    """Path and cycle queries over a successor map."""

    @staticmethod
    def path(
        start: str,
        goal: str,
        successors: Mapping[str, Sequence[str]],
    ) -> list[str] | None:
        """Shortest path from `start` to `goal`, both included. None if unreachable."""
        previous: dict[str, str | None] = {start: None}
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            for nxt in successors.get(current, ()):
                if nxt in previous:
                    continue
                previous[nxt] = current
                if nxt == goal:
                    found = [goal]
                    cursor = goal
                    while cursor != start:
                        parent = previous[cursor]
                        if parent is None:
                            break
                        found.append(parent)
                        cursor = parent
                    found.reverse()
                    return found
                queue.append(nxt)
        return None

    @staticmethod
    def closing_cycle(
        predecessor: str,
        successor: str,
        successors: Mapping[str, Sequence[str]],
    ) -> list[str] | None:
        """Cycle created by adding predecessor -> successor, or None.

        The returned list starts and ends with `predecessor`.
        """
        if predecessor == successor:
            return [predecessor, predecessor]
        existing = GraphSearch.path(successor, predecessor, successors)
        if existing is None:
            return None
        return [predecessor, *existing]

    @staticmethod
    def directed_cycle(
        nodes: Sequence[str],
        successors: Mapping[str, Sequence[str]],
    ) -> list[str] | None:
        """One directed cycle as [a, b, c, a], or None when the map is acyclic."""
        color = {node: 0 for node in nodes}
        parent: dict[str, str] = {}

        def walk(node: str) -> list[str] | None:
            color[node] = 1
            for nxt in successors.get(node, ()):
                if nxt not in color:
                    continue
                if color[nxt] == 1:
                    cycle = [node]
                    cursor = node
                    while cursor != nxt:
                        cursor = parent[cursor]
                        cycle.append(cursor)
                    cycle.reverse()
                    cycle.append(nxt)
                    return cycle
                if color[nxt] == 0:
                    parent[nxt] = node
                    found = walk(nxt)
                    if found is not None:
                        return found
            color[node] = 2
            return None

        for node in nodes:
            if color[node] == 0:
                found = walk(node)
                if found is not None:
                    return found
        return None


class DependencyGraph:
    """Assets, dependency edges, and episode groups. Pure memory, JSON-serializable."""

    FORMAT = 1

    def __init__(self) -> None:
        self._nodes: dict[str, AssetNode] = {}
        self._edges: dict[tuple[str, str], AssetEdge] = {}
        self._out: dict[str, list[str]] = {}
        self._in: dict[str, list[str]] = {}
        self._episodes: dict[str, None] = {}
        self._episode_edges: list[tuple[str, str]] = []

    def add_node(
        self,
        node_id: str,
        *,
        kind: str,
        episode_id: str | None = None,
        label: str = "",
        metadata: dict | None = None,
    ) -> AssetNode:
        """Add a pending asset. Further status changes go through `transition`."""
        node_id = Identifiers.token(node_id, what="node")
        if node_id in self._nodes:
            raise DuplicateIdError("node", node_id)
        if not isinstance(kind, str) or not kind.strip():
            raise GraphError("kind must be a non-empty string")
        if episode_id is not None:
            episode_id = Identifiers.token(episode_id, what="episode")
        if not isinstance(label, str):
            raise GraphError("label must be a string")
        node = AssetNode(
            id=node_id,
            kind=kind,
            episode_id=episode_id,
            label=label,
            metadata={} if metadata is None else metadata,
        )
        # AssetNode.from_dict copies and checks metadata. Rebuild so add_node shares that check.
        node = AssetNode.from_dict(node.to_dict())
        self._restore_node(node)
        return node.copy()

    def add_edge(
        self,
        predecessor: str,
        successor: str,
        *,
        kind: EdgeKind | str = EdgeKind.HARD,
        blocks: bool = True,
    ) -> AssetEdge:
        """Depend `successor` on `predecessor`. Reject the edge when it would cycle.

        Every edge kind blocks execution unless `blocks` is false. A blocking
        edge that crosses episodes also orders those episodes.
        """
        predecessor = self._require_node(predecessor)
        successor = self._require_node(successor)
        edge_kind = EdgeKind.parse(kind)
        if not isinstance(blocks, bool):
            raise GraphError("blocks must be a boolean")
        if (predecessor, successor) in self._edges:
            raise DuplicateIdError("edge", f"{predecessor} -> {successor}")
        cycle = GraphSearch.closing_cycle(predecessor, successor, self._out)
        if cycle is not None:
            self._raise_cycle(predecessor, successor, cycle, noun="edge")
        if blocks:
            self._reject_episode_cycle(predecessor, successor)
        edge = AssetEdge(predecessor, successor, edge_kind, blocks)
        self._insert_edge(edge)
        self._invalidate_built(successor)
        return edge

    def add_episode_dependency(self, predecessor: str, successor: str) -> None:
        """Require every asset in `successor` to wait until `predecessor` is complete."""
        predecessor = Identifiers.token(predecessor, what="episode")
        successor = Identifiers.token(successor, what="episode")
        self._episodes.setdefault(predecessor, None)
        self._episodes.setdefault(successor, None)
        if (predecessor, successor) in self._episode_edge_set():
            raise DuplicateIdError("episode dependency", f"{predecessor} -> {successor}")
        cycle = GraphSearch.closing_cycle(predecessor, successor, self._episode_successors())
        if cycle is not None:
            self._raise_cycle(predecessor, successor, cycle, noun="episode dependency")
        self._episode_edges.append((predecessor, successor))

    def remove_edge(self, predecessor: str, successor: str) -> None:
        """Drop an edge. A generated successor becomes stale; nothing is regenerated."""
        predecessor = Identifiers.token(predecessor, what="predecessor")
        successor = Identifiers.token(successor, what="successor")
        if (predecessor, successor) not in self._edges:
            raise EdgeNotFoundError(predecessor, successor)
        self._delete_edge(predecessor, successor)
        if successor in self._nodes:
            self._invalidate_built(successor)

    def remove_node(self, node_id: str) -> None:
        """Remove an asset and its edges. Generated former successors become stale."""
        node_id = self._require_node(node_id)
        outgoing = list(self._out[node_id])
        incoming = list(self._in[node_id])
        for successor in outgoing:
            self._delete_edge(node_id, successor)
        for predecessor in incoming:
            self._delete_edge(predecessor, node_id)
        del self._nodes[node_id]
        del self._out[node_id]
        del self._in[node_id]
        for successor in outgoing:
            if successor in self._nodes:
                self._invalidate_built(successor)

    def transition(self, node_id: str, status: NodeStatus | str) -> AssetNode:
        """Move a node to `status` when that transition is allowed.

        Entering running or done requires every blocking predecessor to be done
        and every explicit predecessor episode to be complete. Entering done
        records predecessor versions and bumps this node's version.
        """
        node_id = self._require_node(node_id)
        target = NodeStatus.parse(status)
        node = self._nodes[node_id]
        if not NodeStatus.can_transition(node.status, target):
            raise InvalidTransitionError(
                node_id,
                node.status.value,
                target.value,
                NodeStatus.allowed_values(node.status),
            )
        source = node.status
        if target in (NodeStatus.RUNNING, NodeStatus.DONE):
            reasons = self._dependency_reasons(node_id)
            if reasons:
                raise NotReadyError(node_id, reasons)
        if target == NodeStatus.STALE:
            self.mark_stale(node_id)
            return self.node(node_id)
        if target == NodeStatus.DONE:
            node.input_versions = {
                pred: self._nodes[pred].version for pred in self._in[node_id]
            }
            node.version += 1
            node.status = NodeStatus.DONE
            self._propagate(node_id, include_self=False)
            return node.copy()
        node.status = target
        if source == NodeStatus.DONE and target == NodeStatus.PENDING:
            self._propagate(node_id, include_self=False)
        return node.copy()

    def pin(self, node_id: str) -> None:
        """Keep upstream changes from marking this asset, or anything only reachable through it, stale."""
        self._nodes[self._require_node(node_id)].pinned = True

    def unpin(self, node_id: str) -> None:
        self._nodes[self._require_node(node_id)].pinned = False

    def revise(self, node_id: str) -> list[str]:
        """Record that the asset changed and stale downstream outputs. Does not regenerate."""
        node = self._nodes[self._require_node(node_id)]
        node.version += 1
        return self._propagate(node_id, include_self=False)

    def mark_stale(self, node_id: str) -> list[str]:
        """Mark this generated asset stale and propagate to downstream outputs.

        Pending and skipped nodes cannot become stale. Pinned downstream nodes
        stay as they are and stop propagation along that path. Nothing is regenerated.
        """
        return self._propagate(self._require_node(node_id), include_self=True)

    def invalidate(self, node_id: str) -> list[str]:
        """Same as `mark_stale`. Nothing is regenerated."""
        return self.mark_stale(node_id)

    def executable_nodes(self) -> list[str]:
        """Nodes that may run now, in topological order.

        Executable means status is pending, stale, or failed; every blocking
        predecessor is done; and every episode this node's episode explicitly
        depends on is complete. Ready is this set, not a stored status.
        """
        ready = []
        for node_id in self.topological_order():
            node = self._nodes[node_id]
            if node.status not in NodeStatus.executable_statuses():
                continue
            if self._dependency_reasons(node_id):
                continue
            ready.append(node_id)
        return ready

    def ready_nodes(self) -> list[str]:
        """Computed ready set. Same as `executable_nodes`."""
        return self.executable_nodes()

    def predecessors(self, node_id: str, depth: int | None = None) -> list[str]:
        """Ancestors in topological order. `depth=1` is direct; None is transitive."""
        self._require_depth(depth)
        return self._within(self._require_node(node_id), depth, incoming=True)

    def successors(self, node_id: str, depth: int | None = None) -> list[str]:
        """Descendants in topological order. `depth=1` is direct; None is transitive."""
        self._require_depth(depth)
        return self._within(self._require_node(node_id), depth, incoming=False)

    def topological_order(self) -> list[str]:
        """All node ids. Predecessors come before the nodes that depend on them."""
        indegree = {node_id: 0 for node_id in self._nodes}
        for _predecessor, successor in self._edges:
            indegree[successor] += 1
        ready = [node_id for node_id in self._nodes if indegree[node_id] == 0]
        order: list[str] = []
        index = 0
        while index < len(ready):
            current = ready[index]
            index += 1
            order.append(current)
            for successor in self._out[current]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
        if len(order) != len(self._nodes):
            cycle = GraphSearch.directed_cycle(list(self._nodes), self._out) or []
            shown = " -> ".join(cycle)
            raise CycleError(f"graph contains a cycle: {shown}", cycle)
        return order

    def stale_nodes(self) -> list[str]:
        """Stale assets in topological order."""
        return [
            node_id
            for node_id in self.topological_order()
            if self._nodes[node_id].status == NodeStatus.STALE
        ]

    def impact(self, node_id: str) -> Impact:
        """Downstream assets affected if this asset changes. No prices.

        Pinned nodes are included and shield their descendants. Pending and
        skipped nodes are omitted, but propagation still looks through them.
        `Impact.with_estimates` is the hook a later cost feature can call.
        """
        node_id = self._require_node(node_id)
        flags: dict[str, bool] = {}
        seen = {node_id}
        queue = deque(self._out[node_id])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            node = self._nodes[current]
            if node.pinned:
                if node.status in NodeStatus.generated_statuses():
                    flags[current] = False
                continue
            if node.status in NodeStatus.generated_statuses():
                flags[current] = node.status in NodeStatus.staleable_statuses()
            queue.extend(self._out[current])
        affected = []
        for current in self.topological_order():
            if current not in flags:
                continue
            node = self._nodes[current]
            affected.append(
                AffectedNode(
                    id=current,
                    kind=node.kind,
                    episode_id=node.episode_id,
                    status=node.status.value,
                    pinned=node.pinned,
                    version=node.version,
                    would_stale=flags[current],
                )
            )
        return Impact(node_id, tuple(affected))

    def what_if(self, node_id: str) -> Impact:
        """Non-mutating preview of `impact`."""
        return self.impact(node_id)

    def why_blocked(self, node_id: str) -> dict:
        """Why a node cannot run, or an empty reason list when it can."""
        node_id = self._require_node(node_id)
        reasons: list[dict] = []
        status = self._nodes[node_id].status
        if status not in NodeStatus.executable_statuses():
            reasons.append({"type": "status", "status": status.value})
        reasons.extend(self._dependency_reasons(node_id))
        return {"node_id": node_id, "executable": not reasons, "reasons": reasons}

    def plan(self) -> list[str]:
        """One topological order of assets that are not done and not skipped.

        The agent chooses what to run. This only respects edge order.
        """
        finished = {NodeStatus.DONE, NodeStatus.SKIPPED}
        return [
            node_id
            for node_id in self.topological_order()
            if self._nodes[node_id].status not in finished
        ]

    def lineage(self, node_id: str) -> list[dict]:
        """Ancestors then the node, roots first, with recorded input versions."""
        node_id = self._require_node(node_id)
        ordered = [*self.predecessors(node_id), node_id]
        return [self._nodes[current].to_dict() for current in ordered]

    def continuity_refs(self, node_id: str) -> list[dict]:
        """Direct continuity edges into this asset (earlier shots it references)."""
        node_id = self._require_node(node_id)
        refs = []
        for predecessor in self._in[node_id]:
            edge = self._edges[(predecessor, node_id)]
            if edge.kind == EdgeKind.CONTINUITY:
                refs.append(edge.to_dict())
        return refs

    def pending_confirmations(self) -> list[str]:
        """Assets waiting for a person, in topological order."""
        return [
            node_id
            for node_id in self.topological_order()
            if self._nodes[node_id].status == NodeStatus.AWAITING_USER
        ]

    def node(self, node_id: str) -> AssetNode:
        """Copy of the asset. Mutating the copy does not change the graph."""
        return self._nodes[self._require_node(node_id)].copy()

    def episodes(self) -> list[str]:
        """Episode ids in registration order."""
        return list(self._episodes)

    def nodes_in_episode(self, episode_id: str) -> list[str]:
        """Assets grouped in the episode, in topological order."""
        episode_id = self._require_episode(episode_id)
        return [
            node_id
            for node_id in self.topological_order()
            if self._nodes[node_id].episode_id == episode_id
        ]

    def is_episode_complete(self, episode_id: str) -> bool:
        """True when the episode has assets and every one of them is done.

        Assets that have not been added are not invented. A skipped asset is
        not done, so the episode is not complete.
        """
        members = self.nodes_in_episode(episode_id)
        if not members:
            return False
        return all(self._nodes[node_id].status == NodeStatus.DONE for node_id in members)

    def episode_dependencies(self, episode_id: str, depth: int | None = None) -> list[str]:
        """Episodes this episode depends on, roots first.

        Includes explicit episode edges and blocking asset edges that cross
        episodes. `depth=1` is direct; None is transitive.
        """
        episode_id = self._require_episode(episode_id)
        self._require_depth(depth)
        if depth == 0:
            return []
        distance = self._episode_distances(episode_id)
        return [
            episode
            for episode in self._episode_order()
            if episode in distance and (depth is None or distance[episode] <= depth)
        ]

    def subgraph(self, node_ids: Sequence[str]) -> DependencyGraph:
        """Induced subgraph. Explicit episode edges survive when both episodes remain."""
        selected: list[str] = []
        seen: set[str] = set()
        for node_id in node_ids:
            node_id = self._require_node(node_id)
            if node_id not in seen:
                seen.add(node_id)
                selected.append(node_id)
        graph = DependencyGraph()
        for node_id in selected:
            graph._restore_node(self._nodes[node_id].copy())
        for edge in self._edges.values():
            if edge.predecessor in seen and edge.successor in seen:
                graph._insert_edge(edge)
        episodes = {
            self._nodes[node_id].episode_id
            for node_id in selected
            if self._nodes[node_id].episode_id
        }
        for predecessor, successor in self._episode_edges:
            if predecessor in episodes and successor in episodes:
                graph._episodes.setdefault(predecessor, None)
                graph._episodes.setdefault(successor, None)
                graph._episode_edges.append((predecessor, successor))
        return graph

    def diff(self, other: DependencyGraph) -> dict:
        """JSON-serializable changes from this graph to `other`."""
        before_nodes = set(self._nodes)
        after_nodes = set(other._nodes)
        status_changes = []
        version_changes = []
        for node_id in self.topological_order():
            if node_id not in other._nodes:
                continue
            before = self._nodes[node_id]
            after = other._nodes[node_id]
            if before.status != after.status:
                status_changes.append(
                    {"id": node_id, "before": before.status.value, "after": after.status.value}
                )
            if before.version != after.version:
                version_changes.append(
                    {"id": node_id, "before": before.version, "after": after.version}
                )
        return {
            "nodes_added": [node_id for node_id in other.topological_order() if node_id not in before_nodes],
            "nodes_removed": [
                node_id for node_id in self.topological_order() if node_id not in after_nodes
            ],
            "status_changes": status_changes,
            "version_changes": version_changes,
            "edges_added": [
                {"predecessor": edge.predecessor, "successor": edge.successor}
                for edge in other._edges.values()
                if (edge.predecessor, edge.successor) not in self._edges
            ],
            "edges_removed": [
                {"predecessor": edge.predecessor, "successor": edge.successor}
                for edge in self._edges.values()
                if (edge.predecessor, edge.successor) not in other._edges
            ],
            "episode_dependencies_added": [
                {"predecessor": pred, "successor": succ}
                for pred, succ in other._episode_edges
                if (pred, succ) not in self._episode_edge_set()
            ],
            "episode_dependencies_removed": [
                {"predecessor": pred, "successor": succ}
                for pred, succ in self._episode_edges
                if (pred, succ) not in other._episode_edge_set()
            ],
        }

    def snapshot(self) -> dict:
        """JSON-serializable copy. Same data as `to_dict`."""
        return self.to_dict()

    def to_dict(self) -> dict:
        """JSON-serializable snapshot. `from_dict` restores it without changing calls."""
        return {
            "format": self.FORMAT,
            "nodes": [node.to_dict() for node in self._nodes.values()],
            "edges": [edge.to_dict() for edge in self._edges.values()],
            "episode_dependencies": [
                {"predecessor": pred, "successor": succ} for pred, succ in self._episode_edges
            ],
        }

    @classmethod
    def from_dict(cls, payload: object) -> DependencyGraph:
        """Rebuild a graph. Invalid structure raises instead of being stored."""
        report = cls.inspect(payload)
        if not report.ok:
            raise InvalidStructureError(report)
        assert isinstance(payload, dict)
        graph = cls()
        for raw in payload.get("nodes", []):
            graph._restore_node(AssetNode.from_dict(raw))
        for raw in payload.get("edges", []):
            graph._insert_edge(AssetEdge.from_dict(raw))
        for raw in payload.get("episode_dependencies", []):
            graph.add_episode_dependency(raw["predecessor"], raw["successor"])
        return graph

    def validate(self) -> StructureReport:
        """Report dangling edges, duplicate ids, and cycles in this graph."""
        return self.inspect(self.to_dict())

    @classmethod
    def inspect(cls, payload: object) -> StructureReport:
        """Report structural problems in a snapshot. Does not raise."""
        report = StructureReport()
        if not isinstance(payload, dict):
            report.add("invalid_payload", "graph payload must be an object")
            return report
        if "format" in payload and payload["format"] != cls.FORMAT:
            report.add("invalid_payload", f"unsupported format: {payload['format']!r}")

        raw_nodes = payload.get("nodes", [])
        raw_edges = payload.get("edges", [])
        raw_episodes = payload.get("episode_dependencies", [])
        if not isinstance(raw_nodes, list):
            report.add("invalid_payload", "nodes must be a list")
            raw_nodes = []
        if not isinstance(raw_edges, list):
            report.add("invalid_payload", "edges must be a list")
            raw_edges = []
        if not isinstance(raw_episodes, list):
            report.add("invalid_payload", "episode_dependencies must be a list")
            raw_episodes = []

        nodes: list[AssetNode] = []
        seen_nodes: set[str] = set()
        for raw in raw_nodes:
            node_id = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(node_id, str) and node_id in seen_nodes:
                report.add("duplicate_node", f"duplicate node: {node_id}", [node_id])
                continue
            try:
                node = AssetNode.from_dict(raw)
            except GraphError as exc:
                ids = [node_id] if isinstance(node_id, str) and node_id.strip() else []
                report.add("invalid_payload", str(exc), ids)
                continue
            seen_nodes.add(node.id)
            nodes.append(node)

        edges: list[AssetEdge] = []
        seen_edges: set[tuple[str, str]] = set()
        for raw in raw_edges:
            try:
                edge = AssetEdge.from_dict(raw)
            except GraphError as exc:
                report.add("invalid_edge", str(exc))
                continue
            pair = (edge.predecessor, edge.successor)
            if pair in seen_edges:
                report.add(
                    "duplicate_edge",
                    f"duplicate edge: {edge.predecessor} -> {edge.successor}",
                    [edge.predecessor, edge.successor],
                )
                continue
            seen_edges.add(pair)
            missing = [end for end in pair if end not in seen_nodes]
            if missing:
                report.add(
                    "dangling_edge",
                    f"dangling edge: {edge.predecessor} -> {edge.successor}",
                    [edge.predecessor, edge.successor],
                )
                continue
            edges.append(edge)

        episode_pairs: list[tuple[str, str]] = []
        seen_episode_pairs: set[tuple[str, str]] = set()
        for raw in raw_episodes:
            if not isinstance(raw, dict):
                report.add("invalid_payload", "episode dependency must be an object")
                continue
            try:
                predecessor = Identifiers.token(raw.get("predecessor"), what="episode")
                successor = Identifiers.token(raw.get("successor"), what="episode")
            except GraphError as exc:
                report.add("invalid_payload", str(exc))
                continue
            pair = (predecessor, successor)
            if pair in seen_episode_pairs:
                report.add(
                    "duplicate_edge",
                    f"duplicate episode dependency: {predecessor} -> {successor}",
                    [predecessor, successor],
                )
                continue
            seen_episode_pairs.add(pair)
            episode_pairs.append(pair)

        successors: dict[str, list[str]] = {node.id: [] for node in nodes}
        for edge in edges:
            successors[edge.predecessor].append(edge.successor)
        cycle = GraphSearch.directed_cycle([node.id for node in nodes], successors)
        if cycle is not None:
            report.add("cycle", "graph contains a cycle: " + " -> ".join(cycle), cycle)

        episode_cycle = cls._episode_cycle(nodes, edges, episode_pairs)
        if episode_cycle is not None:
            report.add(
                "episode_cycle",
                "episode cycle: " + " -> ".join(episode_cycle),
                episode_cycle,
            )
        return report

    def __contains__(self, node_id: object) -> bool:
        return isinstance(node_id, str) and node_id in self._nodes

    def _restore_node(self, node: AssetNode) -> None:
        if node.id in self._nodes:
            raise DuplicateIdError("node", node.id)
        stored = node.copy()
        self._nodes[stored.id] = stored
        self._out[stored.id] = []
        self._in[stored.id] = []
        if stored.episode_id:
            self._episodes.setdefault(stored.episode_id, None)

    def _insert_edge(self, edge: AssetEdge) -> None:
        self._edges[(edge.predecessor, edge.successor)] = edge
        self._out[edge.predecessor].append(edge.successor)
        self._in[edge.successor].append(edge.predecessor)

    def _delete_edge(self, predecessor: str, successor: str) -> None:
        del self._edges[(predecessor, successor)]
        self._out[predecessor].remove(successor)
        self._in[successor].remove(predecessor)

    def _require_node(self, node_id: str) -> str:
        node_id = Identifiers.token(node_id, what="node")
        if node_id not in self._nodes:
            raise NodeNotFoundError(node_id)
        return node_id

    def _require_episode(self, episode_id: str) -> str:
        episode_id = Identifiers.token(episode_id, what="episode")
        if episode_id not in self._episodes:
            raise EpisodeNotFoundError(episode_id)
        return episode_id

    @staticmethod
    def _require_depth(depth: int | None) -> None:
        if depth is None:
            return
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise GraphError("depth must be a non-negative integer or None")

    def _dependency_reasons(self, node_id: str) -> list[dict]:
        reasons = []
        for predecessor in self._in[node_id]:
            edge = self._edges[(predecessor, node_id)]
            if not edge.blocks:
                continue
            status = self._nodes[predecessor].status
            if status != NodeStatus.DONE:
                reasons.append(
                    {
                        "type": "predecessor",
                        "id": predecessor,
                        "status": status.value,
                        "kind": edge.kind.value,
                    }
                )
        episode_id = self._nodes[node_id].episode_id
        if episode_id:
            for pred_episode in self._explicit_episode_predecessors(episode_id):
                if not self.is_episode_complete(pred_episode):
                    reasons.append({"type": "episode", "episode_id": pred_episode})
        return reasons

    def _within(self, origin: str, depth: int | None, *, incoming: bool) -> list[str]:
        if depth == 0:
            return []
        adjacency = self._in if incoming else self._out
        distance: dict[str, int] = {}
        queue: deque[str] = deque()
        for nxt in adjacency[origin]:
            distance[nxt] = 1
            queue.append(nxt)
        while queue:
            current = queue.popleft()
            if depth is not None and distance[current] >= depth:
                continue
            for nxt in adjacency[current]:
                if nxt in distance:
                    continue
                distance[nxt] = distance[current] + 1
                queue.append(nxt)
        return [
            node_id
            for node_id in self.topological_order()
            if node_id in distance and (depth is None or distance[node_id] <= depth)
        ]

    def _propagate(self, origin: str, *, include_self: bool) -> list[str]:
        changed: set[str] = set()
        if include_self:
            node = self._nodes[origin]
            if node.status == NodeStatus.STALE:
                pass
            elif node.status not in NodeStatus.staleable_statuses():
                raise InvalidTransitionError(
                    origin,
                    node.status.value,
                    NodeStatus.STALE.value,
                    NodeStatus.allowed_values(node.status),
                )
            elif self._try_stale(origin):
                changed.add(origin)
        seen = {origin}
        queue = deque(self._out[origin])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            if self._nodes[current].pinned:
                continue
            if self._try_stale(current):
                changed.add(current)
            queue.extend(self._out[current])
        return [node_id for node_id in self.topological_order() if node_id in changed]

    def _try_stale(self, node_id: str) -> bool:
        node = self._nodes[node_id]
        if node.status not in NodeStatus.staleable_statuses():
            return False
        if not NodeStatus.can_transition(node.status, NodeStatus.STALE):
            return False
        node.status = NodeStatus.STALE
        return True

    def _invalidate_built(self, node_id: str) -> None:
        """Stale a generated asset whose inputs changed, then walk downstream.

        Pending and skipped assets are not marked; the walk still continues so a
        later generated asset does not keep the old input. Pinned assets stop it.
        """
        if node_id not in self._nodes or self._nodes[node_id].pinned:
            return
        status = self._nodes[node_id].status
        include_self = status == NodeStatus.STALE or status in NodeStatus.staleable_statuses()
        self._propagate(node_id, include_self=include_self)

    def _explicit_episode_predecessors(self, episode_id: str) -> list[str]:
        return [pred for pred, succ in self._episode_edges if succ == episode_id]

    def _direct_episode_dependencies(self, episode_id: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for predecessor in self._explicit_episode_predecessors(episode_id):
            found.append(predecessor)
            seen.add(predecessor)
        for node_id, node in self._nodes.items():
            if node.episode_id != episode_id:
                continue
            for predecessor in self._in[node_id]:
                edge = self._edges[(predecessor, node_id)]
                if not edge.blocks:
                    continue
                other = self._nodes[predecessor].episode_id
                if other and other != episode_id and other not in seen:
                    found.append(other)
                    seen.add(other)
        return found

    def _episode_successors(self) -> dict[str, list[str]]:
        succs: dict[str, list[str]] = {episode: [] for episode in self._episodes}
        for episode in self._episodes:
            for predecessor in self._direct_episode_dependencies(episode):
                succs.setdefault(predecessor, [])
                if episode not in succs[predecessor]:
                    succs[predecessor].append(episode)
        return succs

    def _episode_distances(self, episode_id: str) -> dict[str, int]:
        distance: dict[str, int] = {}
        queue: deque[str] = deque()
        for predecessor in self._direct_episode_dependencies(episode_id):
            distance[predecessor] = 1
            queue.append(predecessor)
        while queue:
            current = queue.popleft()
            for predecessor in self._direct_episode_dependencies(current):
                if predecessor in distance:
                    continue
                distance[predecessor] = distance[current] + 1
                queue.append(predecessor)
        return distance

    def _episode_order(self) -> list[str]:
        succs = self._episode_successors()
        indegree = {episode: 0 for episode in succs}
        for children in succs.values():
            for child in children:
                indegree[child] = indegree.get(child, 0) + 1
        ready = [episode for episode in self._episodes if indegree.get(episode, 0) == 0]
        for episode in indegree:
            if episode not in self._episodes and indegree[episode] == 0 and episode not in ready:
                ready.append(episode)
        order: list[str] = []
        index = 0
        while index < len(ready):
            current = ready[index]
            index += 1
            order.append(current)
            for child in succs.get(current, []):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        return order

    def _episode_edge_set(self) -> set[tuple[str, str]]:
        return set(self._episode_edges)

    def _reject_episode_cycle(self, predecessor: str, successor: str) -> None:
        pred_episode = self._nodes[predecessor].episode_id
        succ_episode = self._nodes[successor].episode_id
        if not pred_episode or not succ_episode or pred_episode == succ_episode:
            return
        cycle = GraphSearch.closing_cycle(pred_episode, succ_episode, self._episode_successors())
        if cycle is None:
            return
        shown = " -> ".join(cycle)
        raise CycleError(
            f"adding edge from {predecessor!r} to {successor!r} would create an episode cycle: {shown}",
            cycle,
        )

    @staticmethod
    def _raise_cycle(predecessor: str, successor: str, cycle: list[str], *, noun: str) -> None:
        shown = " -> ".join(cycle)
        raise CycleError(
            f"adding {noun} from {predecessor!r} to {successor!r} would create a cycle: {shown}",
            cycle,
        )

    @classmethod
    def _episode_cycle(
        cls,
        nodes: Sequence[AssetNode],
        edges: Sequence[AssetEdge],
        episode_pairs: Sequence[tuple[str, str]],
    ) -> list[str] | None:
        by_id = {node.id: node for node in nodes}
        episodes: list[str] = []
        seen: set[str] = set()

        def add_episode(episode_id: str | None) -> None:
            if episode_id and episode_id not in seen:
                seen.add(episode_id)
                episodes.append(episode_id)

        for node in nodes:
            add_episode(node.episode_id)
        succs: dict[str, list[str]] = {episode: [] for episode in episodes}

        def link(predecessor: str, successor: str) -> None:
            add_episode(predecessor)
            add_episode(successor)
            succs.setdefault(predecessor, [])
            succs.setdefault(successor, [])
            if successor not in succs[predecessor]:
                succs[predecessor].append(successor)

        for predecessor, successor in episode_pairs:
            link(predecessor, successor)
        for edge in edges:
            if not edge.blocks:
                continue
            pred_episode = by_id[edge.predecessor].episode_id
            succ_episode = by_id[edge.successor].episode_id
            if pred_episode and succ_episode and pred_episode != succ_episode:
                link(pred_episode, succ_episode)
        return GraphSearch.directed_cycle(list(succs), succs)
