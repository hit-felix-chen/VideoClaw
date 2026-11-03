"""Project records and the serialized dependency graph."""

from __future__ import annotations

import sqlite3
from typing import Any

from storage.blobs import BlobStore
from storage.database import (
    ProjectNotFoundError,
    StorageError,
    dump_json,
    load_json,
    new_id,
    optional_amount,
    require_id,
    require_name,
    require_status,
    transaction,
    utc_now,
)

_PROJECT_COLUMNS = "id, name, created_at, updated_at, budget_cap, spent, status"

# Sentinel so update() can tell "leave this column" from "set it to null".
class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


def _project_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "budget_cap": None if row["budget_cap"] is None else float(row["budget_cap"]),
        "spent": None if row["spent"] is None else float(row["spent"]),
        "status": row["status"],
    }


def _require_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    project_id = require_id(project_id, "project_id")
    row = conn.execute(
        f"SELECT {_PROJECT_COLUMNS}, graph_json FROM project WHERE id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise ProjectNotFoundError(project_id)
    return row


class ProjectStore:
    """Create, open, list, update, and delete projects."""

    @staticmethod
    def create(
        conn: sqlite3.Connection,
        name: str,
        *,
        budget_cap: float | None = None,
        spent: float | None = None,
        status: str = "active",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert a project. ``budget_cap`` and ``spent`` stay null when omitted."""
        name = require_name(name)
        status = require_status(status)
        budget_cap = optional_amount(budget_cap, "budget_cap")
        spent = optional_amount(spent, "spent")
        project_id = new_id() if project_id is None else require_id(project_id, "project_id")
        created_at = utc_now()
        try:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO project (
                        id, name, created_at, updated_at, budget_cap, spent, status, graph_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (project_id, name, created_at, created_at, budget_cap, spent, status),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"project already exists: {project_id}") from exc
        return ProjectStore.open(conn, project_id)

    @staticmethod
    def open(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        row = _require_project(conn, project_id)
        return _project_from_row(row)

    @staticmethod
    def list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT {_PROJECT_COLUMNS}
            FROM project
            ORDER BY created_at DESC, id ASC
            """
        ).fetchall()
        return [_project_from_row(row) for row in rows]

    @staticmethod
    def update(
        conn: sqlite3.Connection,
        project_id: str,
        *,
        name: str | _Unset = UNSET,
        budget_cap: Any = UNSET,
        spent: Any = UNSET,
        status: str | _Unset = UNSET,
    ) -> dict[str, Any]:
        """Change metadata. Pass ``None`` to clear ``budget_cap`` or ``spent``."""
        _require_project(conn, project_id)
        assignments: list[str] = []
        values: list[Any] = []
        if name is not UNSET:
            assignments.append("name = ?")
            values.append(require_name(name))
        if budget_cap is not UNSET:
            assignments.append("budget_cap = ?")
            values.append(optional_amount(budget_cap, "budget_cap"))
        if spent is not UNSET:
            assignments.append("spent = ?")
            values.append(optional_amount(spent, "spent"))
        if status is not UNSET:
            assignments.append("status = ?")
            values.append(require_status(status))
        if not assignments:
            raise StorageError("no project fields to update")
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(require_id(project_id, "project_id"))
        with transaction(conn):
            conn.execute(
                f"UPDATE project SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        return ProjectStore.open(conn, project_id)

    @staticmethod
    def delete(conn: sqlite3.Connection, project_id: str) -> bool:
        """Delete the project row and rows that cascade from it.

        Blob files referenced only by this project's images are released.
        A blob still held by another project, or by an outstanding ``put``,
        stays on disk. The data directory and any sibling folders stay.
        """
        project_id = require_id(project_id, "project_id")
        with transaction(conn):
            exists = conn.execute(
                "SELECT 1 FROM project WHERE id = ?",
                (project_id,),
            ).fetchone()
            if exists is None:
                return False
            hashes = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT i.blob_hash
                    FROM image AS i
                    WHERE i.blob_hash IS NOT NULL
                      AND (
                        i.asset_id IN (SELECT id FROM asset WHERE project_id = ?)
                        OR i.look_id IN (
                            SELECT l.id
                            FROM look AS l
                            JOIN asset AS a ON a.id = l.asset_id
                            WHERE a.project_id = ?
                        )
                      )
                    """,
                    (project_id, project_id),
                )
            ]
            conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
            BlobStore.release_many(conn, hashes)
        return True


class GraphStore:
    """Persist one R6 ``to_dict()`` document per project."""

    @staticmethod
    def save(conn: sqlite3.Connection, project_id: str, graph: dict[str, Any]) -> dict[str, Any]:
        """Replace the project's graph JSON. ``graph`` is a JSON object."""
        if not isinstance(graph, dict):
            raise StorageError("graph must be a JSON object")
        payload = dump_json(graph)
        loaded = load_json(payload)
        if not isinstance(loaded, dict):
            raise StorageError("graph must be a JSON object")
        _require_project(conn, project_id)
        project_id = require_id(project_id, "project_id")
        with transaction(conn):
            updated = conn.execute(
                "UPDATE project SET graph_json = ?, updated_at = ? WHERE id = ?",
                (payload, utc_now(), project_id),
            )
            if updated.rowcount == 0:
                raise ProjectNotFoundError(project_id)
        restored = GraphStore.load(conn, project_id)
        if restored is None:
            raise StorageError("graph was not stored")
        return restored

    @staticmethod
    def load(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
        """Return the stored object, or None when this project has no graph yet."""
        row = _require_project(conn, project_id)
        payload = row["graph_json"]
        if payload is None:
            return None
        graph = load_json(payload)
        if not isinstance(graph, dict):
            raise StorageError("stored graph is not a JSON object")
        return graph
