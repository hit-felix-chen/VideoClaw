"""Asset → Look → Image records, with inheritable looks.

Creating or updating a look appends a ``look_version``. Image rows are the
versions: each picture is its own row (source, model, prompt, cost), never a
``{asset_id}*`` filename glob. :meth:`LookStore.select_image` records which
image is current.

Resolved description, prompt, and attributes live on the look row.
``covered_json`` says which of those the look owns. Uncovered fields copy
the parent and are rewritten, with a ``follow`` version, when the parent
changes. Rollback restores a historical snapshot and then refreshes
descendants. Setting ``parent_look_id`` rejects cycles and does not cap depth.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from storage.blobs import BlobStore
from storage.database import (
    AssetNotFoundError,
    ImageNotFoundError,
    LookNotFoundError,
    LookVersionNotFoundError,
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
from storage.inheritance import (
    assert_parent_acyclic,
    covered_from_create,
    descendant_ids,
    dump_covered,
    load_covered,
    materialize,
    normalize_covered,
    own_from_stored,
    resolve_parent,
    visual_of,
    visuals_equal,
)
from storage.schema import Schema
from storage.staleness import mark_look_outputs_stale, mark_segments_stale

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
    record["attributes"] = _load_attributes(record.pop("attributes_json")) or {}
    record["covered"] = load_covered(
        record.pop("covered_json"),
        description=record["description"],
        prompt=record["prompt"],
        attributes=record["attributes"],
    )
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
    def promote(conn: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
        """Move a project asset to the global library. Looks and images stay."""
        current = AssetStore.get(conn, asset_id)
        if current["scope"] == "global":
            raise StorageError("asset is already global")
        with transaction(conn):
            conn.execute(
                """
                UPDATE asset
                SET scope = 'global', project_id = NULL, episode_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), current["id"]),
            )
        return AssetStore.get(conn, current["id"])

    @staticmethod
    def search(
        conn: sqlite3.Connection,
        *,
        tags: list[str],
        project_id: str | None = None,
        include_global: bool = True,
        kind: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Assets that carry every requested tag. Tag match is exact."""
        if not isinstance(tags, list) or not tags:
            raise StorageError("tags must be a non-empty list of strings")
        wanted: list[str] = []
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip() or "\x00" in tag:
                raise StorageError("tag must be a non-empty string")
            wanted.append(tag.strip())
        if kind is not None and kind not in Schema.ASSET_KINDS:
            raise StorageError(f"asset kind must be one of {Schema.ASSET_KINDS}")
        if scope is not None and scope not in Schema.ASSET_SCOPES:
            raise StorageError(f"asset scope must be one of {Schema.ASSET_SCOPES}")
        clauses: list[str] = []
        values: list[Any] = []
        if project_id is not None:
            project_id = require_id(project_id, "project_id")
            if include_global:
                clauses.append("(project_id = ? OR scope = 'global')")
                values.append(project_id)
            else:
                clauses.append("project_id = ?")
                values.append(project_id)
        if scope is not None:
            clauses.append("scope = ?")
            values.append(scope)
        if kind is not None:
            clauses.append("kind = ?")
            values.append(kind)
        sql = "SELECT * FROM asset"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, id ASC"
        found = []
        needed = set(wanted)
        for row in conn.execute(sql, values):
            record = _asset_record(row)
            have = set(record["tags"] or [])
            if needed <= have:
                found.append(record)
        return found

    @staticmethod
    def delete(conn: sqlite3.Connection, asset_id: str) -> bool:
        asset_id = require_id(asset_id, "asset_id")
        with transaction(conn):
            exists = conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone()
            if exists is None:
                return False
            external_roots = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT id FROM look
                    WHERE parent_look_id IN (SELECT id FROM look WHERE asset_id = ?)
                      AND asset_id != ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (asset_id, asset_id),
                )
            ]
            segment_ids = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT DISTINCT segment_asset_id FROM segment_ref
                    WHERE asset_id = ?
                       OR look_id IN (SELECT id FROM look WHERE asset_id = ?)
                    """,
                    (asset_id, asset_id),
                )
                if row[0] != asset_id
            ]
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
            _refresh_many(conn, external_roots)
            mark_segments_stale(conn, segment_ids, reason="asset deleted")
        return True


