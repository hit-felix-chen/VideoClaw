"""Versioned, idempotent schema migrations for the project store.

Append a ``(version, name, statements)`` entry to :data:`Schema.MIGRATIONS`
when a later issue needs columns or tables. Each version runs once inside a
single transaction. Statements use ``IF NOT EXISTS`` so a crash before the
version row is recorded can be retried.

R6 persists the in-memory graph with :class:`storage.GraphStore` (the
``project.graph_json`` column holds ``to_dict()``). ``edge`` and
``node_state`` are the relational shape from the design and are not filled
from that JSON. R5 owns asset-library behavior on top of ``asset``, ``look``,
``look_version``, ``image``, ``look_selection``, and ``segment_ref``. R3 owns
ledger behavior on top of ``ledger``.
"""

from __future__ import annotations

import logging
import sqlite3

from storage.database import transaction, utc_now

logger = logging.getLogger(__name__)

# Kept in lockstep with the CHECK constraints below. Tests insert every value.
ASSET_KINDS = ("script", "character", "setting", "prop", "segment", "final_cut")
ASSET_SCOPES = ("global", "project")
IMAGE_SOURCES = ("generated", "uploaded", "extracted_frame")
EDGE_TYPES = ("hard", "ref", "continuity", "inherit")
NODE_STATUSES = (
    "pending",
    "ready",
    "running",
    "awaiting_user",
    "done",
    "stale",
    "failed",
    "skipped",
)

_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

# v1 statements. Order matters: parents before children.
_V1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS project (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        budget_cap REAL,
        spent REAL,
        status TEXT NOT NULL,
        graph_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episode (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        title TEXT,
        script_asset_id TEXT,
        final_cut_asset_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (project_id, number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS asset (
        id TEXT PRIMARY KEY,
        project_id TEXT REFERENCES project(id) ON DELETE CASCADE,
        scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
        kind TEXT NOT NULL CHECK (
            kind IN ('script', 'character', 'setting', 'prop', 'segment', 'final_cut')
        ),
        name TEXT NOT NULL,
        description TEXT,
        tags_json TEXT,
        episode_id TEXT REFERENCES episode(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (scope = 'project' AND project_id IS NOT NULL)
            OR (scope = 'global' AND project_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS look (
        id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        parent_look_id TEXT REFERENCES look(id)
            ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
        description TEXT,
        prompt TEXT,
        attributes_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS look_version (
        id TEXT PRIMARY KEY,
        look_id TEXT NOT NULL REFERENCES look(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        parent_look_id TEXT,
        name TEXT NOT NULL,
        description TEXT,
        prompt TEXT,
        attributes_json TEXT,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (look_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blob (
        hash TEXT PRIMARY KEY,
        mime TEXT NOT NULL,
        kind TEXT NOT NULL,
        ext TEXT NOT NULL,
        size INTEGER NOT NULL,
        refcount INTEGER NOT NULL CHECK (refcount >= 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS image (
        id TEXT PRIMARY KEY,
        asset_id TEXT REFERENCES asset(id) ON DELETE CASCADE,
        look_id TEXT REFERENCES look(id) ON DELETE CASCADE,
        blob_hash TEXT REFERENCES blob(hash) ON DELETE RESTRICT,
        source TEXT NOT NULL CHECK (source IN ('generated', 'uploaded', 'extracted_frame')),
        model TEXT,
        prompt TEXT,
        cost REAL,
        width INTEGER,
        height INTEGER,
        created_at TEXT NOT NULL,
        CHECK (asset_id IS NOT NULL OR look_id IS NOT NULL)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS look_selection (
        look_id TEXT PRIMARY KEY REFERENCES look(id) ON DELETE CASCADE,
        image_id TEXT NOT NULL REFERENCES image(id) ON DELETE CASCADE,
        selected_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS segment_ref (
        id TEXT PRIMARY KEY,
        segment_asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
        asset_id TEXT NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
        look_id TEXT REFERENCES look(id) ON DELETE SET NULL,
        role TEXT,
        alias TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edge (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
        src_id TEXT NOT NULL,
        dst_id TEXT NOT NULL,
        edge_type TEXT NOT NULL CHECK (edge_type IN ('hard', 'ref', 'continuity', 'inherit')),
        payload_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (project_id, src_id, dst_id, edge_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_state (
        project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
        node_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN (
                'pending', 'ready', 'running', 'awaiting_user',
                'done', 'stale', 'failed', 'skipped'
            )
        ),
        input_fingerprint TEXT,
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
        node_id TEXT,
        kind TEXT NOT NULL,
        model TEXT,
        quote_amount REAL,
        actual_amount REAL,
        currency TEXT,
        confirmed INTEGER,
        status TEXT NOT NULL,
        detail_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_episode_project ON episode(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_asset_project ON asset(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_asset_kind ON asset(kind)",
    "CREATE INDEX IF NOT EXISTS idx_look_asset ON look(asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_look_parent ON look(parent_look_id)",
    "CREATE INDEX IF NOT EXISTS idx_look_version_look ON look_version(look_id)",
    "CREATE INDEX IF NOT EXISTS idx_image_asset ON image(asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_image_look ON image(look_id)",
    "CREATE INDEX IF NOT EXISTS idx_image_blob ON image(blob_hash)",
    "CREATE INDEX IF NOT EXISTS idx_segment_ref_segment ON segment_ref(segment_asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_edge_project ON edge(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_project ON ledger(project_id)",
)


def _strip(statement: str) -> str:
    return statement.strip()


class Schema:
    """Apply and inspect ``schema_version``."""

    VERSION = 1
    ASSET_KINDS = ASSET_KINDS
    ASSET_SCOPES = ASSET_SCOPES
    IMAGE_SOURCES = IMAGE_SOURCES
    EDGE_TYPES = EDGE_TYPES
    NODE_STATUSES = NODE_STATUSES
    TABLES = (
        "project",
        "episode",
        "asset",
        "look",
        "look_version",
        "image",
        "look_selection",
        "segment_ref",
        "edge",
        "node_state",
        "ledger",
        "blob",
    )
    MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
        (1, "initial_project_store", tuple(_strip(item) for item in _V1)),
    )

    @staticmethod
    def ensure_version_table(conn: sqlite3.Connection) -> None:
        conn.execute(_VERSION_TABLE)

    @staticmethod
    def applied_versions(conn: sqlite3.Connection) -> list[int]:
        Schema.ensure_version_table(conn)
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        return [int(row[0]) for row in rows]

    @staticmethod
    def current(conn: sqlite3.Connection) -> int:
        versions = Schema.applied_versions(conn)
        if not versions:
            return 0
        return versions[-1]

    @staticmethod
    def migrate(conn: sqlite3.Connection) -> int:
        """Apply every migration whose version is not recorded yet.

        Safe to call on a fresh file, on a database that already finished
        these migrations, and on a database whose tables exist but whose
        ``schema_version`` row was never written.
        """
        Schema.ensure_version_table(conn)
        with transaction(conn):
            applied = set(Schema.applied_versions(conn))
            for version, name, statements in Schema.MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, utc_now()),
                )
                conn.execute(f"PRAGMA user_version = {int(version)}")
                logger.info("applied storage migration %s (%s)", version, name)
        return Schema.current(conn)
