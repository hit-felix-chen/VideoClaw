"""SQLite project store for VideoClaw 2.0.

Local single-user persistence. A project replaces the old session. Call
:meth:`Database.connect` to open ``backend/code/data/videoclaw.db`` in WAL
mode and apply versioned migrations. Pass an explicit directory in tests.

Blob bytes live at ``<data>/blobs/<sha256[:2]>/<sha256>.<ext>``. The database
stores the hash, mime, and kind. Identical bytes share one file.

``GraphStore`` saves the JSON object returned by R6's ``to_dict`` on the
project. Looks inherit through ``parent_look_id`` with cycle detection,
version history, and rollback. ``SegmentRefStore`` binds a segment to an
asset and a look. ``RegenerationQuote`` prices a stale regenerate and does
not spend. ``LedgerStore`` records pre-call quotes and actual charges on
the ``ledger`` table; the budget gate that writes those rows lives in
``models.middle``. A look change marks dependents stale in ``node_state``
and, when the project has an R6 snapshot, in that graph (see
``storage.staleness``).
"""

from storage.assets import AssetStore, ImageStore, LookStore
from storage.blobs import BlobStore
from storage.database import (
    AssetNotFoundError,
    BlobNotFoundError,
    Database,
    ImageNotFoundError,
    LookCycleError,
    LookNotFoundError,
    LookVersionNotFoundError,
    ProjectNotFoundError,
    SegmentRefNotFoundError,
    StorageError,
    StoreConnection,
)
from storage.ledger import LedgerStore
from storage.projects import GraphStore, ProjectStore
from storage.references import SegmentRefStore
from storage.schema import Schema
from storage.staleness import RegenerationQuote

__all__ = [
    "AssetNotFoundError",
    "AssetStore",
    "BlobNotFoundError",
    "BlobStore",
    "Database",
    "GraphStore",
    "ImageNotFoundError",
    "ImageStore",
    "LedgerStore",
    "LookCycleError",
    "LookNotFoundError",
    "LookStore",
    "LookVersionNotFoundError",
    "ProjectNotFoundError",
    "ProjectStore",
    "RegenerationQuote",
    "Schema",
    "SegmentRefNotFoundError",
    "SegmentRefStore",
    "StorageError",
    "StoreConnection",
]