class LookStore:
    """Looks under an asset. Each write appends a version; children follow."""

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
        attribute_map = _attribute_dict(attributes)
        if parent_look_id is not None:
            parent_look_id = require_id(parent_look_id, "parent_look_id")
        look_id = new_id() if look_id is None else require_id(look_id, "look_id")
        if conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone() is None:
            raise AssetNotFoundError(asset_id)
        assert_parent_acyclic(conn, look_id, parent_look_id)
        parent_id, parent_visual = resolve_parent(conn, parent_look_id)
        covered, own = covered_from_create(description, prompt, attribute_map)
        visual = materialize(covered, own, parent_visual)
        created_at = utc_now()
        try:
            with transaction(conn):
                _insert_look_row(
                    conn,
                    look_id=look_id,
                    asset_id=asset_id,
                    name=name,
                    parent_look_id=parent_id,
                    visual=visual,
                    covered=covered,
                    created_at=created_at,
                )
                _insert_version(
                    conn,
                    look_id=look_id,
                    version=1,
                    parent_look_id=parent_id,
                    name=name,
                    visual=visual,
                    covered=covered,
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
        """Edit owned fields, append a version, and let uncovered descendants follow.

        Pass ``description=None``, ``prompt=None``, or ``attributes=None`` to
        uncover that field so the next parent value is copied in. Passing a
        dict for ``attributes`` replaces the owned attribute keys.
        """
        current = LookStore.get(conn, look_id)
        look_id = current["id"]
        new_name = current["name"] if name is _UNSET else require_name(name)
        if parent_look_id is _UNSET:
            new_parent = current["parent_look_id"]
        elif parent_look_id is None:
            new_parent = None
        else:
            new_parent = require_id(parent_look_id, "parent_look_id")
        covered = normalize_covered(current["covered"])
        own = own_from_stored(
            current["description"],
            current["prompt"],
            current["attributes"],
            covered,
        )
        if description is not _UNSET:
            if description is None:
                covered["description"] = False
                own["description"] = None
            else:
                covered["description"] = True
                own["description"] = require_name(description, "description")
        if prompt is not _UNSET:
            if prompt is None:
                covered["prompt"] = False
                own["prompt"] = None
            else:
                covered["prompt"] = True
                own["prompt"] = require_name(prompt, "prompt")
        if attributes is not _UNSET:
            if attributes is None:
                covered["attributes"] = []
                own["attributes"] = {}
            else:
                attribute_map = _attribute_dict(attributes) or {}
                covered["attributes"] = sorted(attribute_map)
                own["attributes"] = attribute_map
        assert_parent_acyclic(conn, look_id, new_parent)
        parent_id, parent_visual = resolve_parent(conn, new_parent)
        visual = materialize(covered, own, parent_visual)
        covered = normalize_covered(covered)
        unchanged = (
            new_name == current["name"]
            and parent_id == current["parent_look_id"]
            and visuals_equal(visual, visual_of(
                current["description"], current["prompt"], current["attributes"]
            ))
            and covered == normalize_covered(current["covered"])
        )
        if unchanged:
            return current
        reason = require_name(reason, "reason")
        old_visual = visual_of(current["description"], current["prompt"], current["attributes"])
        try:
            with transaction(conn):
                _write_look_state(
                    conn,
                    look_id=look_id,
                    name=new_name,
                    parent_look_id=parent_id,
                    visual=visual,
                    covered=covered,
                    reason=reason,
                    version=int(current["version"]) + 1,
                )
                _propagate_descendants(
                    conn,
                    look_id,
                    origin_visual_changed=not visuals_equal(visual, old_visual),
                    reason=reason,
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not update look {look_id}") from exc
        return LookStore.get(conn, look_id)

    @staticmethod
    def rollback(conn: sqlite3.Connection, look_id: str, version: int) -> dict[str, Any]:
        """Restore a historical snapshot, then refresh descendants from it.

        The restored row is itself a new version (``reason='rollback'``).
        Uncovered fields stay uncovered, so a later parent edit can follow
        again. This does not re-copy the current parent over the snapshot.
        """
        current = LookStore.get(conn, look_id)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StorageError("version must be a positive integer")
        row = conn.execute(
            "SELECT * FROM look_version WHERE look_id = ? AND version = ?",
            (current["id"], version),
        ).fetchone()
        if row is None:
            raise LookVersionNotFoundError(current["id"], version)
        snapshot = _version_record(row)
        assert_parent_acyclic(conn, current["id"], snapshot["parent_look_id"])
        visual = visual_of(snapshot["description"], snapshot["prompt"], snapshot["attributes"])
        covered = normalize_covered(snapshot["covered"])
        unchanged = (
            snapshot["name"] == current["name"]
            and snapshot["parent_look_id"] == current["parent_look_id"]
            and visuals_equal(
                visual,
                visual_of(current["description"], current["prompt"], current["attributes"]),
            )
            and covered == normalize_covered(current["covered"])
        )
        if unchanged:
            return current
        old_visual = visual_of(current["description"], current["prompt"], current["attributes"])
        try:
            with transaction(conn):
                _write_look_state(
                    conn,
                    look_id=current["id"],
                    name=snapshot["name"],
                    parent_look_id=snapshot["parent_look_id"],
                    visual=visual,
                    covered=covered,
                    reason="rollback",
                    version=int(current["version"]) + 1,
                )
                _propagate_descendants(
                    conn,
                    current["id"],
                    origin_visual_changed=not visuals_equal(visual, old_visual),
                    reason="rollback",
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"could not roll back look {current['id']}") from exc
        return LookStore.get(conn, current["id"])

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
        return [_version_record(row) for row in rows]

    @staticmethod
    def select_image(conn: sqlite3.Connection, look_id: str, image_id: str) -> dict[str, Any]:
        """Point ``look_selection`` at one image. Choosing an image does not spend."""
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
            children = descendant_ids(conn, look_id)
            segment_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT segment_asset_id FROM segment_ref WHERE look_id = ?",
                    (look_id,),
                )
            ]
            hashes = [
                row[0]
                for row in conn.execute(
                    "SELECT blob_hash FROM image WHERE look_id = ? AND blob_hash IS NOT NULL",
                    (look_id,),
                )
            ]
            conn.execute("DELETE FROM look WHERE id = ?", (look_id,))
            BlobStore.release_many(conn, hashes)
            _refresh_many(conn, children)
            mark_segments_stale(conn, segment_ids, reason="look deleted")
        return True


