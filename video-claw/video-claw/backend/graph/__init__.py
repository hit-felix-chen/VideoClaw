# -*- coding: utf-8 -*-
"""In-memory asset dependency graph.

Nodes are assets. An edge points from the asset that must exist to the asset
that depends on it. Episodes group assets; they are not nodes. The snapshot
from `to_dict` is JSON-serializable so a later store can persist it.
"""

from graph.dependency_graph import DependencyGraph
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
    AssetKind,
    AssetNode,
    EdgeKind,
    Finding,
    Impact,
    NodeStatus,
    StructureReport,
)

__all__ = [
    "AffectedNode",
    "AssetEdge",
    "AssetKind",
    "AssetNode",
    "CycleError",
    "DependencyGraph",
    "DuplicateIdError",
    "EdgeKind",
    "EdgeNotFoundError",
    "EpisodeNotFoundError",
    "Finding",
    "GraphError",
    "Impact",
    "InvalidStructureError",
    "InvalidTransitionError",
    "NodeNotFoundError",
    "NodeStatus",
    "NotReadyError",
    "StructureReport",
]
