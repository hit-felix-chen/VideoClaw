# -*- coding: utf-8 -*-
"""Asset records, statuses, and JSON snapshots for the dependency graph."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping

from graph.errors import GraphError


class JsonValue:
    """JSON object checks. Snapshots stay serializable without a new dependency."""

    @staticmethod
    def is_bool(value: object) -> bool:
        return isinstance(value, bool)

    @staticmethod
    def is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def require(cls, value: object, *, path: str = "value") -> None:
        """Raise GraphError when `value` is not JSON-serializable."""
        if value is None or cls.is_bool(value):
            return
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise GraphError(f"{path} is not JSON-serializable")
            return
        if cls.is_int(value) or isinstance(value, str):
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                cls.require(item, path=f"{path}[{index}]")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise GraphError(f"{path} has a non-string key")
                cls.require(item, path=f"{path}.{key}")
            return
        raise GraphError(f"{path} is not JSON-serializable")

    @classmethod
    def object_copy(cls, value: object, *, path: str = "metadata") -> dict:
        if not isinstance(value, dict):
            raise GraphError(f"{path} must be a JSON object")
        cls.require(value, path=path)
        return copy.deepcopy(value)


class AssetKind:
    """Well-known asset kinds. Any non-empty string is accepted so plugins can insert nodes."""

    SCRIPT = "script"
    CHARACTER = "character"
    SCENE = "scene"
    SHOT_SEGMENT = "shot_segment"
    FINAL_VIDEO = "final_video"
    PREVIZ = "previz"
    KEYFRAME = "keyframe"
    DRAFT_VIDEO = "draft_video"

    @staticmethod
    def known() -> frozenset[str]:
        return frozenset(
            {
                AssetKind.SCRIPT,
                AssetKind.CHARACTER,
                AssetKind.SCENE,
                AssetKind.SHOT_SEGMENT,
                AssetKind.FINAL_VIDEO,
                AssetKind.PREVIZ,
                AssetKind.KEYFRAME,
                AssetKind.DRAFT_VIDEO,
            }
        )


class NodeStatus(str, Enum):
    """Stored node status. `ready` is computed by the graph and is not a member."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    DONE = "done"
    STALE = "stale"
    FAILED = "failed"
    SKIPPED = "skipped"

    @classmethod
    def parse(cls, value: NodeStatus | str) -> NodeStatus:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        known = ", ".join(item.value for item in cls)
        raise GraphError(f"unknown status: {value!r}; expected one of: {known}")

    @classmethod
    def allowed_targets(cls, status: NodeStatus | str) -> frozenset[NodeStatus]:
        return _ALLOWED[cls.parse(status)]

    @classmethod
    def allowed_values(cls, status: NodeStatus | str) -> list[str]:
        return sorted(item.value for item in cls.allowed_targets(status))

    @classmethod
    def can_transition(cls, source: NodeStatus | str, target: NodeStatus | str) -> bool:
        return cls.parse(target) in cls.allowed_targets(source)

    @staticmethod
    def executable_statuses() -> frozenset[NodeStatus]:
        """Statuses that may start work once every blocking predecessor is done."""
        return frozenset({NodeStatus.PENDING, NodeStatus.STALE, NodeStatus.FAILED})

    @staticmethod
    def generated_statuses() -> frozenset[NodeStatus]:
        """Statuses that represent an output a downstream asset could have consumed."""
        return frozenset(
            {
                NodeStatus.DONE,
                NodeStatus.RUNNING,
                NodeStatus.AWAITING_USER,
                NodeStatus.FAILED,
                NodeStatus.STALE,
            }
        )

    @staticmethod
    def staleable_statuses() -> frozenset[NodeStatus]:
        """Statuses propagation moves to stale. Already-stale is left as it is."""
        return frozenset(
            {
                NodeStatus.DONE,
                NodeStatus.RUNNING,
                NodeStatus.AWAITING_USER,
                NodeStatus.FAILED,
            }
        )


_ALLOWED: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.RUNNING, NodeStatus.SKIPPED}),
    NodeStatus.RUNNING: frozenset(
        {
            NodeStatus.DONE,
            NodeStatus.FAILED,
            NodeStatus.AWAITING_USER,
            NodeStatus.PENDING,
            NodeStatus.STALE,
        }
    ),
    NodeStatus.AWAITING_USER: frozenset(
        {
            NodeStatus.DONE,
            NodeStatus.FAILED,
            NodeStatus.RUNNING,
            NodeStatus.PENDING,
            NodeStatus.STALE,
        }
    ),
    NodeStatus.DONE: frozenset({NodeStatus.STALE, NodeStatus.PENDING}),
    NodeStatus.STALE: frozenset({NodeStatus.RUNNING, NodeStatus.PENDING, NodeStatus.SKIPPED}),
    NodeStatus.FAILED: frozenset({NodeStatus.RUNNING, NodeStatus.PENDING, NodeStatus.STALE}),
    NodeStatus.SKIPPED: frozenset({NodeStatus.PENDING}),
}


class EdgeKind(str, Enum):
    """Why a successor depends on a predecessor. Every kind can block execution."""

    HARD = "hard"
    REF = "ref"
    CONTINUITY = "continuity"
    INHERIT = "inherit"

    @classmethod
    def parse(cls, value: EdgeKind | str) -> EdgeKind:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        known = ", ".join(item.value for item in cls)
        raise GraphError(f"unknown edge kind: {value!r}; expected one of: {known}")


