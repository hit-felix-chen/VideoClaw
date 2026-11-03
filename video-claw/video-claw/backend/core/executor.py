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

A video confirm that ``ProjectBudget.authorize`` accepts then submits.
Before that authorize, the confirm claims the node in SQLite
(``BEGIN IMMEDIATE``): ``awaiting_user`` becomes ``running``. The loser of
two concurrent confirms is rejected immediately and does not authorize or
submit. The node runner prepares the Seedance request. Submit uses the
video adapter passed into ``GraphExecutor`` when tests inject one,
otherwise the registry adapter for that model when its provider key is
configured. Seedance 2.5 (``doubao-seedance-2-5-260628``) resolves to
``ArkVideoAdapter``, which reads ``ARK_API_KEY`` /
``api_providers.ark.api_key``.

``adapter.submit`` runs synchronously. For Ark that one call submits the
task, polls until the vendor finishes, downloads the file to ``save_path``,
and returns the remote video URL. ``result_id`` is that URL. The local
``save_path`` is stored on the node result beside it. A bare task id is not
a finished result. Duration on the request is the shot length when it sits
inside the model's verified range. Seedance 2.5 allows 4 to 30 seconds, or
-1 when the shot length is outside that range and the model allows auto.

The node is marked done and the reserved quote is spent only after that
URL or path comes back. Reference asset ids are turned into on-disk image
files immediately before ``submit``. A missing image or file skips the
quote and leaves the node awaiting the user; the asset id is not sent as a
URL. With no injected adapter and no provider key, the runner's prepared
request is still stored with those asset ids and spent. That is the
dispatcher path the existing suite drives.

If ``submit`` raises or returns nothing useful, the reserved quote is
marked skipped, the same release an impact regenerate uses for a runner
failure. The claim is put back to ``awaiting_user`` only when
``node_state.updated_at`` is still the stamp this claim wrote, so a late
failure cannot overwrite another attempt that already finished.
``submit_error`` is stored with ``ARK_API_KEY`` redacted. The raised
error does not chain the vendor exception. No spent row is written.
Spent and done are committed in one transaction after submit returns.

Before ``adapter.submit``, the same claim writes ``submit_dispatched`` on
the node and commits it. That marker and ``vendor_task_id`` are cleared
only when this attempt never issued HTTP: a missing key, a missing file,
an unknown model, ``FileNotFoundError``, or ``UnsupportedParam``. Poll and
download errors, vendor failed or expired, and every HTTP 4xx keep both.
A 4xx body that contains an id stores that id. The quote is skipped, the
node returns to ``awaiting_user``, and a plain confirm does not POST.
``retry_video_submit`` claims without clearing the marker. It resumes a
stored task id or settles a stored result URL, including a stale running
claim, and spends the original quote. ``force_new=True`` first releases
that quote and authorizes a new one. The task id is dropped only after
that authorization, and only then does a new POST run. If the new quote
does not fit the budget, the id and marker stay and ``raise_budget`` may
continue that attempt. A result URL is committed on its own before the
spent-and-done transaction.
After ``VIDEO_SUBMIT_CLAIM_TTL_SECONDS`` (30 minutes), recovery and the
next ``confirm`` do not submit again. A stale ``running`` row with no
marker died before the vendor call and can be claimed again.

The Seedance client stops the whole submit at 20 minutes
(``SUBMIT_DEADLINE_SECONDS``), under the 30-minute TTL. The poll's own
cap of 120 rounds with a 30-second HTTP timeout and a 5-second pause
would otherwise run about 74 minutes and outlive the claim.
A fresh ``running`` claim is rejected so a live submit is not double-spent.
``raise_budget`` refuses while ``submit_dispatched`` or ``vendor_task_id``
is set, unless this pause is the budget exit of ``force_new``. Otherwise
it claims the same way. ``node_state`` ``ready`` left by a
kill after the cap is raised is claimable, and that confirm spends the
quote already reserved instead of saying the node is not awaiting.

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

