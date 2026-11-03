"""Mark generated dependents stale, and quote a regenerate without spending.

R6's :class:`graph.dependency_graph.DependencyGraph` is on this branch. A
look whose resolved picture changes does two things:

* Upsert ``node_state.status = 'stale'`` for generated or extracted images
  (node id ``image:{image_id}``, which the graph does not model) and for
  segment assets bound in ``segment_ref``. Pinned rows, and rows already
  ``pending`` or ``skipped``, stay as they are. ``node_state.project_id`` is
  required, so a global look with no project and no segment consumer is
  recorded only on the quote.
* When the project's ``graph_json`` is a valid R6 snapshot, ``revise`` the
  look's asset (downstream only) and ``mark_stale`` each bound segment that
  can leave its current status, then save the snapshot. A pending segment is
  left pending: R6 refuses to mark that status stale. A failure in this hook
  does not undo the look write.

TODO(R6): image rows are not graph nodes. ``node_state`` keeps
``image:{image_id}`` until the graph grows an image node.

A named model is priced with the R3 price book. A model with no documented
API price stays unknown: this module does not turn prior image costs into
a stand-in number. :class:`RegenerationQuote` still never calls a model and
never writes a ledger row. The executor spends only after its own confirm.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import sqlite3
from typing import Any

from storage.database import (
    LookNotFoundError,
    StorageError,
    dump_json,
    load_json,
    optional_amount,
    require_id,
    require_name,
    utc_now,
)
from storage.projects import GraphStore

logger = logging.getLogger(__name__)

IMAGE_NODE_PREFIX = "image:"
STALE_IMAGE_SOURCES = ("generated", "extracted_frame")
_PROTECTED_STATUSES = ("pending", "skipped")

# Pricing a model id belongs to the price book (R3). Until a caller passes
# unit_cost, the quote sums costs already stored on image rows.
_PRICING_NOTE = (
    "unit_cost when the caller passes one; otherwise PriceBook for the named model. "
    "A model with no documented API price is unknown and is not turned into a number. "
    "Confirming this quote does not spend; the executor writes the ledger "
    "only after its own confirmation."
)


def image_node_id(image_id: str) -> str:
    return f"{IMAGE_NODE_PREFIX}{image_id}"


def mark_look_outputs_stale(
    conn: sqlite3.Connection,
    look_id: str,
    *,
    reason: str,
) -> list[dict[str, Any]]:
    """Flag generated images and bound segments after this look's picture changed."""
    look_id = require_id(look_id, "look_id")
    reason = require_name(reason, "reason")
    look = conn.execute(
        "SELECT id, asset_id FROM look WHERE id = ?",
        (look_id,),
    ).fetchone()
    if look is None:
        return []
    asset = conn.execute(
        "SELECT id, project_id FROM asset WHERE id = ?",
        (look["asset_id"],),
    ).fetchone()
    images = conn.execute(
        "SELECT id, source FROM image WHERE look_id = ?",
        (look_id,),
    ).fetchall()
    refs = _segment_rows(conn, look_id, look["asset_id"])

    project_ids: set[str] = set()
    if asset is not None and asset["project_id"]:
        project_ids.add(asset["project_id"])
    for ref in refs:
        if ref["project_id"]:
            project_ids.add(ref["project_id"])

    image_ids = [row["id"] for row in images if row["source"] in STALE_IMAGE_SOURCES]
    segment_ids = sorted({ref["segment_asset_id"] for ref in refs})
    now = utc_now()
    marked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for project_id in sorted(project_ids):
        for image_id in image_ids:
            node_id = image_node_id(image_id)
            _append_mark(marked, seen, _upsert_stale(conn, project_id, node_id, now))
        for ref in refs:
            if ref["project_id"] != project_id:
                continue
            _append_mark(
                marked,
                seen,
                _upsert_stale(conn, project_id, ref["segment_asset_id"], now),
            )
    _try_r6(
        conn,
        project_ids,
        asset_id=look["asset_id"],
        segment_ids=segment_ids,
        reason=reason,
    )
    return marked