class Identifiers:
    """Shared id checks for nodes, episodes, and edges."""

    @staticmethod
    def token(value: object, *, what: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GraphError(f"{what} id must be a non-empty string")
        return value


@dataclass
class AssetNode:
    """One asset. Status changes go through the graph so transitions stay valid."""

    id: str
    kind: str
    episode_id: str | None = None
    status: NodeStatus = NodeStatus.PENDING
    version: int = 0
    input_versions: dict[str, int] = field(default_factory=dict)
    pinned: bool = False
    label: str = ""
    metadata: dict = field(default_factory=dict)

    def copy(self) -> AssetNode:
        return replace(
            self,
            input_versions=dict(self.input_versions),
            metadata=copy.deepcopy(self.metadata),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "episode_id": self.episode_id,
            "status": self.status.value,
            "version": self.version,
            "input_versions": dict(self.input_versions),
            "pinned": self.pinned,
            "label": self.label,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: object) -> AssetNode:
        if not isinstance(data, dict):
            raise GraphError("node must be an object")
        node_id = Identifiers.token(data.get("id"), what="node")
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise GraphError(f"node {node_id!r} kind must be a non-empty string")
        episode_id = data.get("episode_id")
        if episode_id is not None:
            episode_id = Identifiers.token(episode_id, what="episode")
        status = NodeStatus.PENDING
        if "status" in data and data["status"] is not None:
            status = NodeStatus.parse(data["status"])
        version = data.get("version", 0)
        if not JsonValue.is_int(version) or version < 0:
            raise GraphError(f"node {node_id!r} version must be a non-negative integer")
        raw_inputs = data.get("input_versions", {})
        if not isinstance(raw_inputs, dict):
            raise GraphError(f"node {node_id!r} input_versions must be an object")
        input_versions: dict[str, int] = {}
        for key, recorded in raw_inputs.items():
            if not isinstance(key, str) or not key.strip():
                raise GraphError(f"node {node_id!r} input_versions keys must be node ids")
            if not JsonValue.is_int(recorded) or recorded < 0:
                raise GraphError(f"node {node_id!r} input version for {key!r} is invalid")
            input_versions[key] = recorded
        pinned = data.get("pinned", False)
        if not isinstance(pinned, bool):
            raise GraphError(f"node {node_id!r} pinned must be a boolean")
        label = data.get("label", "")
        if not isinstance(label, str):
            raise GraphError(f"node {node_id!r} label must be a string")
        metadata = JsonValue.object_copy(data.get("metadata", {}), path=f"node {node_id!r} metadata")
        return cls(
            id=node_id,
            kind=kind,
            episode_id=episode_id,
            status=status,
            version=int(version),
            input_versions=input_versions,
            pinned=pinned,
            label=label,
            metadata=metadata,
        )


@dataclass(frozen=True)
class AssetEdge:
    """Directed dependency: `predecessor` must be done before `successor` when `blocks`."""

    predecessor: str
    successor: str
    kind: EdgeKind = EdgeKind.HARD
    blocks: bool = True

    def to_dict(self) -> dict:
        return {
            "predecessor": self.predecessor,
            "successor": self.successor,
            "kind": self.kind.value,
            "blocks": self.blocks,
        }

    @classmethod
    def from_dict(cls, data: object) -> AssetEdge:
        if not isinstance(data, dict):
            raise GraphError("edge must be an object")
        predecessor = Identifiers.token(data.get("predecessor"), what="predecessor")
        successor = Identifiers.token(data.get("successor"), what="successor")
        kind = EdgeKind.HARD
        if "kind" in data and data["kind"] is not None:
            kind = EdgeKind.parse(data["kind"])
        blocks = data.get("blocks", True)
        if not isinstance(blocks, bool):
            raise GraphError(
                f"edge {predecessor!r} -> {successor!r} blocks must be a boolean"
            )
        return cls(
            predecessor=predecessor,
            successor=successor,
            kind=kind,
            blocks=blocks,
        )


@dataclass(frozen=True)
class Finding:
    """One structural problem. `code` is stable for callers."""

    code: str
    message: str
    ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "ids": list(self.ids)}


@dataclass
class StructureReport:
    """Dangling edges, duplicate ids, and cycles. Empty means the structure is valid."""

    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, message: str, ids: list[str] | None = None) -> None:
        self.findings.append(Finding(code, message, tuple(ids or ())))

    @property
    def ok(self) -> bool:
        return not self.findings

    def codes(self) -> list[str]:
        return [finding.code for finding in self.findings]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "findings": [finding.to_dict() for finding in self.findings]}

    def __str__(self) -> str:
        if self.ok:
            return "graph structure is valid"
        return "; ".join(finding.message for finding in self.findings)


@dataclass(frozen=True)
class AffectedNode:
    """One downstream asset a later cost estimate may price. This object has no price."""

    id: str
    kind: str
    episode_id: str | None
    status: str
    pinned: bool
    version: int
    would_stale: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "episode_id": self.episode_id,
            "status": self.status,
            "pinned": self.pinned,
            "version": self.version,
            "would_stale": self.would_stale,
        }


@dataclass(frozen=True)
class Impact:
    """Downstream assets affected by changing `source_id`. Prices are supplied by the caller."""

    source_id: str
    affected: tuple[AffectedNode, ...]

    def to_dict(self) -> dict:
        nodes = [item.to_dict() for item in self.affected]
        return {"source_id": self.source_id, "affected": nodes, "count": len(nodes)}

    def with_estimates(self, estimator: Callable[[Mapping[str, Any]], Any]) -> dict:
        """Attach caller-supplied estimates. The graph does not price anything."""
        data = self.to_dict()
        data["estimates"] = [
            {"id": item["id"], "estimate": estimator(item)} for item in data["affected"]
        ]
        return data
