"""Per-project budget gate.

Every generation call is quoted before it is reserved. When used plus
committed plus this quote would pass the project cap, the call is not
submitted: the ledger row stays ``awaiting_user`` until the user raises
the cap or declines. Raising continues the same quote. Declining skips it.
This module does not call a provider. Video confirm-before-execute is R7.
"""

from __future__ import annotations

from typing import Any, Optional

from models.middle.pricing import QuoteRequest, quote
from storage.database import StorageError, optional_amount, require_id, transaction, utc_now
from storage.ledger import AWAITING_USER, QUOTED, SKIPPED, SPENT, UNAVAILABLE, LedgerStore
from storage.projects import ProjectStore


def _exceeds(used: float, committed: float, this_quote: float, cap: Optional[float]) -> bool:
    if cap is None:
        return False
    return round(used + committed + this_quote, 6) > round(float(cap), 6)


def _plain(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _quote_text(priced: dict) -> str:
    low = priced["min"]
    high = priced["max"]
    if low == high:
        return _plain(low)
    return f"{_plain(low)}~{_plain(high)}"


def _currency_suffix(currency: Optional[str]) -> str:
    if not currency:
        return ""
    return f" {currency}"


def pause_message(used: float, priced: dict, cap: float) -> str:
    """The over-budget prompt: used / this quote / current cap."""
    suffix = _currency_suffix(priced.get("currency"))
    return (
        f"已用 {_plain(used)}{suffix} / "
        f"本次报价 {_quote_text(priced)}{suffix} / "
        f"当前上限 {_plain(cap)}{suffix}。"
        "是否上调预算上限？"
    )


def _set_node(conn, project_id: str, node_id: Optional[str], status: str) -> None:
    if not node_id:
        return
    node_id = require_id(node_id, "node_id")
    project_id = require_id(project_id, "project_id")
    conn.execute(
        """
        INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
        VALUES (?, ?, ?, 0, ?)
        ON CONFLICT(project_id, node_id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        WHERE node_state.pinned = 0
        """,
        (project_id, node_id, status, utc_now()),
    )


class ProjectBudget:
    """Quote, reserve, pause, and record actuals for one project."""

    @staticmethod
    def budget_status(conn, project_id: str) -> dict[str, Any]:
        project = ProjectStore.open(conn, project_id)
        totals = LedgerStore.sums(conn, project_id)
        currencies = LedgerStore.currencies(conn, project_id)
        cap = project["budget_cap"]
        used = totals["used"]
        committed = totals["committed"]
        remaining = None if cap is None else round(cap - used - committed, 6)
        return {
            "used": used,
            "committed": committed,
            "remaining": remaining,
            "cap": cap,
            "currency": currencies[0] if len(currencies) == 1 else None,
            "mixed_currency": len(currencies) > 1,
        }

    @staticmethod
    def _view(
        conn,
        project_id: str,
        entry: dict,
        priced: dict,
        *,
        allowed: bool,
        message: str,
    ) -> dict[str, Any]:
        status = ProjectBudget.budget_status(conn, project_id)
        quote_body = None
        this_quote = None
        if priced.get("available"):
            quote_body = {
                "min": priced["min"],
                "max": priced["max"],
                "currency": priced["currency"],
                "unit": priced["unit"],
                "source_url": priced["source_url"],
                "effective_date": priced["effective_date"],
                "resolution": priced["resolution"],
            }
            this_quote = priced["max"]
        return {
            "entry_id": entry["id"],
            "status": entry["status"],
            "allowed": allowed,
            "submitted": False,
            "confirmed": entry["confirmed"],
            "model_id": priced.get("model_id") or entry.get("model"),
            "quote": quote_body,
            "this_quote": this_quote,
            "unknown_models": list(priced.get("unknown_models") or []),
            "message": message,
            "reason": priced.get("reason") or "",
            "used": status["used"],
            "committed": status["committed"],
            "remaining": status["remaining"],
            "cap": status["cap"],
            "currency": status["currency"],
        }

    @staticmethod
    def authorize(
        conn,
        project_id: str,
        request: QuoteRequest,
        *,
        node_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> dict[str, Any]:
        """Quote and either reserve or pause. Does not submit the provider call."""
        ProjectStore.open(conn, project_id)
        priced = quote(request)
        call_kind = kind or (request.modality or "").strip() or "generation"
        model_id = priced.get("model_id") or request.model_id
        if node_id is not None:
            node_id = require_id(node_id, "node_id")

        if not priced["available"]:
            with transaction(conn):
                entry = LedgerStore.add(
                    conn,
                    project_id,
                    kind=call_kind,
                    status=UNAVAILABLE,
                    model=model_id or None,
                    node_id=node_id,
                    quote_amount=None,
                    currency=None,
                    confirmed=False,
                    detail=priced,
                )
            message = ""
            if priced["unknown_models"]:
                message = "没有文档价格，不计算报价，不提交调用。"
            return ProjectBudget._view(conn, project_id, entry, priced, allowed=False, message=message)

        currencies = LedgerStore.currencies(conn, project_id)
        if priced["currency"] and currencies and priced["currency"] not in currencies:
            with transaction(conn):
                entry = LedgerStore.add(
                    conn,
                    project_id,
                    kind=call_kind,
                    status=UNAVAILABLE,
                    model=model_id,
                    node_id=node_id,
                    quote_amount=None,
                    currency=priced["currency"],
                    confirmed=False,
                    detail=priced,
                )
            refused = dict(priced)
            refused["available"] = False
            refused["reason"] = "quote currency does not match the project ledger"
            return ProjectBudget._view(
                conn,
                project_id,
                entry,
                refused,
                allowed=False,
                message="报价币种与项目账本不一致，不提交调用。",
            )

        commitment = float(priced["max"])
        with transaction(conn):
            status = ProjectBudget.budget_status(conn, project_id)
            over = _exceeds(status["used"], status["committed"], commitment, status["cap"])
            if over:
                entry = LedgerStore.add(
                    conn,
                    project_id,
                    kind=call_kind,
                    status=AWAITING_USER,
                    model=model_id,
                    node_id=node_id,
                    quote_amount=commitment,
                    currency=priced["currency"],
                    confirmed=False,
                    detail=priced,
                )
                _set_node(conn, project_id, node_id, AWAITING_USER)
                message = pause_message(status["used"], priced, status["cap"])
                allowed = False
            else:
                entry = LedgerStore.add(
                    conn,
                    project_id,
                    kind=call_kind,
                    status=QUOTED,
                    model=model_id,
                    node_id=node_id,
                    quote_amount=commitment,
                    currency=priced["currency"],
                    confirmed=False,
                    detail=priced,
                )
                message = ""
                allowed = True
        return ProjectBudget._view(conn, project_id, entry, priced, allowed=allowed, message=message)

    @staticmethod
    def _require_waiting(conn, project_id: str, entry_id: str) -> dict:
        project_id = require_id(project_id, "project_id")
        entry = LedgerStore.get(conn, entry_id)
        if entry["project_id"] != project_id:
            raise StorageError("ledger entry is not on this project")
        if entry["status"] != AWAITING_USER:
            raise StorageError("ledger entry is not awaiting a budget decision")
        if entry["quote_amount"] is None:
            raise StorageError("ledger entry has no quote to continue")
        return entry

    @staticmethod
    def raise_cap(conn, project_id: str, entry_id: str, new_cap: float) -> dict[str, Any]:
        """Raise the cap and reserve this quote, or leave the pause in place.

        A cap that still does not cover used + committed + this quote is
        refused. The previous cap stays, and the call is still not submitted.
        """
        accepted = optional_amount(new_cap, "budget_cap")
        if accepted is None:
            raise StorageError("budget_cap must be a number")
        with transaction(conn):
            entry = ProjectBudget._require_waiting(conn, project_id, entry_id)
            status = ProjectBudget.budget_status(conn, project_id)
            if _exceeds(status["used"], status["committed"], entry["quote_amount"], accepted):
                still = dict(entry)
                priced = entry["detail"] if isinstance(entry["detail"], dict) else {}
                message = pause_message(status["used"], {
                    "min": priced.get("min", entry["quote_amount"]),
                    "max": priced.get("max", entry["quote_amount"]),
                    "currency": entry["currency"],
                }, status["cap"] if status["cap"] is not None else accepted)
                view = ProjectBudget._view(
                    conn,
                    project_id,
                    still,
                    priced if priced.get("available") else {
                        "available": True,
                        "model_id": entry["model"],
                        "min": entry["quote_amount"],
                        "max": entry["quote_amount"],
                        "currency": entry["currency"],
                        "unit": None,
                        "source_url": "",
                        "effective_date": None,
                        "resolution": None,
                        "unknown_models": [],
                        "reason": "new cap still does not cover this quote",
                    },
                    allowed=False,
                    message=message,
                )
                view["status"] = AWAITING_USER
                view["allowed"] = False
                view["submitted"] = False
                return view
            ProjectStore.update(conn, project_id, budget_cap=accepted)
            updated = LedgerStore.update(conn, entry_id, status=QUOTED, confirmed=True)
            _set_node(conn, project_id, entry["node_id"], "ready")
        priced = updated["detail"] if isinstance(updated["detail"], dict) else {}
        if not priced.get("available"):
            priced = {
                "available": True,
                "model_id": updated["model"],
                "min": updated["quote_amount"],
                "max": updated["quote_amount"],
                "currency": updated["currency"],
                "unit": priced.get("unit"),
                "source_url": priced.get("source_url") or "",
                "effective_date": priced.get("effective_date"),
                "resolution": priced.get("resolution"),
                "unknown_models": [],
                "reason": "",
            }
        view = ProjectBudget._view(
            conn,
            project_id,
            updated,
            priced,
            allowed=True,
            message="已上调预算上限，可以继续。",
        )
        view["submitted"] = False
        return view

    @staticmethod
    def decline(conn, project_id: str, entry_id: str) -> dict[str, Any]:
        """Skip the paused call. The quote is not reserved and nothing is submitted."""
        with transaction(conn):
            entry = ProjectBudget._require_waiting(conn, project_id, entry_id)
            updated = LedgerStore.update(conn, entry_id, status=SKIPPED, confirmed=False)
            _set_node(conn, project_id, entry["node_id"], SKIPPED)
        priced = updated["detail"] if isinstance(updated["detail"], dict) else {}
        if not priced:
            priced = {
                "available": True,
                "model_id": updated["model"],
                "min": updated["quote_amount"],
                "max": updated["quote_amount"],
                "currency": updated["currency"],
                "unit": None,
                "source_url": "",
                "effective_date": None,
                "resolution": None,
                "unknown_models": [],
                "reason": "",
            }
        return ProjectBudget._view(
            conn,
            project_id,
            updated,
            priced,
            allowed=False,
            message="未上调预算上限，本次调用不执行。",
        )

    @staticmethod
    def record_actual(conn, project_id: str, entry_id: str, actual: float) -> dict[str, Any]:
        """Write the settled charge for a quote that was already reserved."""
        amount = optional_amount(actual, "actual_amount")
        if amount is None:
            raise StorageError("actual_amount must be a number")
        project_id = require_id(project_id, "project_id")
        with transaction(conn):
            entry = LedgerStore.get(conn, entry_id)
            if entry["project_id"] != project_id:
                raise StorageError("ledger entry is not on this project")
            if entry["status"] != QUOTED:
                raise StorageError("only a reserved quote can record an actual")
            updated = LedgerStore.update(
                conn,
                entry_id,
                status=SPENT,
                actual_amount=amount,
            )
            used = LedgerStore.sums(conn, project_id)["used"]
            ProjectStore.update(conn, project_id, spent=used)
        priced = updated["detail"] if isinstance(updated["detail"], dict) else {}
        if not priced.get("available"):
            priced = {
                "available": True,
                "model_id": updated["model"],
                "min": updated["quote_amount"],
                "max": updated["quote_amount"],
                "currency": updated["currency"],
                "unit": priced.get("unit"),
                "source_url": priced.get("source_url") or "",
                "effective_date": priced.get("effective_date"),
                "resolution": priced.get("resolution"),
                "unknown_models": [],
                "reason": "",
            }
        view = ProjectBudget._view(conn, project_id, updated, priced, allowed=False, message="")
        view["actual"] = updated["actual_amount"]
        view["submitted"] = False
        return view