def mark_segments_stale(
    conn: sqlite3.Connection,
    segment_ids: list[str],
    *,
    reason: str,
) -> list[dict[str, Any]]:
    """Mark segment assets that still exist. Used when the look row is gone."""
    require_name(reason, "reason")
    now = utc_now()
    marked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for segment_id in segment_ids:
        row = conn.execute(
            "SELECT id, project_id FROM asset WHERE id = ? AND kind = 'segment'",
            (segment_id,),
        ).fetchone()
        if row is None or not row["project_id"]:
            continue
        _append_mark(
            marked,
            seen,
            _upsert_stale(conn, row["project_id"], row["id"], now),
        )
    return marked


class RegenerationQuote:
    """Price a stale regenerate. Confirming still does not generate or spend."""

    @staticmethod
    def build(
        conn: sqlite3.Connection,
        look_id: str,
        *,
        model: str | None = None,
        unit_cost: float | None = None,
    ) -> dict[str, Any]:
        look_id = require_id(look_id, "look_id")
        if conn.execute("SELECT 1 FROM look WHERE id = ?", (look_id,)).fetchone() is None:
            raise LookNotFoundError(look_id)
        if model is not None:
            model = require_name(model, "model")
        unit_cost = optional_amount(unit_cost, "unit_cost")
        artifacts = _stale_artifacts(conn, look_id)
        estimated, cost_known, unknown_models, price_source = _price_artifacts(
            artifacts,
            unit_cost=unit_cost,
            model=model,
        )
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM look_version WHERE look_id = ?",
            (look_id,),
        ).fetchone()[0]
        payload = {
            "look_id": look_id,
            "look_version": int(version),
            "model": model,
            "unit_cost": unit_cost,
            "estimated_cost": estimated,
            "unknown_models": unknown_models,
            "artifacts": [
                {
                    "node_id": item["node_id"],
                    "project_id": item["project_id"],
                    "prior_cost": item["prior_cost"],
                }
                for item in artifacts
            ],
        }
        count = len(artifacts)
        return {
            "look_id": look_id,
            "look_version": int(version),
            "model": model,
            "unit_cost": unit_cost,
            "currency": "CNY",
            "artifact_count": count,
            "artifacts": artifacts,
            "estimated_cost": estimated,
            "cost_known": cost_known,
            "unknown_models": unknown_models,
            "price_source": price_source,
            "requires_confirmation": count > 0,
            "confirmed": False,
            "executed": False,
            "will_spend": False,
            "confirm_token": hashlib.sha256(dump_json(payload).encode("utf-8")).hexdigest(),
            "pricing": _PRICING_NOTE,
            "todo": (
                "R7 must wait for user confirmation of confirm_token before spending. "
                "This response does not generate or charge."
            ),
        }

    @staticmethod
    def confirm(
        conn: sqlite3.Connection,
        look_id: str,
        confirm_token: str,
        *,
        model: str | None = None,
        unit_cost: float | None = None,
    ) -> dict[str, Any]:
        """Accept the current quote. Still does not generate or spend."""
        if not isinstance(confirm_token, str) or not confirm_token.strip():
            raise StorageError("confirm_token is required")
        quote = RegenerationQuote.build(
            conn,
            look_id,
            model=model,
            unit_cost=unit_cost,
        )
        if confirm_token.strip() != quote["confirm_token"]:
            raise StorageError("confirm_token does not match the current regeneration quote")
        if not quote["requires_confirmation"]:
            raise StorageError("nothing stale to confirm")
        accepted = dict(quote)
        accepted["confirmed"] = True
        accepted["executed"] = False
        accepted["will_spend"] = False
        return accepted


