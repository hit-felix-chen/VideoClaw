"""CRUD stubs for the Asset → Look → Image chain.

R5 owns inheritance, cycle detection, follow-parent updates, and rollback.
These methods insert and read the rows that chain needs. Creating or
updating a look also writes a ``look_version`` row so version history has
a place to grow. Image rows are immutable records: a new picture is a new
row, and :meth:`LookStore.select_image` records which one is current.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from storage.blobs import BlobStore
from storage.database import (
    AssetNotFoundError,
    ImageNotFoundError,
    LookNotFoundError,
    ProjectNotFoundError,
    StorageError,
    dump_json,
    load_json,
    new_id,
    optional_amount,
    require_id,
    require_name,
    row_dict,
    transaction,
    utc_now,
)
from storage.schema import Schema

_UNSET = object()


def _tags(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StorageError("tags must be a list of strings")
    return dump_json(value)


def _attributes(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StorageError("attributes must be a JSON object")
    return dump_json(value)


def _load_tags(payload: str | None) -> list[str] | None:
    if payload is None:
        return None
    loaded = load_json(payload)
    if not isinstance(loaded, list):
        raise StorageError("stored tags are not a list")
    return loaded


def _load_attributes(payload: str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    loaded = load_json(payload)
    if not isinstance(loaded, dict):
        raise StorageError("stored attributes are not an object")
    return loaded


def _asset_record(row: sqlite3.Row) -> dict[str, Any]:
    record = row_dict(row)
    record["tags"] = _load_tags(record.pop("tags_json"))
    return record


def _look_record(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    record = row_dict(row)
    record["attributes"] = _load_attributes(record.pop("attributes_json"))
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM look_version WHERE look_id = ?",
        (record["id"],),
    ).fetchone()[0]
    record["version"] = int(version)
    return record


def _image_record(row: sqlite3.Row) -> dict[str, Any]:
    record = row_dict(row)
    if record["cost"] is not None:
        record["cost"] = float(record["cost"])
    return record


class AssetStore:
    """Project-scoped and global assets. Behavior beyond CRUD belongs to R5."""

    KINDS = Schema.ASSET_KINDS
    SCOPES = Schema.ASSET_SCOPES

    @staticmethod
    def create(
        conn: sqlite3.Connection,
        *,
        name: str,
        kind: str,
        scope: str = "project",
        project_id: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        episode_id: str | None = None,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        name = require_name(name)
        if kind not in Schema.ASSET_KINDS:
            raise StorageError(f"asset kind must be one of {Schema.ASSET_KINDS}")
        if scope not in Schema.ASSET_SCOPES:
            raise StorageError(f"asset scope must be one of {Schema.ASSET_SCOPES}")
        if scope == "global":
            if project_id is not None:
                raise StorageError("a global asset has no project_id")
        else:
            project_id = require_id(project_id or "", "project_id")
            found = conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone()
            if found is None:
                raise ProjectNotFoundError(project_id)
        if episode_id is not None:
            episode_id = require_id(episode_id, "episode_id")
        if description is not None:
            description = require_name(description, "description")
        asset_id = new_id() if asset_id is None else require_id(asset_id, "asset_id")
        created_at = utc_now()
        try:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO asset (
                        id, project_id, scope, kind, name, description, tags_json,
                        episode_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        project_id,
                        scope,
                        kind,
                        name,
                        description,
                        _tags(tags),
                        episode_id,
                        created_at,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not create asset {asset_id}") from exc
        return AssetStore.get(conn, asset_id)

    @staticmethod
    def get(conn: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
        asset_id = require_id(asset_id, "asset_id")
        row = conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise AssetNotFoundError(asset_id)
        return _asset_record(row)

    @staticmethod
    def list(
        conn: sqlite3.Connection,
        *,
        project_id: str | None = None,
        scope: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(require_id(project_id, "project_id"))
        if scope is not None:
            if scope not in Schema.ASSET_SCOPES:
                raise StorageError(f"asset scope must be one of {Schema.ASSET_SCOPES}")
            clauses.append("scope = ?")
            values.append(scope)
        if kind is not None:
            if kind not in Schema.ASSET_KINDS:
                raise StorageError(f"asset kind must be one of {Schema.ASSET_KINDS}")
            clauses.append("kind = ?")
            values.append(kind)
        sql = "SELECT * FROM asset"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, id ASC"
        return [_asset_record(row) for row in conn.execute(sql, values)]

    @staticmethod
    def update(
        conn: sqlite3.Connection,
        asset_id: str,
        *,
        name: Any = _UNSET,
        description: Any = _UNSET,
        tags: Any = _UNSET,
        episode_id: Any = _UNSET,
    ) -> dict[str, Any]:
        AssetStore.get(conn, asset_id)
        assignments: list[str] = []
        values: list[Any] = []
        if name is not _UNSET:
            assignments.append("name = ?")
            values.append(require_name(name))
        if description is not _UNSET:
            assignments.append("description = ?")
            values.append(None if description is None else require_name(description, "description"))
        if tags is not _UNSET:
            assignments.append("tags_json = ?")
            values.append(_tags(tags))
        if episode_id is not _UNSET:
            assignments.append("episode_id = ?")
            values.append(None if episode_id is None else require_id(episode_id, "episode_id"))
        if not assignments:
            raise StorageError("no asset fields to update")
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(require_id(asset_id, "asset_id"))
        try:
            with transaction(conn):
                conn.execute(
                    f"UPDATE asset SET {', '.join(assignments)} WHERE id = ?",
                    values,
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not update asset {asset_id}") from exc
        return AssetStore.get(conn, asset_id)

    @staticmethod
    def delete(conn: sqlite3.Connection, asset_id: str) -> bool:
        asset_id = require_id(asset_id, "asset_id")
        with transaction(conn):
            exists = conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone()
            if exists is None:
                return False
            hashes = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT blob_hash FROM image
                    WHERE blob_hash IS NOT NULL
                      AND (
                        asset_id = ?
                        OR look_id IN (SELECT id FROM look WHERE asset_id = ?)
                      )
                    """,
                    (asset_id, asset_id),
                )
            ]
            conn.execute("DELETE FROM asset WHERE id = ?", (asset_id,))
            BlobStore.release_many(conn, hashes)
        return True


