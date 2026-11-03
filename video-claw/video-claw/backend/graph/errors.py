# -*- coding: utf-8 -*-
"""Errors raised by the in-memory asset dependency graph."""

from __future__ import annotations


class GraphError(Exception):
    """Base error for graph operations."""


class NodeNotFoundError(GraphError):
    """A node id is not in the graph."""

    def __init__(self, node_id: str):
        self.node_id = node_id
        super().__init__(f"unknown node: {node_id}")


class EpisodeNotFoundError(GraphError):
    """An episode id has not been registered."""

    def __init__(self, episode_id: str):
        self.episode_id = episode_id
        super().__init__(f"unknown episode: {episode_id}")


class EdgeNotFoundError(GraphError):
    """No edge runs from the predecessor to the successor."""

    def __init__(self, predecessor: str, successor: str):
        self.predecessor = predecessor
        self.successor = successor
        super().__init__(f"unknown edge: {predecessor} -> {successor}")


class DuplicateIdError(GraphError):
    """A node, edge, or episode dependency is already present."""

    def __init__(self, kind: str, identifier: str):
        self.kind = kind
        self.identifier = identifier
        super().__init__(f"duplicate {kind}: {identifier}")


class CycleError(GraphError):
    """Adding the edge would introduce a directed cycle."""

    def __init__(self, message: str, cycle: list[str]):
        self.cycle = list(cycle)
        super().__init__(message)


class InvalidTransitionError(GraphError):
    """The status change is not in the allowed transition table."""

    def __init__(self, node_id: str, source: str, target: str, allowed: list[str]):
        self.node_id = node_id
        self.source = source
        self.target = target
        self.allowed = list(allowed)
        allowed_text = ", ".join(allowed) if allowed else "(none)"
        super().__init__(
            f"cannot transition {node_id!r} from {source} to {target}; allowed: {allowed_text}"
        )


class NotReadyError(GraphError):
    """A node cannot run or finish because a dependency is incomplete."""

    def __init__(self, node_id: str, reasons: list[dict]):
        self.node_id = node_id
        self.reasons = list(reasons)
        super().__init__(f"{node_id!r} cannot run until {self.describe(reasons)}")

    @staticmethod
    def describe(reasons: list[dict]) -> str:
        """Short text for dependency reasons. No pricing."""
        parts: list[str] = []
        for reason in reasons:
            kind = reason.get("type")
            if kind == "predecessor":
                parts.append(
                    f"predecessor {reason['id']!r} is {reason['status']} via {reason['kind']}"
                )
            elif kind == "episode":
                parts.append(f"episode {reason['episode_id']!r} is not complete")
            else:
                parts.append(str(reason))
        return "; ".join(parts) if parts else "dependencies are incomplete"


class InvalidStructureError(GraphError):
    """A serialized graph has dangling edges, duplicate ids, or a cycle."""

    def __init__(self, report: object):
        self.report = report
        super().__init__(str(report))
