"""Explicit segment → asset → look bindings and ``@资产名/形象名`` lookup.

Name matching is exact. With a project id, a project asset wins over a
global asset of the same name. Other projects are not searched. Two visible
assets with the same name stay ambiguous instead of picking one.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from storage.database import (
    AssetNotFoundError,
    LookNotFoundError,
    ProjectNotFoundError,
    SegmentRefNotFoundError,
    StorageError,
    new_id,
    require_id,
    require_name,
    row_dict,
    transaction,
    utc_now,
)

# Stops at whitespace, another @, a slash, or punctuation that ends a sentence.
_MENTION_RE = re.compile(
    r"@(?P<asset>[^\s@/，。！？、；：:;,.!?（）()\[\]{}<>\"']+)"
    r"(?:/(?P<look>[^\s@/，。！？、；：:;,.!?（）()\[\]{}<>\"']+))?"
)


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return require_name(value, field)


class SegmentRefStore:
    """Bind one segment asset to an asset and, when set, one of that asset's looks."""

    @staticmethod
    def create(
        conn: sqlite3.Connection,
        *,
        segment_asset_id: str,
        asset_id: str,
        look_id: str | None = None,
        role: str | None = None,
        alias: str | None = None,
        ref_id: str | None = None,
    ) -> dict[str, Any]:
        segment_asset_id = require_id(segment_asset_id, "segment_asset_id")
        asset_id = require_id(asset_id, "asset_id")
        if look_id is not None:
            look_id = require_id(look_id, "look_id")
        role = _optional_text(role, "role")
        alias = _optional_text(alias, "alias")
        ref_id = new_id() if ref_id is None else require_id(ref_id, "ref_id")
        segment = conn.execute(
            "SELECT id, kind FROM asset WHERE id = ?",
            (segment_asset_id,),
        ).fetchone()
        if segment is None:
            raise AssetNotFoundError(segment_asset_id)
        if segment["kind"] != "segment":
            raise StorageError("segment_asset_id must be a segment asset")
        if conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone() is None:
            raise AssetNotFoundError(asset_id)
        if look_id is not None:
            look = conn.execute(
                "SELECT asset_id FROM look WHERE id = ?",
                (look_id,),
            ).fetchone()
            if look is None:
                raise LookNotFoundError(look_id)
            if look["asset_id"] != asset_id:
                raise StorageError("look does not belong to the bound asset")
        if _duplicate(conn, segment_asset_id, asset_id, look_id):
            raise StorageError("segment ref already exists for this asset and look")
        created_at = utc_now()
        try:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO segment_ref (
                        id, segment_asset_id, asset_id, look_id, role, alias, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ref_id, segment_asset_id, asset_id, look_id, role, alias, created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not create segment ref {ref_id}") from exc
        return SegmentRefStore.get(conn, ref_id)

    @staticmethod
    def get(conn: sqlite3.Connection, ref_id: str) -> dict[str, Any]:
        ref_id = require_id(ref_id, "ref_id")
        row = conn.execute("SELECT * FROM segment_ref WHERE id = ?", (ref_id,)).fetchone()
        if row is None:
            raise SegmentRefNotFoundError(ref_id)
        return row_dict(row)

    @staticmethod
    def list(conn: sqlite3.Connection, segment_asset_id: str) -> list[dict[str, Any]]:
        segment_asset_id = require_id(segment_asset_id, "segment_asset_id")
        if conn.execute("SELECT 1 FROM asset WHERE id = ?", (segment_asset_id,)).fetchone() is None:
            raise AssetNotFoundError(segment_asset_id)
        rows = conn.execute(
            """
            SELECT * FROM segment_ref
            WHERE segment_asset_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (segment_asset_id,),
        ).fetchall()
        return [row_dict(row) for row in rows]

    @staticmethod
    def delete(conn: sqlite3.Connection, ref_id: str) -> bool:
        ref_id = require_id(ref_id, "ref_id")
        with transaction(conn):
            deleted = conn.execute("DELETE FROM segment_ref WHERE id = ?", (ref_id,))
            return deleted.rowcount > 0

    @staticmethod
    def resolve(
        conn: sqlite3.Connection,
        text: str,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve every ``@资产名`` and ``@资产名/形象名`` in ``text``."""
        if not isinstance(text, str):
            raise StorageError("text must be a string")
        if project_id is not None:
            project_id = require_id(project_id, "project_id")
            if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
                raise ProjectNotFoundError(project_id)
        mentions = []
        for match in _MENTION_RE.finditer(text):
            asset_name = match.group("asset")
            look_name = match.group("look")
            mentions.append(
                _resolve_one(
                    conn,
                    raw=match.group(0),
                    asset_name=asset_name,
                    look_name=look_name,
                    project_id=project_id,
                )
            )
        return {"mentions": mentions}


def _duplicate(
    conn: sqlite3.Connection,
    segment_asset_id: str,
    asset_id: str,
    look_id: str | None,
) -> bool:
    if look_id is None:
        row = conn.execute(
            """
            SELECT 1 FROM segment_ref
            WHERE segment_asset_id = ? AND asset_id = ? AND look_id IS NULL
            """,
            (segment_asset_id, asset_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1 FROM segment_ref
            WHERE segment_asset_id = ? AND asset_id = ? AND look_id = ?
            """,
            (segment_asset_id, asset_id, look_id),
        ).fetchone()
    return row is not None


def _visible_assets(
    conn: sqlite3.Connection,
    name: str,
    project_id: str | None,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT id, name, kind, scope, project_id FROM asset WHERE name = ? ORDER BY created_at ASC, id ASC",
        (name,),
    ).fetchall()
    if project_id is None:
        return list(rows)
    in_project = [row for row in rows if row["project_id"] == project_id]
    if in_project:
        return in_project
    return [row for row in rows if row["scope"] == "global"]


def _resolve_one(
    conn: sqlite3.Connection,
    *,
    raw: str,
    asset_name: str,
    look_name: str | None,
    project_id: str | None,
) -> dict[str, Any]:
    assets = _visible_assets(conn, asset_name, project_id)
    matches: list[dict[str, Any]] = []
    if not assets:
        return {
            "raw": raw,
            "asset_name": asset_name,
            "look_name": look_name,
            "resolved": False,
            "ambiguous": False,
            "error": "asset not found",
            "matches": [],
        }
    for asset in assets:
        if look_name is None:
            matches.append(_match_row(asset, None, None))
            continue
        looks = conn.execute(
            """
            SELECT id, name FROM look
            WHERE asset_id = ? AND name = ?
            ORDER BY created_at ASC, id ASC
            """,
            (asset["id"], look_name),
        ).fetchall()
        for look in looks:
            matches.append(_match_row(asset, look["id"], look["name"]))
    if look_name is not None and not matches:
        return {
            "raw": raw,
            "asset_name": asset_name,
            "look_name": look_name,
            "resolved": False,
            "ambiguous": False,
            "error": "look not found",
            "matches": [],
        }
    ambiguous = len(matches) > 1
    return {
        "raw": raw,
        "asset_name": asset_name,
        "look_name": look_name,
        "resolved": len(matches) == 1,
        "ambiguous": ambiguous,
        "error": "ambiguous" if ambiguous else None,
        "matches": matches,
    }


def _match_row(asset: sqlite3.Row, look_id: str | None, look_name: str | None) -> dict[str, Any]:
    return {
        "asset_id": asset["id"],
        "asset_name": asset["name"],
        "look_id": look_id,
        "look_name": look_name,
        "kind": asset["kind"],
        "scope": asset["scope"],
        "project_id": asset["project_id"],
    }