class LookStore:
    """Looks under an asset, plus an append-only version row per write."""

    @staticmethod
    def create(
        conn: sqlite3.Connection,
        *,
        asset_id: str,
        name: str,
        parent_look_id: str | None = None,
        description: str | None = None,
        prompt: str | None = None,
        attributes: dict[str, Any] | None = None,
        look_id: str | None = None,
    ) -> dict[str, Any]:
        asset_id = require_id(asset_id, "asset_id")
        name = require_name(name)
        if description is not None:
            description = require_name(description, "description")
        if prompt is not None:
            prompt = require_name(prompt, "prompt")
        if parent_look_id is not None:
            parent_look_id = require_id(parent_look_id, "parent_look_id")
        look_id = new_id() if look_id is None else require_id(look_id, "look_id")
        if parent_look_id == look_id:
            raise StorageError("look cannot parent itself")
        attributes_json = _attributes(attributes)
        created_at = utc_now()
        if conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone() is None:
            raise AssetNotFoundError(asset_id)
        if parent_look_id is not None and conn.execute(
            "SELECT 1 FROM look WHERE id = ?",
            (parent_look_id,),
        ).fetchone() is None:
            raise LookNotFoundError(parent_look_id)
        try:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO look (
                        id, asset_id, name, parent_look_id, description, prompt,
                        attributes_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        look_id,
                        asset_id,
                        name,
                        parent_look_id,
                        description,
                        prompt,
                        attributes_json,
                        created_at,
                        created_at,
                    ),
                )
                _insert_version(
                    conn,
                    look_id=look_id,
                    version=1,
                    parent_look_id=parent_look_id,
                    name=name,
                    description=description,
                    prompt=prompt,
                    attributes_json=attributes_json,
                    reason="create",
                    created_at=created_at,
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not create look {look_id}") from exc
        return LookStore.get(conn, look_id)

    @staticmethod
    def get(conn: sqlite3.Connection, look_id: str) -> dict[str, Any]:
        look_id = require_id(look_id, "look_id")
        row = conn.execute("SELECT * FROM look WHERE id = ?", (look_id,)).fetchone()
        if row is None:
            raise LookNotFoundError(look_id)
        return _look_record(conn, row)

    @staticmethod
    def list(conn: sqlite3.Connection, asset_id: str) -> list[dict[str, Any]]:
        asset_id = require_id(asset_id, "asset_id")
        rows = conn.execute(
            "SELECT * FROM look WHERE asset_id = ? ORDER BY created_at ASC, id ASC",
            (asset_id,),
        ).fetchall()
        return [_look_record(conn, row) for row in rows]

    @staticmethod
    def update(
        conn: sqlite3.Connection,
        look_id: str,
        *,
        name: Any = _UNSET,
        parent_look_id: Any = _UNSET,
        description: Any = _UNSET,
        prompt: Any = _UNSET,
        attributes: Any = _UNSET,
        reason: str = "edit",
    ) -> dict[str, Any]:
        """Write the live row and append a ``look_version``. R5 fills in follow-parent."""
        current = LookStore.get(conn, look_id)
        look_id = current["id"]
        new_name = current["name"] if name is _UNSET else require_name(name)
        if parent_look_id is _UNSET:
            new_parent = current["parent_look_id"]
        elif parent_look_id is None:
            new_parent = None
        else:
            new_parent = require_id(parent_look_id, "parent_look_id")
        if new_parent == look_id:
            raise StorageError("look cannot parent itself")
        if description is _UNSET:
            new_description = current["description"]
        elif description is None:
            new_description = None
        else:
            new_description = require_name(description, "description")
        if prompt is _UNSET:
            new_prompt = current["prompt"]
        elif prompt is None:
            new_prompt = None
        else:
            new_prompt = require_name(prompt, "prompt")
        if attributes is _UNSET:
            new_attributes = current["attributes"]
        else:
            new_attributes = attributes
        attributes_json = _attributes(new_attributes)
        unchanged = (
            new_name == current["name"]
            and new_parent == current["parent_look_id"]
            and new_description == current["description"]
            and new_prompt == current["prompt"]
            and new_attributes == current["attributes"]
        )
        if unchanged:
            return current
        if new_parent is not None and conn.execute(
            "SELECT 1 FROM look WHERE id = ?",
            (new_parent,),
        ).fetchone() is None:
            raise LookNotFoundError(new_parent)
        reason = require_name(reason, "reason")
        created_at = utc_now()
        next_version = int(current["version"]) + 1
        try:
            with transaction(conn):
                conn.execute(
                    """
                    UPDATE look
                    SET name = ?, parent_look_id = ?, description = ?, prompt = ?,
                        attributes_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_name,
                        new_parent,
                        new_description,
                        new_prompt,
                        attributes_json,
                        created_at,
                        look_id,
                    ),
                )
                _insert_version(
                    conn,
                    look_id=look_id,
                    version=next_version,
                    parent_look_id=new_parent,
                    name=new_name,
                    description=new_description,
                    prompt=new_prompt,
                    attributes_json=attributes_json,
                    reason=reason,
                    created_at=created_at,
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not update look {look_id}") from exc
        return LookStore.get(conn, look_id)

    @staticmethod
    def list_versions(conn: sqlite3.Connection, look_id: str) -> list[dict[str, Any]]:
        LookStore.get(conn, look_id)
        rows = conn.execute(
            """
            SELECT * FROM look_version
            WHERE look_id = ?
            ORDER BY version DESC
            """,
            (require_id(look_id, "look_id"),),
        ).fetchall()
        versions = []
        for row in rows:
            record = row_dict(row)
            record["attributes"] = _load_attributes(record.pop("attributes_json"))
            record["version"] = int(record["version"])
            versions.append(record)
        return versions

    @staticmethod
    def select_image(conn: sqlite3.Connection, look_id: str, image_id: str) -> dict[str, Any]:
        """Point ``look_selection`` at one image. R5 decides when a change is stale."""
        look = LookStore.get(conn, look_id)
        image = ImageStore.get(conn, image_id)
        if image["look_id"] not in (None, look["id"]):
            raise StorageError("image belongs to a different look")
        if image["asset_id"] not in (None, look["asset_id"]):
            raise StorageError("image belongs to a different asset")
        selected_at = utc_now()
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO look_selection (look_id, image_id, selected_at)
                VALUES (?, ?, ?)
                ON CONFLICT(look_id) DO UPDATE SET
                    image_id = excluded.image_id,
                    selected_at = excluded.selected_at
                """,
                (look["id"], image["id"], selected_at),
            )
            row = conn.execute(
                "SELECT * FROM look_selection WHERE look_id = ?",
                (look["id"],),
            ).fetchone()
        return row_dict(row)

    @staticmethod
    def delete(conn: sqlite3.Connection, look_id: str) -> bool:
        look_id = require_id(look_id, "look_id")
        with transaction(conn):
            exists = conn.execute("SELECT 1 FROM look WHERE id = ?", (look_id,)).fetchone()
            if exists is None:
                return False
            hashes = [
                row[0]
                for row in conn.execute(
                    "SELECT blob_hash FROM image WHERE look_id = ? AND blob_hash IS NOT NULL",
                    (look_id,),
                )
            ]
            conn.execute("DELETE FROM look WHERE id = ?", (look_id,))
            BlobStore.release_many(conn, hashes)
        return True


class ImageStore:
    """Image records. The bytes themselves live in :class:`BlobStore`."""

    SOURCES = Schema.IMAGE_SOURCES

    @staticmethod
    def create(
        conn: sqlite3.Connection,
        *,
        source: str,
        asset_id: str | None = None,
        look_id: str | None = None,
        blob_hash: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        cost: float | None = None,
        width: int | None = None,
        height: int | None = None,
        image_id: str | None = None,
    ) -> dict[str, Any]:
        if source not in Schema.IMAGE_SOURCES:
            raise StorageError(f"image source must be one of {Schema.IMAGE_SOURCES}")
        if asset_id is None and look_id is None:
            raise StorageError("image requires an asset_id or a look_id")
        if asset_id is not None:
            asset_id = require_id(asset_id, "asset_id")
            if conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone() is None:
                raise AssetNotFoundError(asset_id)
        if look_id is not None:
            look_id = require_id(look_id, "look_id")
            if conn.execute("SELECT 1 FROM look WHERE id = ?", (look_id,)).fetchone() is None:
                raise LookNotFoundError(look_id)
        if blob_hash is not None:
            BlobStore.info(conn, blob_hash)
        if prompt is not None:
            prompt = require_name(prompt, "prompt")
        if model is not None:
            model = require_name(model, "model")
        cost = optional_amount(cost, "cost")
        width = _optional_pixel(width, "width")
        height = _optional_pixel(height, "height")
        image_id = new_id() if image_id is None else require_id(image_id, "image_id")
        try:
            with transaction(conn):
                if blob_hash is not None:
                    BlobStore.retain(conn, blob_hash)
                conn.execute(
                    """
                    INSERT INTO image (
                        id, asset_id, look_id, blob_hash, source, model, prompt,
                        cost, width, height, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        image_id,
                        asset_id,
                        look_id,
                        blob_hash,
                        source,
                        model,
                        prompt,
                        cost,
                        width,
                        height,
                        utc_now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not create image {image_id}") from exc
        return ImageStore.get(conn, image_id)

    @staticmethod
    def get(conn: sqlite3.Connection, image_id: str) -> dict[str, Any]:
        image_id = require_id(image_id, "image_id")
        row = conn.execute("SELECT * FROM image WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise ImageNotFoundError(image_id)
        return _image_record(row)

    @staticmethod
    def list(
        conn: sqlite3.Connection,
        *,
        asset_id: str | None = None,
        look_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if asset_id is not None:
            clauses.append("asset_id = ?")
            values.append(require_id(asset_id, "asset_id"))
        if look_id is not None:
            clauses.append("look_id = ?")
            values.append(require_id(look_id, "look_id"))
        sql = "SELECT * FROM image"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, id ASC"
        return [_image_record(row) for row in conn.execute(sql, values)]

    @staticmethod
    def delete(conn: sqlite3.Connection, image_id: str) -> bool:
        image_id = require_id(image_id, "image_id")
        with transaction(conn):
            row = conn.execute(
                "SELECT blob_hash FROM image WHERE id = ?",
                (image_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM image WHERE id = ?", (image_id,))
            if row["blob_hash"]:
                BlobStore.release(conn, row["blob_hash"])
        return True


def _optional_pixel(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StorageError(f"{field} must be a positive integer or null")
    return value


def _insert_version(
    conn: sqlite3.Connection,
    *,
    look_id: str,
    version: int,
    parent_look_id: str | None,
    name: str,
    description: str | None,
    prompt: str | None,
    attributes_json: str | None,
    reason: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO look_version (
            id, look_id, version, parent_look_id, name, description, prompt,
            attributes_json, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id(),
            look_id,
            version,
            parent_look_id,
            name,
            description,
            prompt,
            attributes_json,
            reason,
            created_at,
        ),
    )
