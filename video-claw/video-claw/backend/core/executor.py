"""Graph-driven node execution.

Reuses R6 ``DependencyGraph.why_blocked`` / ``revise`` / ``mark_stale``,
R3 ``quote`` / ``ProjectBudget`` / ``LedgerStore``, and R5
``RegenerationQuote``, ``ImageStore``, and ``SegmentRefStore.resolve``.
Confirm and the budget check sit here so a node runner cannot skip them.

Video calls return a quote and wait. The ledger is written only after
``confirm``. Image, VLM, and TTS calls are reserved immediately and do not
ask for a per-call confirm. Over budget pauses; raising the cap continues
the same quote; declining skips it. Regenerating one node reruns only that
node and leaves its successors stale.

Confirming a look impact is the path that does rerun the confirmed
downstream set. Each of those nodes goes through ``run_node``. A video node
then ``confirm``s, which authorizes the quote and writes the ledger. Image,
VLM, and TTS stay on the immediate reserve inside ``run_node``. The first
node that is still awaiting the user stops the rest of the batch, so a
later node is not reserved or spent in that confirm. A runner failure
releases that node's unsettled quote and stores the failure on the consumed
token. The confirmation token is a row in ``impact_confirmation``: one
project, one impact set, single-use, and it expires.

A model with no documented API price is not given a number. Segment
duration for a video quote comes from that model's verified capability,
not from a caller-supplied length.

The legacy stage machine stays beside this entrance. Cutover, once R8's
reference-to-video path is on this fork, still has to remove:

- ``core/orchestrator.py``
- ``api/routers/workflow.py`` (P4 ``cost_estimate`` and the continue API)
- ``api/routers/sessions.py``
- ``api/routers/stages.py``
- stage ``process()`` providers, including P3 last-frame chaining in
  ``core/agents/video_agent.py`` and ``core/agents/last_frame_chain.py``

``session.py`` had no callers and is already gone.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from graph.dependency_graph import DependencyGraph
from graph.model import NodeStatus
from models.middle.budget import ProjectBudget, pause_message
from models.middle.pricing import QuoteRequest, quote
from models.middle.registry import ModelRegistry
from storage.confirmations import ImpactConfirmation
from storage.database import StorageError, require_id, transaction, utc_now
from storage.ledger import AWAITING_USER, QUOTED, SKIPPED, LedgerStore
from storage.projects import GraphStore, ProjectStore
from storage.staleness import RegenerationQuote

_VIDEO = frozenset({"shot_segment", "draft_video", "final_video", "segment", "video"})
_IMAGE = frozenset({"character", "scene", "setting", "keyframe", "image"})
_NAMED = frozenset({"video", "image", "vlm", "tts"})
_BUDGET_ONLY = frozenset({"image", "vlm", "tts"})
_OPEN = frozenset({NodeStatus.PENDING, NodeStatus.STALE, NodeStatus.FAILED})


def _no_provider(call: dict) -> dict:
    """Local stand-in. It does not import a provider client."""
    return {
        "submitted": True,
        "provider_called": False,
        "node_id": call.get("node_id"),
    }


def _modality(node, params: dict) -> str:
    raw = params.get("modality") or node.metadata.get("modality") or ""
    if isinstance(raw, str) and raw.strip().lower() in _NAMED:
        return raw.strip().lower()
    kind = node.kind
    if kind in _VIDEO:
        return "video"
    if kind in _IMAGE:
        return "image"
    if kind in {"vlm", "tts"}:
        return kind
    return ""


def _video_quantity(model_id: str) -> tuple[Optional[dict], Optional[float]]:
    """Verified capability only. Auto (-1) and missing limits are not a length."""
    spec = ModelRegistry.get(model_id)
    if spec is None or spec.duration is None or not spec.duration.verified:
        return None, None
    default = spec.duration.default
    seconds = None
    if isinstance(default, int) and not isinstance(default, bool) and default > 0:
        seconds = float(default)
    return spec.duration.as_dict(), seconds


def _over(budget: dict, amount: float) -> bool:
    cap = budget["cap"]
    if cap is None:
        return False
    total = round(float(budget["used"]) + float(budget["committed"]) + float(amount), 6)
    return total > round(float(cap), 6)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _stored(params: dict, metadata: dict, key: str):
    """Caller value when one was passed, otherwise the value stored on the node."""
    if key in params and params.get(key) is not None:
        return params.get(key)
    if isinstance(metadata, dict):
        return metadata.get(key)
    return None


def _impact_node_ids(graph: DependencyGraph, asset_id: str | None) -> list[str]:
    """Downstream nodes a look change should regenerate, in topological order.

    Pinned nodes are listed by ``impact`` and are not regenerated. Pending and
    skipped nodes are already absent from that list. A node that is already
    stale is included: that is the usual state after the look write.
    """
    if not asset_id or asset_id not in graph:
        return []
    chosen: list[str] = []
    for node in graph.impact(asset_id).affected:
        if node.pinned:
            continue
        if node.would_stale or node.status == NodeStatus.STALE.value:
            chosen.append(node.id)
    return chosen


def _execution_summary(nodes: list[dict], *, halted: bool = False) -> dict:
    spent_ids = [
        item["node_id"]
        for item in nodes
        if item.get("spent") and isinstance(item.get("node_id"), str)
    ]
    return {
        "nodes": nodes,
        "submitted": any(bool(item.get("submitted")) for item in nodes),
        "spent": any(bool(item.get("spent")) for item in nodes),
        "spent_node_ids": spent_ids,
        "failed": False,
        "failed_node_id": None,
        "error": None,
        "halted": halted,
    }


class GraphExecutor:
    """Run one node. The runner is invoked only after the gate allows it."""

    def __init__(self, conn, runner: Optional[Callable[[dict], Any]] = None) -> None:
        self.conn = conn
        self._runner = runner or _no_provider
        self.submissions: list[str] = []

    def run_node(self, project_id: str, node_id: str, params: Optional[dict] = None) -> dict:
        params = dict(params or {})
        graph = self._load(project_id)
        node_id = self._node_id(graph, node_id)
        blocked = graph.why_blocked(node_id)
        if not blocked["executable"]:
            return self._pack(
                project_id,
                graph,
                node_id,
                status="blocked",
                gate="none",
                why_blocked=blocked,
            )
        node = graph.node(node_id)
        modality = _modality(node, params)
        if modality not in _NAMED:
            return self._pack(
                project_id,
                graph,
                node_id,
                status="unsupported",
                gate="none",
                message="node has no billable modality",
            )
        fields = self._fields(node, params, modality)
        if not fields["model_id"]:
            return self._pack(
                project_id,
                graph,
                node_id,
                status="unavailable",
                gate="none",
                message="model_id is required",
            )
        require_id(node_id, "node_id")
        references, versions = self._context(project_id, fields)
        request, duration, _quantity = self._request(fields)
        priced = quote(request)
        if modality == "video":
            if not priced.get("available"):
                return self._pack(
                    project_id,
                    graph,
                    node_id,
                    status="unavailable",
                    gate="none",
                    quote=priced,
                    message=priced.get("reason") or "",
                    duration=duration,
                    references=references,
                    versions=versions,
                )
            budget = ProjectBudget.budget_status(self.conn, project_id)
            over = _over(budget, float(priced["max"]))
            message = ""
            if over and budget["cap"] is not None:
                message = pause_message(budget["used"], priced, budget["cap"])
            graph.update_metadata(node_id, pending_call=fields)
            self._to_awaiting(graph, node_id)
            self._save(project_id, graph)
            return self._pack(
                project_id,
                graph,
                node_id,
                status="awaiting_user",
                gate="confirm",
                quote=priced,
                message=message,
                duration=duration,
                references=references,
                versions=versions,
                over_budget=over,
                call_confirmed=False,
            )
        view = ProjectBudget.authorize(
            self.conn,
            project_id,
            request,
            node_id=node_id,
            kind=modality,
        )
        if not view["allowed"]:
            if view["status"] == AWAITING_USER:
                graph.update_metadata(node_id, pending_call=fields)
                self._to_awaiting(graph, node_id)
                self._save(project_id, graph)
            return self._from_budget(
                project_id,
                graph,
                node_id,
                view,
                duration=duration,
                references=references,
                versions=versions,
                call_confirmed=False,
            )
        return self._finish(
            project_id,
            graph,
            node_id,
            fields,
            view,
            call_confirmed=False,
        )

    def confirm(self, project_id: str, node_id: str) -> dict:
        """Accept a held video quote. This is the first ledger write for that call."""
        graph = self._load(project_id)
        node_id = self._node_id(graph, node_id)
        node = graph.node(node_id)
        if node.status != NodeStatus.AWAITING_USER:
            raise _GateError("node is not awaiting confirmation")
        fields = node.metadata.get("pending_call")
        if not isinstance(fields, dict):
            raise _GateError("node has no quote to confirm")
        existing = self._awaiting_entry(project_id, node_id)
        if existing is not None:
            return self._held_budget(project_id, graph, node_id, existing)
        request, duration, _quantity = self._request(fields)
        view = ProjectBudget.authorize(
            self.conn,
            project_id,
            request,
            node_id=node_id,
            kind=fields.get("modality") or "video",
        )
        references, versions = self._context(project_id, fields)
        if not view["allowed"]:
            self._save(project_id, graph)
            return self._from_budget(
                project_id,
                graph,
                node_id,
                view,
                duration=duration,
                references=references,
                versions=versions,
                call_confirmed=True,
            )
        return self._finish(
            project_id,
            graph,
            node_id,
            fields,
            view,
            call_confirmed=True,
        )

    def decline_node(self, project_id: str, node_id: str) -> dict:
        graph = self._load(project_id)
        node_id = self._node_id(graph, node_id)
        existing = self._awaiting_entry(project_id, node_id)
        if existing is not None:
            return self.decline_budget(project_id, existing["id"])
        if graph.node(node_id).status != NodeStatus.AWAITING_USER:
            raise _GateError("node is not awaiting a decision")
        self._skip(graph, node_id)
        graph.update_metadata(node_id, pending_call=None)
        self._save(project_id, graph)
        return self._pack(
            project_id,
            graph,
            node_id,
            status="skipped",
            gate="none",
            message="未确认本次调用，节点已跳过。",
        )

    def raise_budget(self, project_id: str, entry_id: str, new_cap: float) -> dict:
        view = ProjectBudget.raise_cap(self.conn, project_id, entry_id, new_cap)
        entry = LedgerStore.get(self.conn, entry_id)
        node_id = entry.get("node_id")
        graph = self._load(project_id)
        if not view["allowed"]:
            return self._from_budget(
                project_id,
                graph,
                node_id,
                view,
                call_confirmed=True,
            )
        if not node_id:
            raise _GateError("ledger entry is not attached to a node")
        node_id = self._node_id(graph, node_id)
        fields = graph.node(node_id).metadata.get("pending_call")
        if not isinstance(fields, dict):
            raise _GateError("node has no held call to continue")
        return self._finish(
            project_id,
            graph,
            node_id,
            fields,
            view,
            call_confirmed=True,
        )

    def decline_budget(self, project_id: str, entry_id: str) -> dict:
        view = ProjectBudget.decline(self.conn, project_id, entry_id)
        entry = LedgerStore.get(self.conn, entry_id)
        node_id = entry.get("node_id")
        graph = self._load(project_id)
        if node_id and node_id in graph:
            self._skip(graph, node_id)
            graph.update_metadata(node_id, pending_call=None)
            self._save(project_id, graph)
        return self._from_budget(
            project_id,
            graph,
            node_id,
            view,
            call_confirmed=True,
        )

    def regenerate(self, project_id: str, node_id: str, params: Optional[dict] = None) -> dict:
        """Rerun this node only. Successors are marked stale and not started."""
        graph = self._load(project_id)
        node_id = self._node_id(graph, node_id)
        status = graph.node(node_id).status
        if status == NodeStatus.DONE:
            graph.transition(node_id, NodeStatus.PENDING)
        elif status in {NodeStatus.STALE, NodeStatus.FAILED}:
            graph.revise(node_id)
        elif status == NodeStatus.PENDING:
            pass
        else:
            raise _GateError(f"cannot regenerate a node that is {status.value}")
        self._save(project_id, graph)
        result = self.run_node(project_id, node_id, params)
        result["regenerated"] = node_id
        return result

    def impact(
        self,
        project_id: str,
        look_id: str,
        *,
        model: Optional[str] = None,
        unit_cost: Optional[float] = None,
    ) -> dict:
        """Downstream quote for a look change. Does not run nodes or spend.

        The returned ``confirm_token`` is stored in SQLite and bound to this
        project and the exact downstream set.
        """
        ProjectStore.open(self.conn, project_id)
        body = RegenerationQuote.build(
            self.conn,
            look_id,
            model=model,
            unit_cost=unit_cost,
        )
        row = self.conn.execute(
            "SELECT asset_id FROM look WHERE id = ?",
            (body["look_id"],),
        ).fetchone()
        asset_id = None if row is None else row["asset_id"]
        body["graph_impact"] = None
        node_ids: list[str] = []
        document = GraphStore.load(self.conn, project_id)
        if asset_id and isinstance(document, dict) and "nodes" in document:
            graph = DependencyGraph.from_dict(document)
            if asset_id in graph:
                body["graph_impact"] = graph.impact(asset_id).to_dict()
            node_ids = _impact_node_ids(graph, asset_id)
        issued = ImpactConfirmation.issue(
            self.conn,
            project_id,
            body["look_id"],
            asset_id,
            node_ids,
        )
        body["confirm_token"] = issued["token"]
        body["impact_nodes"] = list(node_ids)
        body["expires_at"] = issued["expires_at"]
        body["submitted"] = False
        body["spent"] = False
        body["provider_called"] = False
        return body

    def confirm_impact(
        self,
        project_id: str,
        look_id: str,
        confirm_token: str,
        *,
        model: Optional[str] = None,
        unit_cost: Optional[float] = None,
    ) -> dict:
        """Regenerate the confirmed downstream set. A second call does not spend again.

        A budget pause stops the rest of the batch. A node that fails after
        its quote is reserved releases that reservation, and the failure is
        stored on the consumed token so a later confirm returns it.
        """
        record = self._load_confirmation(
            project_id,
            confirm_token,
            look_id=look_id,
            allow_consumed=True,
        )
        if record["consumed_at"]:
            summary = record["result"]
            if not isinstance(summary, dict):
                raise _GateError("confirm_token was already consumed")
            return self._impact_body(look_id, model, unit_cost, record, summary)
        if not record["node_ids"]:
            raise _GateError("nothing to regenerate")
        if not ImpactConfirmation.claim(self.conn, record["token"]):
            raise _GateError("confirm_token was already consumed")
        summary = self._run_impact_batch(project_id, record["token"], record["node_ids"], None)
        return self._impact_body(look_id, model, unit_cost, record, summary)

    def regenerate_impact(
        self,
        project_id: str,
        confirm_token: str,
        node_ids: list[str],
        params: Optional[dict] = None,
    ) -> dict:
        """Rerun nodes from a confirmed impact set.

        Unknown nodes, nodes outside the set, and a missing, invalid,
        expired, stale, or consumed token fail before any node runs and
        before any ledger write.
        """
        record = self._load_confirmation(project_id, confirm_token, allow_consumed=False)
        ordered = self._checked_nodes(project_id, record["node_ids"], node_ids)
        if not ImpactConfirmation.claim(self.conn, record["token"]):
            raise _GateError("confirm_token was already consumed")
        summary = self._run_impact_batch(project_id, record["token"], ordered, params)
        return {
            "project_id": project_id,
            "look_id": record["look_id"],
            "confirm_token": record["token"],
            "impact_nodes": list(record["node_ids"]),
            "confirmed": True,
            "nodes": summary["nodes"],
            "submitted": summary["submitted"],
            "spent": summary["spent"],
            "spent_node_ids": list(summary.get("spent_node_ids") or []),
            "failed": bool(summary.get("failed")),
            "failed_node_id": summary.get("failed_node_id"),
            "error": summary.get("error"),
            "halted": bool(summary.get("halted")),
            "provider_called": False,
        }

    def _load_confirmation(
        self,
        project_id: str,
        confirm_token: str,
        *,
        look_id: Optional[str] = None,
        allow_consumed: bool = False,
    ) -> dict:
        project_id = require_id(project_id, "project_id")
        ProjectStore.open(self.conn, project_id)
        if look_id is not None:
            look_id = require_id(look_id, "look_id")
        if not isinstance(confirm_token, str) or not confirm_token.strip():
            raise _GateError("confirm_token is required")
        record = ImpactConfirmation.get(self.conn, confirm_token)
        if record is None:
            raise _GateError("confirm_token is invalid")
        if record["project_id"] != project_id:
            raise _GateError("confirm_token belongs to a different project")
        if look_id is not None and record["look_id"] != look_id:
            raise _GateError("confirm_token does not match this look")
        if record["expires_at"] < utc_now():
            raise _GateError("confirm_token has expired")
        self._reject_if_stale(project_id, record)
        if record["consumed_at"] and not allow_consumed:
            raise _GateError("confirm_token was already consumed")
        return record

    def _reject_if_stale(self, project_id: str, record: dict) -> None:
        try:
            graph = self._load(project_id)
        except _GateError:
            graph = None
        current = [] if graph is None else _impact_node_ids(graph, record.get("asset_id"))
        if sorted(current) != sorted(record["node_ids"]):
            raise _GateError("confirm_token is stale: the impact set changed")

    def _checked_nodes(self, project_id: str, allowed: list[str], node_ids: list[str]) -> list[str]:
        """Validate every requested node. Raises before any of them run."""
        if not isinstance(node_ids, list) or not node_ids:
            raise _GateError("node_ids is required")
        graph = self._load(project_id)
        allowed_set = set(allowed)
        problems: list[str] = []
        seen: set[str] = set()
        for raw in node_ids:
            if not isinstance(raw, str) or not raw.strip():
                problems.append("node id must be a string")
                continue
            node_id = raw.strip()
            if node_id in seen:
                problems.append(f"duplicate node: {node_id}")
                continue
            seen.add(node_id)
            if node_id not in graph:
                problems.append(f"unknown node: {node_id}")
                continue
            if node_id not in allowed_set:
                problems.append(f"node {node_id} is not in the confirmed impact set")
        if problems:
            raise _GateError("cannot regenerate: " + "; ".join(problems))
        return [node_id for node_id in graph.topological_order() if node_id in seen]

    def _run_impact_batch(
        self,
        project_id: str,
        token: str,
        node_ids: list[str],
        params: Optional[dict],
    ) -> dict:
        """Run the batch, persist its summary, and keep a failure on the token."""
        try:
            summary = self._execute_nodes(project_id, node_ids, params)
        except _ImpactFailure as exc:
            ImpactConfirmation.store_result(self.conn, token, exc.summary)
            failed = exc.summary.get("failed_node_id")
            raise _GateError(
                f"impact regenerate failed on node {failed}: {exc.summary.get('error')}"
            ) from exc
        ImpactConfirmation.store_result(self.conn, token, summary)
        return summary

    def _execute_nodes(self, project_id: str, node_ids: list[str], params: Optional[dict]) -> dict:
        """Run nodes in order. A pause or a video hold stops the rest of the batch."""
        graph = self._load(project_id)
        wanted = set(node_ids)
        ordered = [node_id for node_id in graph.topological_order() if node_id in wanted]
        results: list[dict] = []
        halted = False
        for node_id in ordered:
            try:
                result = self._run_confirmed_node(project_id, node_id, params)
            except Exception as exc:
                self._release_quoted_reservation(project_id, node_id)
                summary = _execution_summary(results)
                summary["failed"] = True
                summary["failed_node_id"] = node_id
                summary["error"] = str(exc).strip() or exc.__class__.__name__
                raise _ImpactFailure(summary) from exc
            results.append(result)
            if result.get("status") == AWAITING_USER:
                halted = True
                break
        return _execution_summary(results, halted=halted)

    def _release_quoted_reservation(self, project_id: str, node_id: str) -> None:
        """Drop a quote that was reserved but never settled. Spent rows stay."""
        for entry in LedgerStore.list(self.conn, project_id):
            if entry["node_id"] != node_id or entry["status"] != QUOTED:
                continue
            if entry["actual_amount"] is not None:
                continue
            LedgerStore.update(self.conn, entry["id"], status=SKIPPED)

    def _run_confirmed_node(self, project_id: str, node_id: str, params: Optional[dict]) -> dict:
        result = self.run_node(project_id, node_id, params)
        if result.get("gate") == "confirm":
            result = self.confirm(project_id, node_id)
        return result

    def _impact_body(
        self,
        look_id: str,
        model: Optional[str],
        unit_cost: Optional[float],
        record: dict,
        summary: dict,
    ) -> dict:
        quote = RegenerationQuote.build(
            self.conn,
            look_id,
            model=model,
            unit_cost=unit_cost,
        )
        body = dict(quote)
        body["confirm_token"] = record["token"]
        body["impact_nodes"] = list(record["node_ids"])
        body["confirmed"] = True
        body["executed"] = bool(summary.get("submitted"))
        body["will_spend"] = False
        body["spent"] = bool(summary.get("spent"))
        body["submitted"] = bool(summary.get("submitted"))
        body["provider_called"] = False
        body["nodes"] = list(summary.get("nodes") or [])
        body["spent_node_ids"] = list(summary.get("spent_node_ids") or [])
        body["failed"] = bool(summary.get("failed"))
        body["failed_node_id"] = summary.get("failed_node_id")
        body["error"] = summary.get("error")
        body["halted"] = bool(summary.get("halted"))
        return body

    def status(self, project_id: str) -> dict:
        ProjectStore.open(self.conn, project_id)
        document = GraphStore.load(self.conn, project_id)
        nodes: list[dict] = []
        awaiting: list[str] = []
        executable: list[str] = []
        stale: list[str] = []
        if isinstance(document, dict) and "nodes" in document:
            graph = DependencyGraph.from_dict(document)
            nodes = [graph.node(node_id).to_dict() for node_id in graph.topological_order()]
            awaiting = graph.pending_confirmations()
            executable = graph.executable_nodes()
            stale = graph.stale_nodes()
        return {
            "project_id": project_id,
            "budget": ProjectBudget.budget_status(self.conn, project_id),
            "ledger": LedgerStore.list(self.conn, project_id),
            "nodes": nodes,
            "awaiting_user": awaiting,
            "executable": executable,
            "stale": stale,
        }

    def _finish(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        fields: dict,
        view: dict,
        *,
        call_confirmed: bool,
    ) -> dict:
        request, duration, quantity = self._request(fields)
        del request
        references, versions = self._context(project_id, fields)
        self._submit(
            {
                "project_id": project_id,
                "node_id": node_id,
                "modality": fields.get("modality"),
                "model_id": fields.get("model_id"),
                "quantity": quantity,
                "duration": duration,
                "references": references,
                "versions": versions,
                "provider_called": False,
            }
        )
        amount = view.get("this_quote")
        if amount is None:
            raise _GateError("reserved quote has no amount")
        recorded = ProjectBudget.record_actual(
            self.conn,
            project_id,
            view["entry_id"],
            amount,
        )
        self._to_done(graph, node_id)
        graph.update_metadata(node_id, pending_call=None)
        self._save(project_id, graph)
        body = self._pack(
            project_id,
            graph,
            node_id,
            status="done",
            gate="none",
            quote=view.get("quote"),
            entry_id=view["entry_id"],
            message=view.get("message") or "",
            duration=duration,
            references=references,
            versions=versions,
            submitted=True,
            spent=True,
            call_confirmed=call_confirmed,
        )
        body["actual"] = recorded.get("actual")
        return body

    def _submit(self, call: dict) -> None:
        self.submissions.append(call["node_id"])
        result = self._runner(call)
        if isinstance(result, dict) and result.get("provider_called") is True:
            raise _GateError("node runner called a provider")

    def _fields(self, node, params: dict, modality: str) -> dict:
        model_id = params.get("model_id") or node.metadata.get("model_id") or ""
        if not isinstance(model_id, str):
            model_id = ""
        model_id = model_id.strip()
        asset_count = params.get("asset_count")
        quantity = None
        duration = None
        if modality == "video":
            duration, quantity = _video_quantity(model_id)
            asset_count = None
        elif modality == "image" and asset_count is None:
            asset_count = 1
        else:
            quantity = params.get("quantity")
        asset_id = params.get("asset_id") or node.metadata.get("asset_id")
        look_id = params.get("look_id") or node.metadata.get("look_id")
        return {
            "model_id": model_id,
            "modality": modality,
            "resolution": params.get("resolution"),
            "includes_video_input": params.get("includes_video_input"),
            "asset_count": asset_count,
            "quantity": quantity,
            "unit": params.get("unit"),
            "completion_tokens": _stored(params, node.metadata, "completion_tokens"),
            "input_tokens": _stored(params, node.metadata, "input_tokens"),
            "output_tokens": _stored(params, node.metadata, "output_tokens"),
            "text": _text(params.get("text")),
            "asset_id": asset_id if isinstance(asset_id, str) and asset_id.strip() else None,
            "look_id": look_id if isinstance(look_id, str) and look_id.strip() else None,
            "duration": duration,
        }

    def _request(self, fields: dict) -> tuple[QuoteRequest, Optional[dict], Optional[float]]:
        modality = fields.get("modality") or ""
        model_id = fields.get("model_id") or ""
        duration = fields.get("duration")
        quantity = fields.get("quantity")
        asset_count = fields.get("asset_count")
        if modality == "video":
            duration, quantity = _video_quantity(model_id)
            asset_count = None
        return (
            QuoteRequest(
                model_id=model_id,
                modality=modality,
                quantity=quantity,
                unit=fields.get("unit"),
                resolution=fields.get("resolution"),
                includes_video_input=fields.get("includes_video_input"),
                asset_count=asset_count,
                input_tokens=fields.get("input_tokens"),
                output_tokens=fields.get("output_tokens"),
                completion_tokens=fields.get("completion_tokens"),
            ),
            duration if isinstance(duration, dict) else None,
            quantity if isinstance(quantity, (int, float)) and not isinstance(quantity, bool) else None,
        )

    def _context(self, project_id: str, fields: dict) -> tuple[dict, list[str]]:
        from core.agents.character_agent import CharacterDesignerAgent

        agent = CharacterDesignerAgent()
        references = agent.resolve_calls(self.conn, _text(fields.get("text")), project_id)
        asset_id = fields.get("asset_id")
        look_id = fields.get("look_id")
        if not asset_id and not look_id:
            return references, []
        rows = agent.list_image_versions(self.conn, asset_id=asset_id, look_id=look_id)
        return references, [row["id"] for row in rows]

    def _awaiting_entry(self, project_id: str, node_id: str) -> Optional[dict]:
        rows = [
            entry
            for entry in LedgerStore.list(self.conn, project_id)
            if entry["node_id"] == node_id and entry["status"] == AWAITING_USER
        ]
        if not rows:
            return None
        return rows[-1]

    def _held_budget(self, project_id: str, graph: DependencyGraph, node_id: str, entry: dict) -> dict:
        detail = entry["detail"] if isinstance(entry["detail"], dict) else None
        return self._pack(
            project_id,
            graph,
            node_id,
            status="awaiting_user",
            gate="budget",
            quote=detail,
            entry_id=entry["id"],
            message="已暂停，等待上调预算或放弃。",
            over_budget=True,
            call_confirmed=True,
        )

    def _from_budget(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: Optional[str],
        view: dict,
        *,
        duration=None,
        references=None,
        versions=None,
        call_confirmed: bool,
    ) -> dict:
        status = view["status"]
        gate = "budget" if status == AWAITING_USER else "none"
        priced = view.get("quote")
        if not priced:
            priced = {
                "available": False,
                "min": None,
                "max": None,
                "unknown_models": list(view.get("unknown_models") or []),
                "reason": view.get("reason") or "",
            }
        return self._pack(
            project_id,
            graph,
            node_id,
            status=status,
            gate=gate,
            quote=priced,
            entry_id=view.get("entry_id"),
            message=view.get("message") or "",
            duration=duration,
            references=references,
            versions=versions,
            over_budget=status == AWAITING_USER,
            call_confirmed=call_confirmed,
            submitted=False,
            spent=False,
        )

    def _pack(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: Optional[str],
        *,
        status: str,
        gate: str,
        why_blocked: Optional[dict] = None,
        quote: Optional[dict] = None,
        entry_id: Optional[str] = None,
        message: str = "",
        duration: Optional[dict] = None,
        references: Optional[dict] = None,
        versions: Optional[list] = None,
        over_budget: bool = False,
        call_confirmed: bool = False,
        submitted: bool = False,
        spent: bool = False,
    ) -> dict:
        successors = {}
        if node_id and node_id in graph:
            successors = {
                item: graph.node(item).status.value for item in graph.successors(node_id)
            }
        return {
            "project_id": project_id,
            "node_id": node_id,
            "status": status,
            "submitted": submitted,
            "spent": spent,
            "provider_called": False,
            "gate": gate,
            "call_confirmed": call_confirmed,
            "why_blocked": why_blocked,
            "quote": quote,
            "entry_id": entry_id,
            "message": message,
            "duration": duration,
            "references": references,
            "versions": list(versions or []),
            "over_budget": over_budget,
            "successor_status": successors,
        }

    def _load(self, project_id: str) -> DependencyGraph:
        ProjectStore.open(self.conn, project_id)
        document = GraphStore.load(self.conn, project_id)
        if not isinstance(document, dict) or "nodes" not in document:
            raise _GateError("project has no dependency graph")
        return DependencyGraph.from_dict(document)

    def _save(self, project_id: str, graph: DependencyGraph) -> None:
        GraphStore.save(self.conn, project_id, graph.to_dict())
        now = utc_now()
        with transaction(self.conn):
            for node_id in graph.topological_order():
                require_id(node_id, "node_id")
                self.conn.execute(
                    """
                    INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(project_id, node_id) DO UPDATE SET
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    WHERE node_state.pinned = 0
                    """,
                    (project_id, node_id, graph.node(node_id).status.value, now),
                )

    def _node_id(self, graph: DependencyGraph, node_id: str) -> str:
        if node_id not in graph:
            from graph.errors import NodeNotFoundError

            raise NodeNotFoundError(node_id)
        return node_id

    def _to_awaiting(self, graph: DependencyGraph, node_id: str) -> None:
        status = graph.node(node_id).status
        if status == NodeStatus.AWAITING_USER:
            return
        if status in _OPEN:
            graph.transition(node_id, NodeStatus.RUNNING)
        if graph.node(node_id).status == NodeStatus.RUNNING:
            graph.transition(node_id, NodeStatus.AWAITING_USER)
            return
        raise _GateError(f"cannot hold node {node_id}")

    def _to_done(self, graph: DependencyGraph, node_id: str) -> None:
        status = graph.node(node_id).status
        if status == NodeStatus.DONE:
            return
        if status in _OPEN:
            graph.transition(node_id, NodeStatus.RUNNING)
        if graph.node(node_id).status in {NodeStatus.RUNNING, NodeStatus.AWAITING_USER}:
            graph.transition(node_id, NodeStatus.DONE)
            return
        raise _GateError(f"cannot finish node {node_id}")

    def _skip(self, graph: DependencyGraph, node_id: str) -> None:
        status = graph.node(node_id).status
        if status == NodeStatus.SKIPPED:
            return
        if status == NodeStatus.AWAITING_USER:
            graph.transition(node_id, NodeStatus.PENDING)
            status = NodeStatus.PENDING
        if status == NodeStatus.FAILED:
            graph.transition(node_id, NodeStatus.PENDING)
            status = NodeStatus.PENDING
        if status in {NodeStatus.PENDING, NodeStatus.STALE}:
            graph.transition(node_id, NodeStatus.SKIPPED)
            return
        raise _GateError(f"cannot skip node {node_id}")


class _ImpactFailure(Exception):
    """One node in an impact batch failed after earlier nodes may have spent."""

    def __init__(self, summary: dict) -> None:
        self.summary = summary
        super().__init__(summary.get("error") or "impact regenerate failed")


class _GateError(StorageError):
    """The executor refused the call before any provider work."""
