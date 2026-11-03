"""Look inheritance: unlimited depth, cycle checks, and resolved fields.

A look owns a field only when ``covered_json`` says so. Description and
prompt are whole fields. Attributes are covered per key. The columns on
``look`` store the resolved look, not just the owned slice: uncovered
description, prompt, and attribute keys are copies of the parent. A parent
edit rewrites those copies and appends a ``follow`` version. Owned fields
are left as they are.

Name is not inherited. There is no depth cap; :func:`assert_parent_acyclic`
only rejects a parent pointer that would loop.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from storage.database import LookCycleError, LookNotFoundError, StorageError, dump_json, load_json


def empty_visual() -> dict[str, Any]:
    return {"description": None, "prompt": None, "attributes": {}}


def covered_from_create(
    description: str | None,
    prompt: str | None,
    attributes: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ownership mask for a new look. Omitted fields follow the parent."""
    attrs = dict(attributes or {})
    covered = {
        "description": description is not None,
        "prompt": prompt is not None,
        "attributes": sorted(attrs),
    }
    own = {"description": description, "prompt": prompt, "attributes": attrs}
    return covered, own


def normalize_covered(covered: dict[str, Any]) -> dict[str, Any]:
    attributes = covered.get("attributes") or []
    if not isinstance(attributes, list) or not all(isinstance(key, str) for key in attributes):
        raise StorageError("covered attributes must be a list of strings")
    return {
        "description": bool(covered.get("description")),
        "prompt": bool(covered.get("prompt")),
        "attributes": sorted(attributes),
    }


def load_covered(
    payload: str | None,
    *,
    description: str | None,
    prompt: str | None,
    attributes: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read a mask. A missing mask (v1 rows) treats each stored value as owned."""
    if payload is None:
        attrs = attributes or {}
        return {
            "description": description is not None,
            "prompt": prompt is not None,
            "attributes": sorted(attrs),
        }
    loaded = load_json(payload)
    if not isinstance(loaded, dict):
        raise StorageError("stored covered mask is not an object")
    return normalize_covered(loaded)


def dump_covered(covered: dict[str, Any]) -> str:
    return dump_json(normalize_covered(covered))


def visual_of(
    description: str | None,
    prompt: str | None,
    attributes: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "description": description,
        "prompt": prompt,
        "attributes": dict(attributes or {}),
    }


def visuals_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["description"] == right["description"]
        and left["prompt"] == right["prompt"]
        and left["attributes"] == right["attributes"]
    )


def own_from_stored(
    description: str | None,
    prompt: str | None,
    attributes: dict[str, Any] | None,
    covered: dict[str, Any],
) -> dict[str, Any]:
    """Pull the owned slice back out of a resolved row."""
    covered = normalize_covered(covered)
    attrs = dict(attributes or {})
    missing = [key for key in covered["attributes"] if key not in attrs]
    if missing:
        raise StorageError("covered attribute is missing from the look: " + ", ".join(missing))
    return {
        "description": description if covered["description"] else None,
        "prompt": prompt if covered["prompt"] else None,
        "attributes": {key: attrs[key] for key in covered["attributes"]},
    }


def materialize(
    covered: dict[str, Any],
    own: dict[str, Any],
    parent_visual: dict[str, Any],
) -> dict[str, Any]:
    """Resolved look. Owned values win; everything else copies the parent."""
    covered = normalize_covered(covered)
    parent_attributes = dict(parent_visual.get("attributes") or {})
    attributes = dict(parent_attributes)
    for key in covered["attributes"]:
        attributes[key] = own["attributes"][key]
    return {
        "description": own["description"] if covered["description"] else parent_visual.get("description"),
        "prompt": own["prompt"] if covered["prompt"] else parent_visual.get("prompt"),
        "attributes": attributes,
    }


def assert_parent_acyclic(
    conn: sqlite3.Connection,
    look_id: str,
    parent_id: str | None,
) -> None:
    """Reject a parent chain that reaches ``look_id``. No maximum depth."""
    if parent_id is None:
        return
    if parent_id == look_id:
        raise LookCycleError(look_id, parent_id, [look_id, parent_id])
    chain = [look_id]
    seen = {look_id}
    current: str | None = parent_id
    while current is not None:
        chain.append(current)
        if current in seen:
            raise LookCycleError(look_id, parent_id, chain)
        seen.add(current)
        row = conn.execute(
            "SELECT parent_look_id FROM look WHERE id = ?",
            (current,),
        ).fetchone()
        if row is None:
            raise LookNotFoundError(current)
        current = row["parent_look_id"]


def resolve_parent(
    conn: sqlite3.Connection,
    parent_id: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """Return the parent id and its resolved visual.

    A missing parent row becomes ``(None, empty)`` so a deleted parent does
    not leave a dangling pointer. Callers that require the parent to exist
    use :func:`assert_parent_acyclic` first.
    """
    if parent_id is None:
        return None, empty_visual()
    row = conn.execute(
        "SELECT id, description, prompt, attributes_json FROM look WHERE id = ?",
        (parent_id,),
    ).fetchone()
    if row is None:
        return None, empty_visual()
    attributes = _load_attributes(row["attributes_json"])
    return row["id"], visual_of(row["description"], row["prompt"], attributes)


def descendant_ids(conn: sqlite3.Connection, look_id: str) -> list[str]:
    """Children, then their children, for as long as the chain runs."""
    ordered: list[str] = []
    seen = {look_id}
    queue = [look_id]
    while queue:
        current = queue.pop(0)
        rows = conn.execute(
            """
            SELECT id FROM look
            WHERE parent_look_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (current,),
        ).fetchall()
        for row in rows:
            child = row["id"]
            if child in seen:
                continue
            seen.add(child)
            ordered.append(child)
            queue.append(child)
    return ordered


def _load_attributes(payload: str | None) -> dict[str, Any]:
    if payload is None:
        return {}
    loaded = load_json(payload)
    if not isinstance(loaded, dict):
        raise StorageError("stored attributes are not an object")
    return loaded
