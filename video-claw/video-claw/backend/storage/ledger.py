"""Project spend ledger on the R4 ``ledger`` table.

Rows store the pre-call quote, the actual charge, and whether the user
confirmed a higher budget. This module does not price a model and does not
call a provider.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from storage.database import (
    StorageError,
    dump_json,
    load_json,
    new_id,
    optional_amount,
    require_id,
    transaction,
    utc_now,
)
from storage.projects import UNSET, ProjectStore

QUOTED = "quoted"
AWAITING_USER = "awaiting_user"
SKIPPED = "skipped"
SPENT = "spent"
UNAVAILABLE = "unavailable"

_STATUSES = (QUOTED, AWAITING_USER, SKIPPED, SPENT, UNAVAILABLE)
_COLUMNS = (
    "id, project_id, node_id, kind, model, quote_amount, actual_amount, "
    "currency, confirmed, status, detail_json, created_at"
)


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StorageError(f"{field} must be a string or null")
    cleaned = value.strip()
    if not cleaned:
        return None
    if "\x00" in cleaned or len(cleaned) > 256:
        raise StorageError(f"{field} must be at most 256 characters")
    return cleaned


def _kind(value: str) -> str:
    if not isinstance(value, str):
        raise StorageError("kind must be a string")
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned or len(cleaned) > 64:
        raise StorageError("kind must be a non-empty string up to 64 characters")
    return cleaned


def _status(value: str) -> str:
    if value not in _STATUSES:
        raise StorageError(f"ledger status must be one of {', '.join(_STATUSES)}")
    return value


def _confirmed(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if value in (0, 1):
        return int(value)
    raise StorageError("confirmed must be true or false")


def _entry(row: sqlite3.Row) -> dict[str, Any]:
    detail = None
    if row["detail_json"]:
        loaded = load_json(row["detail_json"])
        detail = loaded if isinstance(loaded, dict) else {"value": loaded}
    quote_amount = None if row["quote_amount"] is None else float(row["quote_amount"])
    actual_amount = None if row["actual_amount"] is None else float(row["actual_amount"])
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "node_id": row["node_id"],
        "kind": row["kind"],
        "model": row["model"],
        "quote_amount": quote_amount,
        "actual_amount": actual_amount,
        "currency": row["currency"],
        "confirmed": bool(row["confirmed"]) if row["confirmed"] is not None else False,
        "status": row["status"],
        "detail": detail,
        "created_at": row["created_at"],
    }


class LedgerStore:
    """Insert and read ledger rows for one project."""

    @staticmethod
    def add(
        conn: sqlite3.Connection,
        project_id: str,
        *,
        kind: str,
        status: str,
        model: str | None = None,
        node_id: str | None = None,
        quote_amount: float | None = None,
        actual_amount: float | None = None,
        currency: str | None = None,
        confirmed: bool = False,
        detail: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> dict[str, Any]:
        ProjectStore.open(conn, project_id)
        project_id = require_id(project_id, "project_id")
        if node_id is not None:
            node_id = require_id(node_id, "node_id")
        kind = _kind(kind)
        status = _status(status)
        model = _optional_text(model, "model")
        currency = _optional_text(currency, "currency")
        quote_amount = optional_amount(quote_amount, "quote_amount")
        actual_amount = optional_amount(actual_amount, "actual_amount")
        confirmed_bit = _confirmed(confirmed)
        if detail is not None and not isinstance(detail, dict):
            raise StorageError("detail must be a JSON object or null")
        payload = None if detail is None else dump_json(detail)
        entry_id = new_id() if entry_id is None else require_id(entry_id, "entry_id")
        created_at = utc_now()
        try:
            with transaction(conn):
                conn.execute(
                    f"""
                    INSERT INTO ledger ({_COLUMNS})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        project_id,
                        node_id,
                        kind,
                        model,
                        quote_amount,
                        actual_amount,
                        currency,
                        confirmed_bit,
                        status,
                        payload,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"ledger entry already exists: {entry_id}") from exc
        return LedgerStore.get(conn, entry_id)

    @staticmethod
    def get(conn: sqlite3.Connection, entry_id: str) -> dict[str, Any]:
        entry_id = require_id(entry_id, "entry_id")
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM ledger WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"ledger entry not found: {entry_id}")
        return _entry(row)

    @staticmethod
    def list(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
        project_id = require_id(project_id, "project_id")
        ProjectStore.open(conn, project_id)
        rows = conn.execute(
            f"""
            SELECT {_COLUMNS}
            FROM ledger
            WHERE project_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (project_id,),
        ).fetchall()
        return [_entry(row) for row in rows]

    @staticmethod
    def update(
        conn: sqlite3.Connection,
        entry_id: str,
        *,
        status: Any = UNSET,
        confirmed: Any = UNSET,
        actual_amount: Any = UNSET,
        quote_amount: Any = UNSET,
    ) -> dict[str, Any]:
        entry_id = require_id(entry_id, "entry_id")
        current = LedgerStore.get(conn, entry_id)
        assignments: list[str] = []
        values: list[Any] = []
        if status is not UNSET:
            assignments.append("status = ?")
            values.append(_status(status))
        if confirmed is not UNSET:
            assignments.append("confirmed = ?")
            values.append(_confirmed(confirmed))
        if actual_amount is not UNSET:
            assignments.append("actual_amount = ?")
            values.append(optional_amount(actual_amount, "actual_amount"))
        if quote_amount is not UNSET:
            assignments.append("quote_amount = ?")
            values.append(optional_amount(quote_amount, "quote_amount"))
        if not assignments:
            return current
        values.append(entry_id)
        with transaction(conn):
            conn.execute(
                f"UPDATE ledger SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        return LedgerStore.get(conn, entry_id)

    @staticmethod
    def sums(conn: sqlite3.Connection, project_id: str) -> dict[str, float]:
        """Used is recorded actuals. Committed is a reserved quote not yet actual."""
        project_id = require_id(project_id, "project_id")
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(actual_amount), 0) AS used,
                COALESCE(SUM(
                    CASE
                        WHEN status = ? AND actual_amount IS NULL THEN quote_amount
                        ELSE 0
                    END
                ), 0) AS committed
            FROM ledger
            WHERE project_id = ?
            """,
            (QUOTED, project_id),
        ).fetchone()
        return {
            "used": round(float(row["used"]), 6),
            "committed": round(float(row["committed"]), 6),
        }

    @staticmethod
    def currencies(conn: sqlite3.Connection, project_id: str) -> list[str]:
        project_id = require_id(project_id, "project_id")
        rows = conn.execute(
            """
            SELECT DISTINCT currency
            FROM ledger
            WHERE project_id = ? AND currency IS NOT NULL AND currency != ''
            ORDER BY currency ASC
            """,
            (project_id,),
        ).fetchall()
        return [row["currency"] for row in rows]
