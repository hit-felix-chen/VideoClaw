"""SQLite connection, transactions, and shared errors for the project store."""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

DB_FILENAME = "videoclaw.db"
BLOB_DIRNAME = "blobs"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class StoreConnection:
    """SQLite connection bound to the data directory ``Database.connect`` opened.

    ``sqlite3.Connection`` cannot carry extra attributes in this interpreter,
    so the path and the list of blob files waiting to be unlinked live here.
    The object forwards the connection methods the store uses.
    """

    def __init__(self, raw: sqlite3.Connection, data_dir: Path) -> None:
        self.raw = raw
        self.data_dir = data_dir
        self.pending_unlinks: list[Path] = []

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self.raw.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor:
        return self.raw.executemany(sql, seq_of_parameters)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    @property
    def in_transaction(self) -> bool:
        return self.raw.in_transaction


class StorageError(Exception):
    """The project store rejected a call or could not finish it."""


class ProjectNotFoundError(StorageError):
    """No project row exists for this id."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"project not found: {project_id}")


class AssetNotFoundError(StorageError):
    """No asset row exists for this id."""

    def __init__(self, asset_id: str) -> None:
        self.asset_id = asset_id
        super().__init__(f"asset not found: {asset_id}")


class LookNotFoundError(StorageError):
    """No look row exists for this id."""

    def __init__(self, look_id: str) -> None:
        self.look_id = look_id
        super().__init__(f"look not found: {look_id}")


class ImageNotFoundError(StorageError):
    """No image row exists for this id."""

    def __init__(self, image_id: str) -> None:
        self.image_id = image_id
        super().__init__(f"image not found: {image_id}")


class BlobNotFoundError(StorageError):
    """No blob row exists, or its file is missing from disk."""

    def __init__(self, digest: str, *, missing_file: bool = False) -> None:
        self.digest = digest
        self.missing_file = missing_file
        if missing_file:
            message = f"blob file missing: {digest}"
        else:
            message = f"blob not found: {digest}"
        super().__init__(message)


def dump_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StorageError("value is not JSON serializable") from exc


def load_json(payload: str) -> Any:
    try:
        return json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise StorageError("stored value is not valid JSON") from exc


def utc_now() -> str:
    """UTC timestamp with fixed width so text ordering matches time ordering."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id() -> str:
    return uuid.uuid4().hex


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def require_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise StorageError(f"{field} must be a non-empty id")
    return value


def require_name(value: str, field: str = "name") -> str:
    if not isinstance(value, str):
        raise StorageError(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned:
        raise StorageError(f"{field} must not be empty")
    return cleaned


def require_status(value: str) -> str:
    if not isinstance(value, str):
        raise StorageError("status must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 64 or "\x00" in cleaned:
        raise StorageError("status must be a non-empty string up to 64 characters")
    return cleaned


def optional_amount(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageError(f"{field} must be a number or null")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise StorageError(f"{field} must be a finite number >= 0")
    return number


def _as_store(conn: StoreConnection) -> StoreConnection:
    if not isinstance(conn, StoreConnection):
        raise StorageError("connection was not opened by Database.connect")
    return conn


def defer_unlink(conn: StoreConnection, path: Path) -> None:
    """Remove ``path`` only after the outermost transaction commits."""
    _as_store(conn).pending_unlinks.append(path)


def _flush_unlinks(conn: StoreConnection) -> None:
    pending = _as_store(conn).pending_unlinks
    paths = list(pending)
    pending.clear()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove blob file %s", path, exc_info=True)


@contextmanager
def transaction(conn: StoreConnection) -> Iterator[StoreConnection]:
    """Commit ``conn`` as one unit. Nested calls use a savepoint.

    Blob files queued with :func:`defer_unlink` are removed after the
    outermost commit. A rollback leaves those files on disk.
    """
    store = _as_store(conn)
    pending = store.pending_unlinks
    if conn.in_transaction:
        mark = len(pending)
        name = f"sp_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            del pending[mark:]
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {name}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        finally:
            pending.clear()
        raise
    else:
        _flush_unlinks(conn)


class Database:
    """Open the local SQLite database and run migrations."""

    @staticmethod
    def default_data_dir() -> Path:
        """``backend/code/data`` next to this package.

        The database file is ``videoclaw.db`` inside that directory.
        ``code/`` is runtime data and stays out of git.
        """
        backend_dir = Path(__file__).resolve().parents[1]
        return backend_dir / "code" / "data"

    @staticmethod
    def connect(data_dir: str | Path | None = None) -> StoreConnection:
        """Open ``videoclaw.db`` in WAL mode and apply pending migrations.

        ``data_dir`` defaults to :meth:`default_data_dir`. The directory is
        created when it is missing. Legacy session folders are left alone.
        """
        root = Path(data_dir).expanduser() if data_dir is not None else Database.default_data_dir()
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        db_path = root / DB_FILENAME
        raw = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        raw.row_factory = sqlite3.Row
        conn = StoreConnection(raw, root)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise StorageError("SQLite foreign keys could not be enabled")
            journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal).lower() != "wal":
                raise StorageError(f"SQLite WAL mode could not be enabled (got {journal})")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            from storage.schema import Schema

            Schema.migrate(conn)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    @contextmanager
    def session(data_dir: str | Path | None = None) -> Iterator[StoreConnection]:
        conn = Database.connect(data_dir)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def data_dir_of(conn: StoreConnection) -> Path:
        return _as_store(conn).data_dir
