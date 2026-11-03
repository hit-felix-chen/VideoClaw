"""Content-addressed blob files.

Bytes are stored once, at ``<data_dir>/blobs/<sha256[:2]>/<sha256>.<ext>``.
The ``blob`` table holds the hash, mime type, kind, extension, size, and a
reference count. ``put`` acquires one reference. Each image row that points
at the hash acquires another. ``release`` drops one reference and deletes
the file only when the count reaches zero and no image still points at it.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import uuid
from pathlib import Path

from storage.database import (
    BLOB_DIRNAME,
    BlobNotFoundError,
    Database,
    StorageError,
    defer_unlink,
    row_dict,
    transaction,
    utc_now,
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")
_MIME_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/mp4": "m4a",
    "application/octet-stream": "bin",
}


def extension_for(mime: str) -> str:
    """Filename suffix for ``mime``. Unknown types use a sanitized subtype."""
    if mime in _MIME_EXT:
        return _MIME_EXT[mime]
    subtype = mime.split("/", 1)[1]
    cleaned = re.sub(r"[^a-z0-9]", "", subtype)[:8]
    if cleaned:
        return cleaned
    return "bin"


def _require_mime(mime: str) -> str:
    if not isinstance(mime, str):
        raise StorageError("mime must be a string")
    cleaned = mime.split(";", 1)[0].strip().lower()
    if _MIME_RE.fullmatch(cleaned) is None:
        raise StorageError("mime must be a type/subtype string")
    return cleaned


def _require_kind(kind: str) -> str:
    if not isinstance(kind, str):
        raise StorageError("kind must be a string")
    cleaned = kind.strip().lower()
    if not cleaned or len(cleaned) > 32 or not re.fullmatch(r"[a-z0-9_-]+", cleaned):
        raise StorageError("kind must be a short token such as image, video, or audio")
    return cleaned


def _require_hash(digest: str) -> str:
    if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
        raise StorageError("blob hash must be a sha256 hex digest")
    return digest


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class BlobStore:
    """Put, get, and release content-addressed files for one data directory."""

    @staticmethod
    def path_for(data_dir: Path, digest: str, ext: str) -> Path:
        digest = _require_hash(digest)
        if not isinstance(ext, str) or _EXT_RE.fullmatch(ext) is None:
            raise StorageError("blob extension must be 1-8 lowercase letters or digits")
        root = Path(data_dir) / BLOB_DIRNAME
        path = root / digest[:2] / f"{digest}.{ext}"
        path.relative_to(root)
        return path

    @staticmethod
    def put(
        conn: sqlite3.Connection,
        data: bytes,
        *,
        mime: str,
        kind: str,
    ) -> dict:
        """Store ``data`` and acquire one reference.

        Identical bytes share one file. A second ``put`` increments
        ``refcount`` and leaves the original mime and kind in place. A
        different mime or kind for the same bytes raises :class:`StorageError`.
        """
        if isinstance(data, bytearray):
            payload = bytes(data)
        elif isinstance(data, bytes):
            payload = data
        else:
            raise StorageError("blob data must be bytes")
        mime = _require_mime(mime)
        kind = _require_kind(kind)
        digest = hashlib.sha256(payload).hexdigest()
        ext = extension_for(mime)
        data_dir = Database.data_dir_of(conn)
        path = BlobStore.path_for(data_dir, digest, ext)

        last_conflict: sqlite3.IntegrityError | None = None
        for _attempt in range(2):
            created_file = False
            try:
                with transaction(conn):
                    row = conn.execute(
                        "SELECT * FROM blob WHERE hash = ?",
                        (digest,),
                    ).fetchone()
                    if row is None:
                        if not path.is_file():
                            _write_atomic(path, payload)
                            created_file = True
                        conn.execute(
                            """
                            INSERT INTO blob (hash, mime, kind, ext, size, refcount, created_at)
                            VALUES (?, ?, ?, ?, ?, 1, ?)
                            """,
                            (digest, mime, kind, ext, len(payload), utc_now()),
                        )
                    else:
                        if row["mime"] != mime or row["kind"] != kind:
                            raise StorageError(
                                f"blob {digest} is already stored as "
                                f"{row['mime']} ({row['kind']})"
                            )
                        stored = BlobStore.path_for(data_dir, digest, row["ext"])
                        if not stored.is_file():
                            _write_atomic(stored, payload)
                        conn.execute(
                            "UPDATE blob SET refcount = refcount + 1 WHERE hash = ?",
                            (digest,),
                        )
                return BlobStore.info(conn, digest)
            except sqlite3.IntegrityError as exc:
                last_conflict = exc
                if created_file:
                    leftover = conn.execute(
                        "SELECT 1 FROM blob WHERE hash = ?",
                        (digest,),
                    ).fetchone()
                    if leftover is None:
                        path.unlink(missing_ok=True)
                continue
            except Exception:
                if created_file:
                    leftover = conn.execute(
                        "SELECT 1 FROM blob WHERE hash = ?",
                        (digest,),
                    ).fetchone()
                    if leftover is None:
                        path.unlink(missing_ok=True)
                raise
        raise StorageError(f"could not store blob {digest}") from last_conflict

    @staticmethod
    def info(conn: sqlite3.Connection, digest: str) -> dict:
        digest = _require_hash(digest)
        row = conn.execute("SELECT * FROM blob WHERE hash = ?", (digest,)).fetchone()
        if row is None:
            raise BlobNotFoundError(digest)
        record = row_dict(row)
        record["size"] = int(record["size"])
        record["refcount"] = int(record["refcount"])
        record["path"] = str(
            BlobStore.path_for(Database.data_dir_of(conn), digest, record["ext"])
        )
        return record

    @staticmethod
    def get(conn: sqlite3.Connection, digest: str) -> bytes:
        record = BlobStore.info(conn, digest)
        path = Path(record["path"])
        if not path.is_file():
            raise BlobNotFoundError(digest, missing_file=True)
        return path.read_bytes()

    @staticmethod
    def retain(conn: sqlite3.Connection, digest: str) -> dict:
        """Acquire one more reference to an existing blob."""
        digest = _require_hash(digest)
        with transaction(conn):
            row = conn.execute("SELECT hash FROM blob WHERE hash = ?", (digest,)).fetchone()
            if row is None:
                raise BlobNotFoundError(digest)
            conn.execute(
                "UPDATE blob SET refcount = refcount + 1 WHERE hash = ?",
                (digest,),
            )
        return BlobStore.info(conn, digest)

    @staticmethod
    def release(conn: sqlite3.Connection, digest: str) -> bool:
        """Drop one reference. Delete the file when the count reaches zero.

        Returns True when the file is scheduled for removal. If an image row
        still points at the hash, the count is left unchanged and
        :class:`StorageError` is raised so a shared file is not deleted.
        """
        digest = _require_hash(digest)
        with transaction(conn):
            row = conn.execute("SELECT * FROM blob WHERE hash = ?", (digest,)).fetchone()
            if row is None:
                raise BlobNotFoundError(digest)
            if int(row["refcount"]) <= 0:
                raise StorageError(f"blob {digest} has no references to release")
            remaining_images = conn.execute(
                "SELECT COUNT(*) FROM image WHERE blob_hash = ?",
                (digest,),
            ).fetchone()[0]
            new_count = int(row["refcount"]) - 1
            if new_count == 0 and int(remaining_images) > 0:
                raise StorageError(f"blob {digest} is still referenced by an image")
            if new_count == 0:
                conn.execute("DELETE FROM blob WHERE hash = ?", (digest,))
                defer_unlink(
                    conn,
                    BlobStore.path_for(Database.data_dir_of(conn), digest, row["ext"]),
                )
                return True
            conn.execute(
                "UPDATE blob SET refcount = ? WHERE hash = ?",
                (new_count, digest),
            )
        return False

    @staticmethod
    def release_many(conn: sqlite3.Connection, digests: list[str]) -> None:
        for digest in digests:
            if digest:
                BlobStore.release(conn, digest)