The stage machine is gone. ``run_node`` dispatches to the agent node
executors after the gate. Video defaults to Seedance 2.5 reference mode.
Script and storyboard nodes run without a per-call confirm and without a
ledger row. Image, VLM, and TTS still reserve immediately.
"""

from __future__ import annotations

from typing import Any, Callable, NoReturn, Optional

from graph.dependency_graph import DependencyGraph
from graph.model import NodeStatus
from models.middle.budget import ProjectBudget, pause_message
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.pricing import QuoteRequest, quote
from models.middle.registry import ModelRegistry
from storage.confirmations import ImpactConfirmation
from storage.database import StorageError, dump_json, require_id, transaction, utc_now
from storage.ledger import AWAITING_USER, QUOTED, SKIPPED, LedgerStore
from storage.projects import GraphStore, ProjectStore
from storage.staleness import RegenerationQuote

_VIDEO = frozenset({"shot_segment", "draft_video", "final_video", "segment", "video"})
_IMAGE = frozenset({"character", "scene", "setting", "keyframe", "image"})
_NAMED = frozenset({"video", "image", "vlm", "tts"})
_BUDGET_ONLY = frozenset({"image", "vlm", "tts"})
_STRUCTURAL = frozenset({"script", "storyboard", "previz"})
_OPEN = frozenset({NodeStatus.PENDING, NodeStatus.STALE, NodeStatus.FAILED})
# Must stay above Seedance SUBMIT_DEADLINE_SECONDS (20 minutes). The raw
# poll cap is about 74 minutes, which used to outlive this claim.
VIDEO_SUBMIT_CLAIM_TTL_SECONDS = 30 * 60
SUBMIT_DISPATCHED = "submit_dispatched"
SUBMIT_DISPATCHED_NOTE = (
    "previous submit may have reached the vendor and was not finished; "
    "it was not sent again. Call retry_video_submit to confirm a manual retry."
)
VENDOR_FAILED_NOTE = (
    "vendor judged it failed; a manual retry_video_submit(force_new=True) "
    "is required to create a new one"
)
ORPHAN_RESULT_NOTE = (
    "result reached vendor but state was overwritten; manual check needed"
)


def _default_runner(call: dict) -> dict:
    from core.nodes.dispatch import dispatch_node

    return dispatch_node(call)


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


def _provider_configured(provider: str) -> bool:
    """True when that vendor's API key is present in config.

    Seedance reads ``api_providers.ark.api_key`` (``Config.ARK_API_KEY``).
    An empty key keeps the dispatcher path and does not open a connection.
    """
    from config import Config

    keys = {
        "ark": Config.ARK_API_KEY,
        "dashscope": Config.DASHSCOPE_API_KEY,
        "kling": Config.KLING_API_KEY,
    }
    value = keys.get(provider)
    return isinstance(value, str) and bool(value.strip())


def _video_output_path(project_id: str, node_id: str) -> str:
    from pathlib import Path

    from config import Config

    def safe(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
        return cleaned or "node"

    return str(Path(Config.RESULT_DIR) / "video" / safe(project_id) / f"{safe(node_id)}.mp4")


def _claim_cutoff() -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(seconds=VIDEO_SUBMIT_CLAIM_TTL_SECONDS)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _redact_secret(message: str) -> str:
    """Hide ``Config.ARK_API_KEY`` if a provider error echoes it."""
    from config import Config

    key = Config.ARK_API_KEY
    if isinstance(key, str) and key and key in message:
        return message.replace(key, "***")
    return message


def _looks_like_url_or_path(text: str) -> bool:
    """A remote URL or a filesystem path. A bare task id is neither."""
    if text.startswith(("http://", "https://", "file://")):
        return True
    return text.startswith(("/", "\\")) or "/" in text or "\\" in text


def _submit_ref(value: Any) -> Optional[str]:
    """Remote URL or local path from a finished submit.

    ``ArkVideoAdapter.submit`` submits, polls, downloads, and returns the
    remote video URL. That string is ``result_id``. A bare task id is not
    a finished result and is treated as failure.
    """
    candidates: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            candidates.append(text)
    elif isinstance(value, dict):
        if value.get("error") or value.get("ok") is False or value.get("success") is False:
            return None
        for key in ("url", "path", "save_path", "result_id", "task_id", "id"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                candidates.append(item.strip())
    for text in candidates:
        if _looks_like_url_or_path(text):
            return text
    return None


def _look_by_asset(references: Any) -> dict:
    """First resolved mention per asset, including its look when one was named."""
    found: dict = {}
    if not isinstance(references, dict):
        return found
    for mention in references.get("mentions") or []:
        if not isinstance(mention, dict) or not mention.get("resolved"):
            continue
        for match in mention.get("matches") or []:
            if not isinstance(match, dict):
                continue
            asset_id = match.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id or asset_id in found:
                continue
            look_id = match.get("look_id")
            found[asset_id] = look_id if isinstance(look_id, str) and look_id else None
            break
    return found


def _image_disk_path(conn, asset_id: str, look_id: Optional[str]) -> Optional[str]:
    """File for this reference. Prefer the mentioned look, else the asset image."""
    from pathlib import Path

    from storage.assets import ImageStore
    from storage.blobs import BlobStore
    from storage.database import StorageError

    rows: list = []
    if look_id:
        rows = ImageStore.list(conn, asset_id=asset_id, look_id=look_id)
    if not rows:
        listed = ImageStore.list(conn, asset_id=asset_id)
        bare = [row for row in listed if not row.get("look_id")]
        rows = bare or listed
    for row in reversed(rows):
        digest = row.get("blob_hash")
        if not isinstance(digest, str) or not digest:
            continue
        try:
            info = BlobStore.info(conn, digest)
        except StorageError:
            continue
        path = info.get("path")
        if isinstance(path, str) and Path(path).is_file():
            return path
    return None


def _batch_provider_called(nodes: Any) -> bool:
    if not isinstance(nodes, list):
        return False
    return any(bool(item.get("provider_called")) for item in nodes if isinstance(item, dict))


def _metadata(node) -> dict:
    metadata = node.metadata if isinstance(getattr(node, "metadata", None), dict) else {}
    return metadata


def _dispatched(node) -> bool:
    return bool(_metadata(node).get(SUBMIT_DISPATCHED))


def _vendor_task_id(node) -> Optional[str]:
    task_id = _metadata(node).get("vendor_task_id")
    if isinstance(task_id, str) and task_id.strip():
        return task_id.strip()
    return None


def _vendor_hold(node) -> bool:
    """True when a later budget change must not start a second generation."""
    return _dispatched(node) or _vendor_task_id(node) is not None


def _force_new_pending(node) -> bool:
    return bool(_metadata(node).get("force_new_pending"))


def _request_was_not_sent(exc: BaseException) -> bool:
    """True when the vendor cannot have accepted a generation."""
    if isinstance(exc, FileNotFoundError):
        return True
    if type(exc).__name__ in {"UnsupportedParam", "SubmitNotSent"}:
        return True
    text = str(exc)
    return any(
        part in text
        for part in (
            "ARK_API_KEY not set",
            "reference image file is missing",
            "model is not registered",
        )
    )


def _vendor_judged_failed(exc: BaseException) -> bool:
    text = str(exc)
    return "视频生成failed" in text or "视频生成expired" in text


def _keep_dispatched_marker(exc: BaseException) -> bool:
    """Clear the marker only when this attempt never issued HTTP.

    Poll and download errors, vendor failed/expired, every 4xx, timeouts,
    and unknown exceptions keep the marker. A plain confirm must not POST
    again while it is set.
    """
    return not _request_was_not_sent(exc)


def _blocked_message(node) -> str:
    metadata = node.metadata if isinstance(getattr(node, "metadata", None), dict) else {}
    error = metadata.get("submit_error")
    if isinstance(error, str) and "force_new" in error:
        return VENDOR_FAILED_NOTE
    return SUBMIT_DISPATCHED_NOTE


def _ledger_view(entry: dict) -> dict:
    detail = entry["detail"] if isinstance(entry.get("detail"), dict) else None
    return {
        "entry_id": entry["id"],
        "this_quote": entry.get("quote_amount"),
        "quote": detail,
        "message": "",
    }


def _video_submit_params(
    project_id: str,
    node_id: str,
    fields: dict,
    prepared: Any,
    references: dict,
    spec,
) -> dict:
    """The prepared reference request, plus the prompt and a download path.

    Duration stays on the value the runner took from the model capability.
    A missing duration is filled from that same capability, including -1
    when the model allows auto. It is not replaced with a shared default.
    """
    from core.agents.video_agent import _capability_duration, _reference_asset_ids

    body = {}
    if isinstance(prepared, dict):
        body = {key: value for key, value in prepared.items() if key != "provider_called"}
    refs = body.get("reference_image_paths")
    if not isinstance(refs, list):
        refs = _reference_asset_ids(references)
    body["reference_image_paths"] = list(refs)
    if body.get("duration") is None:
        body["duration"] = _capability_duration(spec, fields.get("shot_duration"))
    if not body.get("generation_mode"):
        body["generation_mode"] = "reference" if body["reference_image_paths"] else "text"
    if not body.get("prompt"):
        body["prompt"] = fields.get("text") or ""
    if not body.get("model_id"):
        body["model_id"] = spec.id
    if not body.get("resolution"):
        body["resolution"] = fields.get("resolution") or spec.default_resolution
    body["save_path"] = _video_output_path(project_id, node_id)
    ratio = body.get("video_ratio") or body.get("ratio") or spec.default_ratio
    if ratio:
        body["ratio"] = ratio
        body["video_ratio"] = ratio
    return body


class _SaveRejected(Exception):
    """This node's compare-and-set lost. The savepoint rolls back."""


