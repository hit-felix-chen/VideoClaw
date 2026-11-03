"""Persisted confirmation tokens for a look-impact regenerate.

A token is a row in ``impact_confirmation``. It is bound to one project and
the exact downstream node set captured when it was issued. It expires, and
claiming it is single-use. This module does not run a node and does not
write the ledger.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from storage.database import (
    StorageError,
    dump_json,
    load_json,
    require_id,
    transaction,
    utc_now,
)
from storage.projects import ProjectStore

IMPACT_TOKEN_TTL_SECONDS = 30 * 60
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def _plus(stamp: str, seconds: int) -> str:
    parsed = datetime.strptime(stamp, _STAMP).replace(tzinfo=timezone.utc)
    later = parsed + timedelta(seconds=int(seconds))
    return later.strftime(_STAMP)


def _record(row) -> dict[str, Any]:
    nodes = load_json(row["node_ids_json"])
    if not isinstance(nodes, list) or any(not isinstance(item, str) for item in nodes):
        raise StorageError("stored impact set is invalid")
    result = None
    if row["result_json"]:
        loaded = load_json(row["result_json"])
        if not isinstance(loaded, dict):
            raise StorageError("stored impact result is invalid")
        result = loaded
    return {
        "token": row["token"],
        "project_id": row["project_id"],
        "look_id": row["look_id"],
        "asset_id": row["asset_id"],
        "node_ids": list(nodes),
        "issued_at": row["issued_at"],
        "expires_at": row["expires_at"],
        "consumed_at": row["consumed_at"],
        "result": result,
    }


class ImpactConfirmation:
    """Issue, read, and claim look-impact confirmation tokens."""

    @staticmethod
    def issue(
        conn,
        project_id: str,
        look_id: str,
        asset_id: str | None,
        node_ids: list[str],
        *,
        ttl_seconds: int = IMPACT_TOKEN_TTL_SECONDS,
    ) -> dict[str, Any]:
        ProjectStore.open(conn, project_id)
        project_id = require_id(project_id, "project_id")
        look_id = require_id(look_id, "look_id")
        if asset_id is not None:
            asset_id = require_id(asset_id, "asset_id")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise StorageError("ttl_seconds must be a positive integer")
        if not isinstance(node_ids, list) or any(not isinstance(item, str) or not item.strip() for item in node_ids):
            raise StorageError("impact set is invalid")
        now = utc_now()
        token = secrets.token_urlsafe(32)
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO impact_confirmation (
                    token, project_id, look_id, asset_id, node_ids_json,
                    issued_at, expires_at, consumed_at, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    token,
                    project_id,
                    look_id,
                    asset_id,
                    dump_json(list(node_ids)),
                    now,
                    _plus(now, ttl_seconds),
                ),
            )
        stored = ImpactConfirmation.get(conn, token)
        if stored is None:
            raise StorageError("impact confirmation was not stored")
        return stored

    @staticmethod
    def get(conn, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token.strip():
            return None
        row = conn.execute(
            """
            SELECT token, project_id, look_id, asset_id, node_ids_json,
                   issued_at, expires_at, consumed_at, result_json
            FROM impact_confirmation
            WHERE token = ?
            """,
            (token.strip(),),
        ).fetchone()
        if row is None:
            return None
        return _record(row)

    @staticmethod
    def claim(conn, token: str) -> bool:
        """Mark the token consumed. False when it is missing or already claimed."""
        token = token.strip()
        now = utc_now()
        claimed = False
        with transaction(conn):
            row = conn.execute(
                "SELECT consumed_at FROM impact_confirmation WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                return False
            conn.execute(
                """
                UPDATE impact_confirmation
                SET consumed_at = ?
                WHERE token = ? AND consumed_at IS NULL
                """,
                (now, token),
            )
            claimed = True
        return claimed

    @staticmethod
    def store_result(conn, token: str, result: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            raise StorageError("impact result must be a JSON object")
        token = token.strip()
        with transaction(conn):
            row = conn.execute(
                "SELECT token FROM impact_confirmation WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise StorageError("confirm_token is invalid")
            conn.execute(
                """
                UPDATE impact_confirmation
                SET result_json = ?
                WHERE token = ?
                """,
                (dump_json(result), token),
            )