def _segment_rows(conn: sqlite3.Connection, look_id: str, asset_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.segment_asset_id, r.asset_id, r.look_id, a.project_id
        FROM segment_ref AS r
        JOIN asset AS a ON a.id = r.segment_asset_id
        WHERE r.look_id = ?
           OR (r.look_id IS NULL AND r.asset_id = ?)
        ORDER BY r.created_at ASC, r.id ASC
        """,
        (look_id, asset_id),
    ).fetchall()


def _append_mark(
    marked: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    item: dict[str, Any] | None,
) -> None:
    if item is None:
        return
    key = (item["project_id"], item["node_id"])
    if key in seen:
        return
    seen.add(key)
    marked.append(item)


def _upsert_stale(
    conn: sqlite3.Connection,
    project_id: str,
    node_id: str,
    now: str,
) -> dict[str, Any] | None:
    existing = conn.execute(
        """
        SELECT status, pinned FROM node_state
        WHERE project_id = ? AND node_id = ?
        """,
        (project_id, node_id),
    ).fetchone()
    if existing is not None and (
        int(existing["pinned"]) == 1 or existing["status"] in _PROTECTED_STATUSES
    ):
        return {
            "project_id": project_id,
            "node_id": node_id,
            "status": existing["status"],
            "changed": False,
            "backend": "node_state",
        }
    conn.execute(
        """
        INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
        VALUES (?, ?, 'stale', 0, ?)
        ON CONFLICT(project_id, node_id) DO UPDATE SET
            status = 'stale',
            updated_at = excluded.updated_at
        WHERE node_state.pinned = 0
          AND node_state.status NOT IN ('pending', 'skipped')
        """,
        (project_id, node_id, now),
    )
    return {
        "project_id": project_id,
        "node_id": node_id,
        "status": "stale",
        "changed": True,
        "backend": "node_state",
    }


_R6_CLASS: dict[str, Any] = {"loaded": False, "cls": None}


# Statuses R6 will move to stale. Pending and skipped raise if mark_stale includes them.
_R6_STALEABLE = frozenset({"done", "running", "awaiting_user", "failed"})


def _load_r6() -> Any:
    """Import DependencyGraph once. None only if the package cannot be imported."""
    if not _R6_CLASS["loaded"]:
        _R6_CLASS["loaded"] = True
        try:
            module = importlib.import_module("graph.dependency_graph")
        except ImportError:
            logger.info(
                "R6 graph package is not importable; stale marks stay on node_state. "
                "TODO(R6): call DependencyGraph.mark_stale / revise when graph imports."
            )
            _R6_CLASS["cls"] = None
        else:
            _R6_CLASS["cls"] = getattr(module, "DependencyGraph", None)
    return _R6_CLASS["cls"]


def _node_status(node: Any) -> str:
    status = getattr(node, "status", "")
    value = getattr(status, "value", status)
    return str(value)


def _try_r6(
    conn: sqlite3.Connection,
    project_ids: set[str],
    *,
    asset_id: str,
    segment_ids: list[str],
    reason: str,
) -> None:
    """Revise the saved R6 graph. ImportError leaves node_state as the only mark."""
    del reason
    graph_cls = _load_r6()
    if graph_cls is None:
        return
    for project_id in sorted(project_ids):
        try:
            document = GraphStore.load(conn, project_id)
            if not isinstance(document, dict) or "nodes" not in document:
                continue
            graph = graph_cls.from_dict(document)
            if asset_id in graph:
                graph.revise(asset_id)
            for segment_id in segment_ids:
                if segment_id not in graph:
                    continue
                if _node_status(graph.node(segment_id)) not in _R6_STALEABLE:
                    continue
                graph.mark_stale(segment_id)
            GraphStore.save(conn, project_id, graph.to_dict())
        except Exception:
            logger.warning(
                "R6 stale hook skipped for project %s",
                project_id,
                exc_info=True,
            )


def _version_rows(conn: sqlite3.Connection, look_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT version, description, prompt, attributes_json, created_at
        FROM look_version
        WHERE look_id = ?
        ORDER BY version ASC
        """,
        (look_id,),
    ).fetchall()
    versions = []
    for row in rows:
        attributes = row["attributes_json"]
        parsed: dict[str, Any] = {}
        if attributes is not None:
            loaded = load_json(attributes)
            if isinstance(loaded, dict):
                parsed = loaded
        versions.append(
            {
                "version": int(row["version"]),
                "description": row["description"],
                "prompt": row["prompt"],
                "attributes": parsed,
                "created_at": row["created_at"],
            }
        )
    return versions


def _visual_change_times(versions: list[dict[str, Any]]) -> list[str]:
    previous: tuple[Any, ...] | None = None
    times: list[str] = []
    for version in versions:
        visual = (
            version["description"],
            version["prompt"],
            dump_json(version["attributes"]),
        )
        if previous is not None and visual != previous:
            times.append(version["created_at"])
        previous = visual
    return times