class _ClaimLost(Exception):
    """The submit stamp no longer matches. Do not write the node."""


class GraphExecutor:
    """Run one node. The runner is invoked only after the gate allows it.

    ``video_adapter`` is the submit implementation for an authorized video
    confirm. ``None`` uses the registered adapter when that provider's API
    key is configured.

    ``save_probe`` runs after ``_save`` reads stamps and before it writes,
    and only when this connection is not already inside a transaction.
    Tests use it to commit another connection's claim in that window.
    """

    save_probe = None

    def __init__(
        self,
        conn,
        runner: Optional[Callable[[dict], Any]] = None,
        video_adapter: Any = None,
    ) -> None:
        self.conn = conn
        self._runner = runner or _default_runner
        self._video_adapter = video_adapter
        self.submissions: list[str] = []
        self._claim_stamps: dict[str, str] = {}
        self._baselines: dict[int, dict[str, str]] = {}
        self._retrying = False
        self._force_new = False

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
            if node.kind in _STRUCTURAL:
                return self._run_structural(project_id, graph, node_id, node, params)
            return self._pack(
                project_id,
                graph,
                node_id,
                status="unsupported",
                gate="none",
                message="node has no billable modality",
            )
        fields = self._fields(node, params, modality)
        if modality == "video" and not fields["model_id"]:
            fields["model_id"] = DEFAULT_REFERENCE_VIDEO_MODEL
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

    def confirm(
        self,
        project_id: str,
        node_id: str,
        *,
        retry: bool = False,
        force_new: bool = False,
    ) -> dict:
        """Accept a held video quote. This is the first ledger write for that call.

        The node is claimed (``awaiting_user`` to ``running``) before
        authorize. The video adapter submits only after that reservation is
        allowed. Spent and done follow a successful submit. A failed submit
        returns the node to ``awaiting_user``. A plain confirm never POSTs
        while ``submit_dispatched`` is set. ``retry=True`` claims without
        clearing that marker. ``force_new`` is honored only together with
        retry. It releases the old quote, authorizes a new one, and drops
        ``vendor_task_id`` only after that quote exists.
        """
        if force_new and not retry:
            raise _GateError("force_new is only valid on retry_video_submit")
        graph = self._load(project_id)
        node_id = self._node_id(graph, node_id)
        node = graph.node(node_id)
        if retry and not _dispatched(node):
            raise _GateError("node has no interrupted submit to retry")
        if not retry and _dispatched(node) and node.status == NodeStatus.AWAITING_USER:
            raise _GateError(_blocked_message(node))
        if node.status not in {NodeStatus.AWAITING_USER, NodeStatus.RUNNING}:
            raise _GateError("node is not awaiting confirmation")
        fields = node.metadata.get("pending_call")
        if not isinstance(fields, dict):
            raise _GateError("node has no quote to confirm")
        existing = self._awaiting_entry(project_id, node_id)
        if existing is not None:
            return self._held_budget(project_id, graph, node_id, existing)
        self._retrying = retry
        self._force_new = bool(force_new and retry)
        try:
            quoted = self._open_quoted(project_id, node_id)
            state = self._node_state_row(project_id, node_id)
            # A quote left reserved by the same attempt is settled as-is.
            # ``force_new`` must not reuse it: that request is a new one.
            # ``ready`` is the raise_budget case.
            if (
                not self._force_new
                and quoted is not None
                and (state is None or state["status"] in {"ready", "awaiting_user", "running"})
            ):
                self._acquire_submit_claim(project_id, graph, node_id)
                try:
                    return self._finish(
                        project_id,
                        graph,
                        node_id,
                        fields,
                        _ledger_view(quoted),
                        call_confirmed=True,
                    )
                except Exception:
                    self._abandon_submit_claim(project_id, node_id)
                    raise
            self._acquire_submit_claim(project_id, graph, node_id)
            try:
                if self._force_new:
                    # The old reservation paid for the previous task. Release
                    # it before the new authorization, and do not drop the
                    # task id until that authorization succeeds.
                    self._release_quoted_reservation(project_id, node_id)
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
                    # authorize may already have moved node_state off the claim
                    # stamp. Write the graph back from this in-memory node.
                    # The task id and marker stay so a rejected budget cannot
                    # forget a generation that already exists.
                    if self._force_new and view.get("status") == AWAITING_USER:
                        graph.update_metadata(node_id, force_new_pending=True)
                    self._restore_awaiting(project_id, graph, node_id)
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
                if self._force_new:
                    self._drop_replaced_task(project_id, graph, node_id)
                return self._finish(
                    project_id,
                    graph,
                    node_id,
                    fields,
                    view,
                    call_confirmed=True,
                )
            except Exception:
                self._abandon_submit_claim(project_id, node_id)
                raise
        finally:
            self._retrying = False
            self._force_new = False

    def retry_video_submit(
        self,
        project_id: str,
        node_id: str,
        force_new: bool = False,
    ) -> dict:
        """Manual retry of a submit that may have reached the vendor.

        The marker stays set. A stored ``vendor_task_id`` is polled again
        and the original quote is spent, including when the claim is stale.
        A stored result URL is settled with no new POST. ``force_new=True``
        releases that quote, authorizes a new one, and only then drops the
        task id and POSTs. The default is False. A plain confirm never does
        either.
        """
        return self.confirm(project_id, node_id, retry=True, force_new=force_new)

    @staticmethod
    def recover_stale_submit_claims(conn) -> list[str]:
        """Release video submit claims left ``running`` by a dead process.

        A row older than ``VIDEO_SUBMIT_CLAIM_TTL_SECONDS`` is put back to
        ``awaiting_user`` and any unsettled quote is skipped. Call this on
        startup. ``confirm`` performs the same reclaim, so a process that
        never calls this still unsticks on the next confirm after the TTL.
        A claim newer than the TTL is left alone so a live submit is not
        interrupted.
        """
        cutoff = _claim_cutoff()
        rows = conn.execute(
            """
            SELECT project_id, node_id FROM node_state
            WHERE status = ? AND updated_at < ?
            ORDER BY project_id, node_id
            """,
            (NodeStatus.RUNNING.value, cutoff),
        ).fetchall()
        released: list[str] = []
        for row in rows:
            project_id = row["project_id"]
            node_id = row["node_id"]
            executor = GraphExecutor(conn)
            try:
                graph = executor._load(project_id)
            except _GateError:
                continue
            if node_id not in graph:
                continue
            node = graph.node(node_id)
            if node.kind not in _VIDEO and _modality(node, {}) != "video":
                continue
            if node.status not in {NodeStatus.RUNNING, NodeStatus.AWAITING_USER}:
                continue
            state = executor._node_state_row(project_id, node_id)
            if state is None or state["updated_at"] >= cutoff:
                continue
            executor._claim_stamps[node_id] = state["updated_at"]
            if _dispatched(node):
                executor._park_dispatched_attempt(project_id, graph, node_id)
            else:
                executor._release_quoted_reservation(project_id, node_id)
                executor._release_submit_claim(project_id, graph, node_id)
            released.append(node_id)
        return released

    def _acquire_submit_claim(self, project_id: str, graph: DependencyGraph, node_id: str) -> None:
        """Take the single in-flight submit for this node.

        ``BEGIN IMMEDIATE`` plus ``UPDATE ... WHERE status = 'awaiting_user'``
        lets one connection win. ``ready`` (left by ``raise_budget``) is
        claimable the same way. The other sees ``running`` and is rejected
        before authorize or ``submit``. A ``running`` row older than
        ``VIDEO_SUBMIT_CLAIM_TTL_SECONDS`` with no dispatched marker died
        before the vendor call: its unsettled quote is skipped and this
        confirm takes the node. A stale row that already has
        ``submit_dispatched`` is parked for a manual retry and is not taken.
        """
        now = utc_now()
        cutoff = _claim_cutoff()
        park = False
        park_stamp = None
        with transaction(self.conn):
            row = self._node_state_row(project_id, node_id)
            if row is not None and row["pinned"]:
                raise _GateError("node submit is already in progress")
            status = None if row is None else row["status"]
            updated_at = None if row is None else row["updated_at"]
            stale = (
                status == NodeStatus.RUNNING.value
                and isinstance(updated_at, str)
                and updated_at < cutoff
            )
            if stale and _dispatched(graph.node(node_id)) and not self._retrying:
                park = True
                park_stamp = updated_at
            elif status == NodeStatus.RUNNING.value and not stale:
                raise _GateError("node submit is already in progress")
            elif status not in (None, NodeStatus.AWAITING_USER.value, "ready") and not stale:
                raise _GateError("node is not awaiting confirmation")
            elif stale:
                # A retry keeps the reserved quote so it can resume or, for
                # force_new, release it only after this claim and before the
                # new authorization. A stale claim with no marker never
                # reached the vendor, so its quote is skipped here.
                if not self._retrying:
                    self._release_quoted_reservation(project_id, node_id)
                changed = self.conn.execute(
                    """
                    UPDATE node_state
                    SET status = ?, updated_at = ?
                    WHERE project_id = ? AND node_id = ? AND status = ?
                      AND pinned = 0 AND updated_at = ?
                    """,
                    (
                        NodeStatus.RUNNING.value,
                        now,
                        project_id,
                        node_id,
                        NodeStatus.RUNNING.value,
                        updated_at,
                    ),
                )
                if changed.rowcount != 1:
                    raise _GateError("node submit is already in progress")
                self._take_claimed_node(project_id, graph, node_id, now)
            elif status is None:
                self.conn.execute(
                    """
                    INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    """,
                    (project_id, node_id, NodeStatus.RUNNING.value, now),
                )
                self._take_claimed_node(project_id, graph, node_id, now)
            else:
                changed = self.conn.execute(
                    """
                    UPDATE node_state
                    SET status = ?, updated_at = ?
                    WHERE project_id = ? AND node_id = ? AND status = ? AND pinned = 0
                    """,
                    (
                        NodeStatus.RUNNING.value,
                        now,
                        project_id,
                        node_id,
                        status,
                    ),
                )
                if changed.rowcount != 1:
                    raise _GateError("node submit is already in progress")
                self._take_claimed_node(project_id, graph, node_id, now)
        if park:
            self._claim_stamps[node_id] = park_stamp
            self._park_dispatched_attempt(project_id, graph, node_id)
            raise _GateError(SUBMIT_DISPATCHED_NOTE)

    def _take_claimed_node(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        stamp: str,
    ) -> None:
        """Mark the node running and persist it in the claim transaction.

        The marker and task id stay. ``force_new`` drops the task id only
        after a new quote is authorized, in ``_drop_replaced_task``.
        """
        self._mark_submitting(graph, node_id)
        self._claim_stamps[node_id] = stamp
        self._save(
            project_id,
            graph,
            owned={node_id},
            touch_state=False,
            claim_node=node_id,
            claim_stamp=stamp,
        )
        self._remember_claim(project_id, node_id)

    def _drop_replaced_task(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
    ) -> None:
        """Forget the previous task after the new quote is reserved.

        The marker stays until the new attempt finishes. This write checks
        the claim stamp, so a lost claim cannot clear someone else's id.
        """
        graph.update_metadata(
            node_id,
            vendor_task_id=None,
            result_obtained=None,
            force_new_pending=None,
        )
        stamp = self._claim_stamps.get(node_id)
        self._save(
            project_id,
            graph,
            owned={node_id},
            touch_state=False,
            claim_node=node_id,
            claim_stamp=stamp,
        )

    def _remember_claim(self, project_id: str, node_id: str) -> None:
        """Store the ``updated_at`` ``_save`` actually wrote for this claim."""
        row = self._node_state_row(project_id, node_id)
        if row is not None and isinstance(row["updated_at"], str):
            self._claim_stamps[node_id] = row["updated_at"]

    def _mark_submitting(self, graph: DependencyGraph, node_id: str) -> None:
        status = graph.node(node_id).status
        if status == NodeStatus.RUNNING:
            return
        if status == NodeStatus.AWAITING_USER:
            graph.transition(node_id, NodeStatus.RUNNING)
            return
        raise _GateError("node is not awaiting confirmation")

    def _release_submit_claim(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        *,
        keep_dispatched: bool = False,
        note: Optional[str] = None,
    ) -> None:
        """Put this claim back to ``awaiting_user``.

        The write runs only when ``node_state.updated_at`` is still the stamp
        this claim stored. A later attempt that already moved the row is left
        untouched, including a node that is already done.
        """
        stamp = self._claim_stamps.get(node_id)
        with transaction(self.conn):
            row = self._node_state_row(project_id, node_id)
            if stamp is None or row is None:
                return
            if row["status"] != NodeStatus.RUNNING.value or row["updated_at"] != stamp:
                return
            status = graph.node(node_id).status
            if status == NodeStatus.DONE:
                return
            if status == NodeStatus.RUNNING:
                graph.transition(node_id, NodeStatus.AWAITING_USER)
            if note is not None:
                graph.update_metadata(node_id, submit_error=note)
            if not keep_dispatched:
                graph.update_metadata(node_id, submit_dispatched=None, vendor_task_id=None)
            try:
                self._save(
                    project_id,
                    graph,
                    owned={node_id},
                    claim_node=node_id,
                    claim_stamp=stamp,
                )
            except _ClaimLost:
                return

    def _park_dispatched_attempt(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
    ) -> None:
        """Stop a stale dispatched submit without calling the vendor again.

        The reserved quote stays. A retry resumes that task and spends it,
        instead of skipping it and leaving the generation unbooked.
        """
        self._release_submit_claim(
            project_id,
            graph,
            node_id,
            keep_dispatched=True,
            note=SUBMIT_DISPATCHED_NOTE,
        )

    def _restore_awaiting(self, project_id: str, graph: DependencyGraph, node_id: str) -> None:
        """Write the in-memory node back to ``awaiting_user`` after a budget pause."""
        status = graph.node(node_id).status
        if status == NodeStatus.DONE:
            return
        if status == NodeStatus.RUNNING:
            graph.transition(node_id, NodeStatus.AWAITING_USER)
        self._save(project_id, graph, owned={node_id}, resume_pause=True)

    def _abandon_submit_claim(self, project_id: str, node_id: str) -> None:
        """Reload the graph, then release only if this claim still owns the row.

        A marker that survived the failed attempt is kept. Abandon does not
        clear it, so a later plain confirm still cannot POST.
        """
        try:
            fresh = self._load(project_id)
        except _GateError:
            return
        if node_id not in fresh:
            return
        self._release_submit_claim(
            project_id,
            fresh,
            node_id,
            keep_dispatched=_dispatched(fresh.node(node_id)),
        )

    def _persist_dispatched_marker(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
    ) -> None:
        """Commit ``submit_dispatched`` before the vendor call.

        ``node_state.updated_at`` stays on the claim stamp so a failure can
        still compare-and-save. The marker is durable if the process dies
        during the poll.
        """
        graph.update_metadata(node_id, submit_dispatched=True)
        stamp = self._claim_stamps.get(node_id)
        self._save(
            project_id,
            graph,
            owned={node_id},
            touch_state=False,
            claim_node=node_id,
            claim_stamp=stamp,
        )

    def _node_state_row(self, project_id: str, node_id: str):
        return self.conn.execute(
            """
            SELECT status, updated_at, pinned FROM node_state
            WHERE project_id = ? AND node_id = ?
            """,
            (project_id, node_id),
        ).fetchone()

    def _open_quoted(self, project_id: str, node_id: str) -> Optional[dict]:
        rows = [
            entry
            for entry in LedgerStore.list(self.conn, project_id)
            if entry["node_id"] == node_id
            and entry["status"] == QUOTED
            and entry["actual_amount"] is None
        ]
        if not rows:
            return None
        return rows[-1]

    def _claim_still_owned(self, project_id: str, node_id: str, entry_id: str) -> bool:
        """True when this claim's stamp and reserved quote are still the live ones."""
        stamp = self._claim_stamps.get(node_id)
        row = self._node_state_row(project_id, node_id)
        if stamp is None or row is None:
            return False
        if row["status"] != NodeStatus.RUNNING.value or row["updated_at"] != stamp:
            return False
        try:
            entry = LedgerStore.get(self.conn, entry_id)
        except StorageError:
            return False
        if entry.get("node_id") != node_id or entry.get("status") != QUOTED:
            return False
        return entry.get("actual_amount") is None

    def _keep_orphaned_result(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        result: Any,
        view: dict,
    ) -> dict:
        """The vendor returned, but this claim no longer owns the node."""
        entry_id = view.get("entry_id")
        if isinstance(entry_id, str):
            try:
                entry = LedgerStore.get(self.conn, entry_id)
            except StorageError:
                entry = None
            if (
                isinstance(entry, dict)
                and entry.get("node_id") == node_id
                and entry.get("status") == QUOTED
                and entry.get("actual_amount") is None
            ):
                LedgerStore.update(self.conn, entry_id, status=SKIPPED)
        row = self._node_state_row(project_id, node_id)
        already_done = row is not None and row["status"] == NodeStatus.DONE.value
        if not already_done and graph.node(node_id).status == NodeStatus.RUNNING:
            graph.transition(node_id, NodeStatus.AWAITING_USER)
        public = {}
        url = None
        if isinstance(result, dict):
            public = {key: value for key, value in result.items() if key != "provider_called"}
            url = public.get("result_id")
        graph.update_metadata(
            node_id,
            result=public or None,
            notes=ORPHAN_RESULT_NOTE,
            submit_error=ORPHAN_RESULT_NOTE,
            submit_dispatched=True,
        )
        if not already_done:
            self._save(project_id, graph, owned={node_id}, lost_claim=True)
        body = self._pack(
            project_id,
            graph,
            node_id,
            status="done" if already_done else "awaiting_user",
            gate="none",
            quote=view.get("quote"),
            entry_id=entry_id if isinstance(entry_id, str) else None,
            message=ORPHAN_RESULT_NOTE,
            submitted=False,
            spent=False,
            call_confirmed=True,
            provider_called=True,
        )
        if isinstance(url, str):
            body["result_id"] = url
        return body

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
        entry = LedgerStore.get(self.conn, entry_id)
        node_id = entry.get("node_id")
        graph = self._load(project_id)
        node = graph.node(node_id) if node_id and node_id in graph else None
        # A force_new pause still holds the previous task until the new quote
        # is reserved. Raising the cap is that reservation, so it must not
        # deadlock on the marker. Any other hold refuses.
        pending_force_new = node is not None and _force_new_pending(node)
        if node is not None and _vendor_hold(node) and not pending_force_new:
            raise _GateError(_blocked_message(node))
        view = ProjectBudget.raise_cap(self.conn, project_id, entry_id, new_cap)
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
        if fields.get("modality") == "video" or graph.node(node_id).kind in _VIDEO:
            if pending_force_new:
                self._force_new = True
            try:
                self._acquire_submit_claim(project_id, graph, node_id)
                try:
                    if pending_force_new:
                        self._drop_replaced_task(project_id, graph, node_id)
                    return self._finish(
                        project_id,
                        graph,
                        node_id,
                        fields,
                        view,
                        call_confirmed=True,
                    )
                except Exception:
                    self._abandon_submit_claim(project_id, node_id)
                    raise
            finally:
                self._force_new = False
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
            "provider_called": _batch_provider_called(summary.get("nodes")),
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
            ) from None
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
                raise _ImpactFailure(summary) from None
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
        body["provider_called"] = _batch_provider_called(summary.get("nodes"))
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
        result = self._submit(
            {
                "project_id": project_id,
                "node_id": node_id,
                "kind": graph.node(node_id).kind,
                "modality": fields.get("modality"),
                "model_id": fields.get("model_id"),
                "quantity": quantity,
                "duration": duration,
                "shot_duration": fields.get("shot_duration"),
                "resolution": fields.get("resolution"),
                "text": fields.get("text") or "",
                "asset_id": fields.get("asset_id"),
                "references": references,
                "versions": versions,
                "provider_called": False,
            }
        )
        provider_called = False
        if fields.get("modality") == "video" and call_confirmed:
            result, provider_called = self._complete_video_submit(
                project_id,
                graph,
                node_id,
                fields,
                result,
                references,
            )
        self._store_result(graph, node_id, result)
        amount = view.get("this_quote")
        if amount is None:
            raise _GateError("reserved quote has no amount")
        obtained = None
        if isinstance(result, dict):
            candidate = result.get("result_id")
            if isinstance(candidate, str) and _looks_like_url_or_path(candidate.strip()):
                obtained = candidate.strip()
        if obtained is None:
            obtained = _submit_ref(result)
        video_claim = (
            fields.get("modality") == "video"
            and call_confirmed
            and node_id in self._claim_stamps
        )
        if video_claim and not self._claim_still_owned(project_id, node_id, view["entry_id"]):
            return self._keep_orphaned_result(project_id, graph, node_id, result, view)
        # The URL is committed before spent/done. If the spend transaction
        # rolls back, the marker and this URL stay, and the quote stays reserved.
        if video_claim and provider_called and isinstance(obtained, str):
            graph.update_metadata(
                node_id,
                result_obtained=obtained,
                submit_dispatched=True,
            )
            self._save(
                project_id,
                graph,
                owned={node_id},
                touch_state=False,
                claim_node=node_id,
                claim_stamp=self._claim_stamps.get(node_id),
            )
        # Spent and done become visible together. The vendor call already
        # returned, so this transaction does not hold the claim across poll.
        with transaction(self.conn):
            recorded = ProjectBudget.record_actual(
                self.conn,
                project_id,
                view["entry_id"],
                amount,
            )
            self._to_done(graph, node_id)
            graph.update_metadata(
                node_id,
                pending_call=None,
                submit_error=None,
                submit_dispatched=None,
                vendor_task_id=None,
            )
            stamp = self._claim_stamps.get(node_id)
            if video_claim and isinstance(stamp, str):
                self._save(
                    project_id,
                    graph,
                    claim_node=node_id,
                    claim_stamp=stamp,
                )
            else:
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
            provider_called=provider_called,
        )
        body["actual"] = recorded.get("actual")
        if isinstance(result, dict) and result.get("result_id"):
            body["result_id"] = result["result_id"]
        return body

    def _video_submitter(self, model_id: str):
        """Injected adapter, or the registered one when its key is set."""
        spec = ModelRegistry.get(model_id) if model_id else None
        if self._video_adapter is not None:
            return self._video_adapter, spec
        if spec is None or not _provider_configured(getattr(spec, "provider", "")):
            return None, spec
        adapter = ModelRegistry.adapter_for(spec, "video")
        if adapter is None or not getattr(adapter, "owns_submit", False):
            return None, spec
        return adapter, spec

    def _complete_video_submit(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        fields: dict,
        prepared: Any,
        references: dict,
    ) -> tuple:
        """Call ``adapter.submit`` after authorize. Failure releases the quote.

        Submit blocks until the adapter has polled and downloaded. The
        returned value is the remote URL (or a local path). It is stored as
        ``result_id``. ``save_path`` on the same result is the local file
        the adapter was asked to write. Reference ids are resolved to image
        files here, and only here, so the no-key dispatcher result keeps
        asset ids.
        """
        adapter, spec = self._video_submitter(_text(fields.get("model_id")))
        if adapter is None or spec is None:
            return prepared, False
        if isinstance(prepared, dict):
            error = prepared.get("error")
            if isinstance(error, str) and error.strip():
                self._abort_reserved_video(project_id, graph, node_id, RuntimeError(error.strip()))
        params = _video_submit_params(project_id, node_id, fields, prepared, references, spec)
        try:
            params = self._with_reference_files(params, references)
        except Exception as exc:
            self._abort_reserved_video(project_id, graph, node_id, exc)
        meta = graph.node(node_id).metadata
        if not isinstance(meta, dict):
            meta = {}
        if not self._force_new:
            stored = meta.get("result_obtained")
            if isinstance(stored, str) and _looks_like_url_or_path(stored.strip()):
                merged = dict(prepared) if isinstance(prepared, dict) else {}
                merged["provider_called"] = True
                merged["result_id"] = stored.strip()
                merged["model_id"] = spec.id
                merged["generation_mode"] = params.get("generation_mode")
                merged["duration"] = params.get("duration")
                return merged, True
            task_id = meta.get("vendor_task_id")
            if isinstance(task_id, str) and task_id.strip():
                params["vendor_task_id"] = task_id.strip()
        # Commit the marker before the vendor call. A kill during poll leaves
        # it set so a later confirm does not submit a second time. Retry does
        # not clear it first; this write only adds it when it is missing.
        if not _dispatched(graph.node(node_id)):
            self._persist_dispatched_marker(project_id, graph, node_id)

        def _on_task(task_id: str) -> None:
            if not isinstance(task_id, str) or not task_id.strip():
                return
            graph.update_metadata(node_id, vendor_task_id=task_id.strip(), submit_dispatched=True)
            self._save(
                project_id,
                graph,
                owned={node_id},
                touch_state=False,
                claim_node=node_id,
                claim_stamp=self._claim_stamps.get(node_id),
            )

        params["on_task"] = _on_task
        try:
            vendor = adapter.submit(spec, params)
        except Exception as exc:
            self._abort_reserved_video(project_id, graph, node_id, exc)
        ref = _submit_ref(vendor)
        if ref is None:
            self._abort_reserved_video(
                project_id,
                graph,
                node_id,
                RuntimeError("video submit returned no result"),
            )
        merged = dict(prepared) if isinstance(prepared, dict) else {}
        merged["provider_called"] = True
        merged["result_id"] = ref
        merged["model_id"] = spec.id
        merged["generation_mode"] = params.get("generation_mode")
        merged["duration"] = params.get("duration")
        if params.get("save_path"):
            merged["save_path"] = params["save_path"]
        return merged, True

    def _with_reference_files(self, params: dict, references: Any) -> dict:
        """Replace asset ids with on-disk image files. Never pass an id through."""
        from pathlib import Path

        asset_ids = params.get("reference_image_paths") or []
        if not isinstance(asset_ids, list) or not asset_ids:
            return params
        looks = _look_by_asset(references)
        files: list[str] = []
        for token in asset_ids:
            if not isinstance(token, str) or not token:
                raise RuntimeError("reference image file is missing")
            if Path(token).is_file():
                files.append(token)
                continue
            path = _image_disk_path(self.conn, token, looks.get(token))
            if path is None:
                raise RuntimeError("reference image file is missing")
            files.append(path)
        updated = dict(params)
        updated["reference_image_paths"] = files
        return updated

    def _abort_reserved_video(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        exc: BaseException,
    ) -> NoReturn:
        """Skip the reserved quote, drop the claim, and surface the error."""
        self._release_quoted_reservation(project_id, node_id)
        message = _redact_secret(str(exc).strip() or exc.__class__.__name__)
        keep = _keep_dispatched_marker(exc)
        vendor_failed = _vendor_judged_failed(exc)
        if vendor_failed:
            message = VENDOR_FAILED_NOTE
        elif keep:
            message = f"{message}; {SUBMIT_DISPATCHED_NOTE}"
        graph.update_metadata(node_id, submit_error=message)
        self._release_submit_claim(project_id, graph, node_id, keep_dispatched=keep)
        if vendor_failed:
            raise _GateError(message) from None
        raise _GateError(f"video submit failed: {message}") from None

    def _submit(self, call: dict) -> Any:
        self.submissions.append(call["node_id"])
        result = self._runner(call)
        if isinstance(result, dict) and result.get("provider_called") is True:
            raise _GateError("node runner called a provider")
        return result

    def _store_result(self, graph: DependencyGraph, node_id: str, result: Any) -> None:
        if not isinstance(result, dict):
            return
        public = {key: value for key, value in result.items() if key != "provider_called"}
        if public:
            graph.update_metadata(node_id, result=public)

    def _run_structural(self, project_id: str, graph: DependencyGraph, node_id: str, node, params: dict) -> dict:
        text = _text(params.get("text")) or _text(node.metadata.get("text") if isinstance(node.metadata, dict) else "")
        result = self._submit(
            {
                "project_id": project_id,
                "node_id": node_id,
                "kind": node.kind,
                "modality": "",
                "model_id": _text(params.get("model_id") or (node.metadata.get("model_id") if isinstance(node.metadata, dict) else "")),
                "text": text,
                "provider_called": False,
            }
        )
        self._to_done(graph, node_id)
        self._store_result(graph, node_id, result)
        self._save(project_id, graph)
        body = self._pack(
            project_id,
            graph,
            node_id,
            status="done",
            gate="none",
            submitted=True,
            spent=False,
            call_confirmed=False,
        )
        if isinstance(result, dict):
            body["result"] = {key: value for key, value in result.items() if key != "provider_called"}
        return body

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
            "resolution": _stored(params, node.metadata, "resolution"),
            "includes_video_input": _stored(params, node.metadata, "includes_video_input"),
            "asset_count": asset_count,
            "quantity": quantity,
            "unit": params.get("unit"),
            "completion_tokens": _stored(params, node.metadata, "completion_tokens"),
            "input_tokens": _stored(params, node.metadata, "input_tokens"),
            "output_tokens": _stored(params, node.metadata, "output_tokens"),
            "text": _text(params.get("text")) or _text(node.metadata.get("text") if isinstance(node.metadata, dict) else ""),
            "shot_duration": node.metadata.get("shot_duration") if modality == "video" and isinstance(node.metadata, dict) else None,
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
        provider_called: bool = False,
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
            "provider_called": provider_called,
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
        graph = DependencyGraph.from_dict(document)
        self._baselines[id(graph)] = {
            node_id: dump_json(graph.node(node_id).to_dict())
            for node_id in graph.topological_order()
        }
        return graph

    def _changed_nodes(self, graph: DependencyGraph) -> list[str]:
        """Nodes this in-memory graph changed after ``_load``."""
        base = self._baselines.get(id(graph))
        order = list(graph.topological_order())
        if base is None:
            return order
        return [
            node_id
            for node_id in order
            if dump_json(graph.node(node_id).to_dict()) != base.get(node_id)
        ]

    def _save(
        self,
        project_id: str,
        graph: DependencyGraph,
        *,
        owned: Optional[set] = None,
        touch_state: bool = True,
        claim_node: Optional[str] = None,
        claim_stamp: Optional[str] = None,
        lost_claim: bool = False,
        resume_pause: bool = False,
    ) -> None:
        """Write only nodes this operation owns, one compare-and-set each.

        The graph document is never replaced. Each owned node is patched with
        ``json_set`` on that array element, in the same transaction as the
        ``node_state`` predicate. A node that is running in memory but already
        carries ``submit_dispatched`` in the database is not a target unless
        this call owns the claim stamp. ``save_probe`` runs after the stamp
        read and before the write when this connection is not in a transaction.
        """
        if owned is not None:
            targets = [node_id for node_id in owned if node_id in graph]
        else:
            targets = self._changed_nodes(graph)
        if not targets:
            return
        selected: dict[str, Optional[str]] = {}
        for node_id in targets:
            row = self._node_state_row(project_id, node_id)
            selected[node_id] = None if row is None else row["updated_at"]
        probe = GraphExecutor.save_probe
        if probe is not None and not self.conn.in_transaction:
            probe(self)
        for node_id in targets:
            self._persist_owned_node(
                project_id,
                graph,
                node_id,
                touch_state=touch_state,
                claim_node=claim_node,
                claim_stamp=claim_stamp,
                selected_stamp=selected.get(node_id),
                lost_claim=lost_claim,
                resume_pause=resume_pause,
            )

    def _persist_owned_node(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        *,
        touch_state: bool,
        claim_node: Optional[str],
        claim_stamp: Optional[str],
        selected_stamp: Optional[str],
        lost_claim: bool,
        resume_pause: bool,
    ) -> None:
        try:
            with transaction(self.conn):
                self._write_owned_node(
                    project_id,
                    graph,
                    node_id,
                    touch_state=touch_state,
                    claim_node=claim_node,
                    claim_stamp=claim_stamp,
                    selected_stamp=selected_stamp,
                    lost_claim=lost_claim,
                    resume_pause=resume_pause,
                )
        except _SaveRejected:
            return

    def _write_owned_node(
        self,
        project_id: str,
        graph: DependencyGraph,
        node_id: str,
        *,
        touch_state: bool,
        claim_node: Optional[str],
        claim_stamp: Optional[str],
        selected_stamp: Optional[str],
        lost_claim: bool,
        resume_pause: bool,
    ) -> None:
        require_id(project_id, "project_id")
        require_id(node_id, "node_id")
        node = graph.node(node_id)
        status = node.status.value
        payload = dump_json(node.to_dict())
        now = utc_now()
        is_claim = claim_node == node_id and isinstance(claim_stamp, str)
        if claim_node == node_id and not isinstance(claim_stamp, str):
            raise _ClaimLost()
        if is_claim:
            if touch_state:
                changed = self.conn.execute(
                    """
                    UPDATE node_state
                    SET status = ?, updated_at = ?
                    WHERE project_id = ? AND node_id = ? AND updated_at = ? AND pinned = 0
                    """,
                    (status, now, project_id, node_id, claim_stamp),
                )
            else:
                changed = self.conn.execute(
                    """
                    UPDATE node_state
                    SET updated_at = updated_at
                    WHERE project_id = ? AND node_id = ? AND updated_at = ? AND pinned = 0
                    """,
                    (project_id, node_id, claim_stamp),
                )
            if changed.rowcount != 1 or self._patch_node_json(project_id, node_id, payload, owner=True) != 1:
                raise _ClaimLost()
            if touch_state:
                self._claim_stamps[node_id] = now
            return
        if lost_claim:
            changed = self.conn.execute(
                """
                UPDATE node_state
                SET status = ?, updated_at = ?
                WHERE project_id = ? AND node_id = ? AND status = ? AND pinned = 0
                """,
                (status, now, project_id, node_id, NodeStatus.RUNNING.value),
            )
            if changed.rowcount != 1 or self._patch_node_json(project_id, node_id, payload, owner=True) != 1:
                raise _SaveRejected()
            return
        if resume_pause:
            if not isinstance(selected_stamp, str):
                inserted = self._insert_node_state(project_id, node_id, status, now)
                if inserted != 1 or self._patch_node_json(project_id, node_id, payload, owner=True) != 1:
                    raise _SaveRejected()
                return
            changed = self.conn.execute(
                """
                UPDATE node_state
                SET status = ?, updated_at = ?
                WHERE project_id = ? AND node_id = ? AND updated_at = ? AND pinned = 0
                """,
                (status, now, project_id, node_id, selected_stamp),
            )
            if changed.rowcount != 1 or self._patch_node_json(project_id, node_id, payload, owner=True) != 1:
                raise _SaveRejected()
            return
        if selected_stamp is None:
            if self._insert_node_state(project_id, node_id, status, now) != 1:
                raise _SaveRejected()
            if self._patch_node_json(project_id, node_id, payload, owner=False) != 1:
                raise _SaveRejected()
            return
        changed = self.conn.execute(
            """
            UPDATE node_state
            SET status = ?, updated_at = ?
            WHERE project_id = ? AND node_id = ? AND updated_at = ? AND pinned = 0
              AND status != 'running'
            """,
            (status, now, project_id, node_id, selected_stamp),
        )
        if changed.rowcount != 1 or self._patch_node_json(project_id, node_id, payload, owner=False) != 1:
            raise _SaveRejected()

    def _insert_node_state(self, project_id: str, node_id: str, status: str, now: str) -> int:
        inserted = self.conn.execute(
            """
            INSERT INTO node_state (project_id, node_id, status, pinned, updated_at)
            SELECT ?, ?, ?, 0, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM node_state WHERE project_id = ? AND node_id = ?
            )
            """,
            (project_id, node_id, status, now, project_id, node_id),
        )
        return inserted.rowcount

    def _patch_node_json(self, project_id: str, node_id: str, payload: str, *, owner: bool) -> int:
        """Replace one node object. ``owner`` may overwrite a running or marked node."""
        if owner:
            guard = "1 = 1"
        else:
            guard = """
                COALESCE(json_extract(e.value, '$.status'), '') != 'running'
                AND COALESCE(json_extract(e.value, '$.metadata.submit_dispatched'), 0) = 0
            """
        updated = self.conn.execute(
            f"""
            UPDATE project
            SET graph_json = (
              SELECT json_set(
                project.graph_json,
                '$.nodes[' || e.key || ']',
                json(?)
              )
              FROM json_each(project.graph_json, '$.nodes') AS e
              WHERE json_extract(e.value, '$.id') = ?
                AND ({guard})
            ),
            updated_at = ?
            WHERE id = ?
              AND EXISTS (
                SELECT 1 FROM json_each(project.graph_json, '$.nodes') AS e
                WHERE json_extract(e.value, '$.id') = ?
                  AND ({guard})
              )
            """,
            (payload, node_id, utc_now(), project_id, node_id),
        )
        return updated.rowcount

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
