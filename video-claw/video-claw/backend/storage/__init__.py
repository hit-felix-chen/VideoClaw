"""SQLite project store for VideoClaw 2.0.

Local single-user persistence. A project replaces the old session. Call
:meth:`Database.connect` to open ``backend/code/data/videoclaw.db`` in WAL
mode and apply versioned migrations. Pass an explicit directory in tests.

Blob bytes live at ``<data>/blobs/<sha256[:2]>/<sha256>.<ext>``. The database
stores the hash, mime, and kind. Identical bytes share one file.

``GraphStore`` saves the JSON object returned by R6's ``to_dict`` on the
project. Asset, look, and image methods are the schema-level stubs R5 extends.
"""

from storage.assets import AssetStore, ImageStore, LookStore
from storage.blobs import BlobStore
from storage.database import (
    AssetNotFoundError,
    BlobNotFoundError,
    Database,
    ImageNotFoundError,
    LookNotFoundError,
    ProjectNotFoundError,
    StorageError,
    StoreConnection,
)
from storage.projects import GraphStore, ProjectStore
from storage.schema import Schema

__all__ = [
    "AssetNotFoundError",
    "AssetStore",
    "BlobNotFoundError",
    "BlobStore",
    "Database",
    "GraphStore",
    "ImageNotFoundError",
    "ImageStore",
    "LookNotFoundError",
    "LookStore",
    "ProjectNotFoundError",
    "ProjectStore",
    "Schema",
    "StorageError",
    "StoreConnection",
]