def _stale_artifacts(conn: sqlite3.Connection, look_id: str) -> list[dict[str, Any]]:
    look = conn.execute("SELECT asset_id FROM look WHERE id = ?", (look_id,)).fetchone()
    if look is None:
        raise LookNotFoundError(look_id)
    changes = _visual_change_times(_version_rows(conn, look_id))
    artifacts: list[dict[str, Any]] = []
    images = conn.execute(
        """
        SELECT id, source, model, prompt, cost, created_at, asset_id
        FROM image
        WHERE look_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (look_id,),
    ).fetchall()
    asset = conn.execute(
        "SELECT project_id FROM asset WHERE id = ?",
        (look["asset_id"],),
    ).fetchone()
    project_id = None if asset is None else asset["project_id"]
    for image in images:
        if image["source"] not in STALE_IMAGE_SOURCES:
            continue
        if not any(stamp > image["created_at"] for stamp in changes):
            continue
        node_id = image_node_id(image["id"])
        if project_id and _graph_blocks_regenerate(conn, project_id, node_id):
            continue
        prior = None if image["cost"] is None else float(image["cost"])
        artifacts.append(
            {
                "node_id": node_id,
                "kind": "image",
                "source": image["source"],
                "model": image["model"],
                "project_id": project_id,
                "prior_cost": prior,
                "stale": True,
            }
        )
    for ref in _segment_rows(conn, look_id, look["asset_id"]):
        if not ref["project_id"]:
            continue
        state = conn.execute(
            """
            SELECT status FROM node_state
            WHERE project_id = ? AND node_id = ?
            """,
            (ref["project_id"], ref["segment_asset_id"]),
        ).fetchone()
        if state is None or state["status"] != "stale":
            continue
        artifacts.append(
            {
                "node_id": ref["segment_asset_id"],
                "kind": "segment",
                "source": None,
                "model": None,
                "project_id": ref["project_id"],
                "prior_cost": None,
                "stale": True,
            }
        )
    artifacts.sort(key=lambda item: (item["node_id"], item["project_id"] or ""))
    # One segment can match two refs. Keep the first.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in artifacts:
        key = (item["node_id"], item["project_id"] or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _graph_blocks_regenerate(conn: sqlite3.Connection, project_id: str, node_id: str) -> bool:
    """Pinned, pending, and skipped rows are not part of a regenerate quote."""
    state = conn.execute(
        """
        SELECT status, pinned FROM node_state
        WHERE project_id = ? AND node_id = ?
        """,
        (project_id, node_id),
    ).fetchone()
    if state is None:
        return False
    if int(state["pinned"]) == 1 or state["status"] in _PROTECTED_STATUSES:
        return True
    return state["status"] != "stale"


def _price_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    unit_cost: float | None,
    model: str | None,
) -> tuple[float | None, bool, list[str], str]:
    """Return estimated cost, whether it is known, unknown models, and the source.

    ``unit_cost`` is an explicit caller price. A named model goes through
    PriceBook and does not fall back to stored image costs. With no model,
    the sum of recorded costs is kept for callers that have not picked one.
    """
    if not artifacts:
        return 0.0, True, [], "none"
    if unit_cost is not None:
        return unit_cost * len(artifacts), True, [], "unit_cost"
    if model:
        return _price_from_book(model, artifacts)
    total = 0.0
    for item in artifacts:
        if item["prior_cost"] is None:
            return None, False, [], "prior_cost"
        total += float(item["prior_cost"])
    return total, True, [], "prior_cost"


def _price_from_book(
    model: str,
    artifacts: list[dict[str, Any]],
) -> tuple[float | None, bool, list[str], str]:
    """Price each stale artifact. A missing API price stays unknown."""
    from models.middle.pricing import QuoteRequest
    from models.middle.pricing import quote as price_quote

    total = 0.0
    for item in artifacts:
        modality = "image" if item.get("kind") == "image" else "video"
        priced = price_quote(
            QuoteRequest(
                model_id=model,
                modality=modality,
                asset_count=1 if modality == "image" else None,
                quantity=1 if modality == "image" else None,
            )
        )
        if not priced.get("available") or priced.get("max") is None:
            listed = [str(name) for name in (priced.get("unknown_models") or []) if name]
            return None, False, listed, "unknown"
        total += float(priced["max"])
    return round(total, 6), True, [], "price_book"