class ImageStore:
    """Image records. The bytes themselves live in :class:`BlobStore`.

    A new picture is a new row. Identity is ``image.id``, not a filename glob.
    ``source``, ``model``, ``prompt``, and ``cost`` are the provenance.
    """

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


def _attribute_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StorageError("attributes must be a JSON object")
    dump_json(value)
    return dict(value)


def _insert_look_row(
    conn: sqlite3.Connection,
    *,
    look_id: str,
    asset_id: str,
    name: str,
    parent_look_id: str | None,
    visual: dict[str, Any],
    covered: dict[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO look (
            id, asset_id, name, parent_look_id, description, prompt,
            attributes_json, created_at, updated_at, covered_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            look_id,
            asset_id,
            name,
            parent_look_id,
            visual["description"],
            visual["prompt"],
            _attributes(visual["attributes"]),
            created_at,
            created_at,
            dump_covered(covered),
        ),
    )


def _write_look_state(
    conn: sqlite3.Connection,
    *,
    look_id: str,
    name: str,
    parent_look_id: str | None,
    visual: dict[str, Any],
    covered: dict[str, Any],
    reason: str,
    version: int,
) -> None:
    created_at = utc_now()
    conn.execute(
        """
        UPDATE look
        SET name = ?, parent_look_id = ?, description = ?, prompt = ?,
            attributes_json = ?, covered_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            name,
            parent_look_id,
            visual["description"],
            visual["prompt"],
            _attributes(visual["attributes"]),
            dump_covered(covered),
            created_at,
            look_id,
        ),
    )
    _insert_version(
        conn,
        look_id=look_id,
        version=version,
        parent_look_id=parent_look_id,
        name=name,
        visual=visual,
        covered=covered,
        reason=reason,
        created_at=created_at,
    )


def _propagate_descendants(
    conn: sqlite3.Connection,
    look_id: str,
    *,
    origin_visual_changed: bool,
    reason: str,
) -> None:
    if origin_visual_changed:
        mark_look_outputs_stale(conn, look_id, reason=reason)
    _refresh_many(conn, descendant_ids(conn, look_id))


def _refresh_many(conn: sqlite3.Connection, root_ids: list[str]) -> None:
    """Rematerialize each look, then its descendants, from the current parent."""
    ordered: list[str] = []
    seen: set[str] = set()
    for root_id in root_ids:
        if root_id in seen:
            continue
        if conn.execute("SELECT 1 FROM look WHERE id = ?", (root_id,)).fetchone() is None:
            continue
        seen.add(root_id)
        ordered.append(root_id)
        for child_id in descendant_ids(conn, root_id):
            if child_id in seen:
                continue
            seen.add(child_id)
            ordered.append(child_id)
    for look_id in ordered:
        _refresh_inherited(conn, look_id)


def _refresh_inherited(conn: sqlite3.Connection, look_id: str) -> bool:
    """Copy uncovered fields from the parent. Append ``follow`` when they change."""
    row = conn.execute("SELECT * FROM look WHERE id = ?", (look_id,)).fetchone()
    if row is None:
        return False
    current = _look_record(conn, row)
    covered = normalize_covered(current["covered"])
    own = own_from_stored(
        current["description"],
        current["prompt"],
        current["attributes"],
        covered,
    )
    parent_id, parent_visual = resolve_parent(conn, current["parent_look_id"])
    visual = materialize(covered, own, parent_visual)
    old_visual = visual_of(current["description"], current["prompt"], current["attributes"])
    parent_changed = parent_id != current["parent_look_id"]
    visual_changed = not visuals_equal(visual, old_visual)
    if not parent_changed and not visual_changed:
        return False
    _write_look_state(
        conn,
        look_id=look_id,
        name=current["name"],
        parent_look_id=parent_id,
        visual=visual,
        covered=covered,
        reason="follow",
        version=int(current["version"]) + 1,
    )
    if visual_changed:
        mark_look_outputs_stale(conn, look_id, reason="follow")
    return visual_changed


def _version_record(row: sqlite3.Row) -> dict[str, Any]:
    record = row_dict(row)
    record["attributes"] = _load_attributes(record.pop("attributes_json")) or {}
    record["covered"] = load_covered(
        record.pop("covered_json"),
        description=record["description"],
        prompt=record["prompt"],
        attributes=record["attributes"],
    )
    record["version"] = int(record["version"])
    return record


def _insert_version(
    conn: sqlite3.Connection,
    *,
    look_id: str,
    version: int,
    parent_look_id: str | None,
    name: str,
    visual: dict[str, Any],
    covered: dict[str, Any],
    reason: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO look_version (
            id, look_id, version, parent_look_id, name, description, prompt,
            attributes_json, reason, created_at, covered_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id(),
            look_id,
            version,
            parent_look_id,
            name,
            visual["description"],
            visual["prompt"],
            _attributes(visual["attributes"]),
            reason,
            created_at,
            dump_covered(covered),
        ),
    )
