"""Project API for one-node runs, video confirm, and the budget gate.

Routes call :class:`core.executor.GraphExecutor`. Video submit happens
inside the executor after confirm authorizes, and only when a video
adapter is injected or the model's provider key is configured.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.routers.library import bind_db, get_library_connection
from api.schemas.executor import (
    BudgetRaiseRequest,
    ConfirmRequest,
    EpisodeProjectCreate,
    ImpactConfirmRequest,
    ImpactRegenerateRequest,
    ImpactRequest,
    ModelUpdateRequest,
    NodeRunRequest,
    PromptUpdateRequest,
    RetryVideoSubmitRequest,
)
from core.episode import seed_episode_graph
from core.executor import GraphExecutor, _ClaimLost, _VIDEO
from graph.dependency_graph import DependencyGraph
from graph.errors import GraphError, NodeNotFoundError
from graph.model import NodeStatus
from storage.database import (
    LookNotFoundError,
    ProjectNotFoundError,
    StorageError,
    StoreConnection,
)
from storage.ledger import LedgerStore
from storage.projects import GraphStore, ProjectStore

router = APIRouter(tags=["Executor"], dependencies=[Depends(get_library_connection)])

_FORCE_NEW_REJECTED = "force_new is only valid on retry_video_submit"
_NO_VENDOR = "未配置视频厂商密钥，不会提交也不会入账"
_CLAIM_MOVED = "状态已变化，请刷新"
_DELETE_BUSY = "项目有尚未结束的提交，不能删除"


@contextmanager
def _errors():
    try:
        yield
    except _ClaimLost:
        raise HTTPException(409, _CLAIM_MOVED) from None
    except NodeNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LookNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (GraphError, StorageError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _held_submit(node) -> bool:
    meta = node.metadata if isinstance(getattr(node, "metadata", None), dict) else {}
    if meta.get("submit_dispatched"):
        return True
    task_id = meta.get("vendor_task_id")
    return isinstance(task_id, str) and bool(task_id.strip())


def _refuse_if_no_vendor(executor: GraphExecutor, project_id: str, node_id: str) -> None:
    """Reject a video confirm when this process cannot call a vendor.

    Direct ``GraphExecutor.confirm`` is unchanged. Only these HTTP routes
    refuse, and they do so before authorize, so the held quote stays put.
    """
    try:
        graph = executor._load(project_id)
    except (GraphError, StorageError):
        return
    if node_id not in graph:
        return
    node = graph.node(node_id)
    meta = node.metadata if isinstance(node.metadata, dict) else {}
    pending = meta.get("pending_call") if isinstance(meta.get("pending_call"), dict) else {}
    modality = pending.get("modality") if isinstance(pending.get("modality"), str) else ""
    if modality != "video" and node.kind not in _VIDEO:
        return
    model_id = pending.get("model_id") or meta.get("model_id") or ""
    if not isinstance(model_id, str):
        model_id = ""
    adapter, _spec = executor._video_submitter(model_id.strip())
    if adapter is None:
        raise HTTPException(409, _NO_VENDOR)


def _delete_blocked(conn: StoreConnection, project_id: str) -> bool:
    try:
        document = GraphStore.load(conn, project_id)
    except ProjectNotFoundError:
        return False
    if isinstance(document, dict) and "nodes" in document:
        graph = DependencyGraph.from_dict(document)
        for node_id in graph.topological_order():
            node = graph.node(node_id)
            if node.status == NodeStatus.RUNNING or _held_submit(node):
                return True
    row = conn.execute(
        "SELECT 1 FROM node_state WHERE project_id = ? AND status = ? LIMIT 1",
        (project_id, NodeStatus.RUNNING.value),
    ).fetchone()
    return row is not None


def _executor(conn: StoreConnection) -> GraphExecutor:
    return GraphExecutor(conn)


def _run_body(body: NodeRunRequest | None) -> dict:
    if body is None:
        return {}
    return body.model_dump()


def _graph_view(document: dict) -> dict:
    graph = DependencyGraph.from_dict(document)
    return {
        "graph": document,
        "nodes": [graph.node(node_id).to_dict() for node_id in graph.topological_order()],
        "edges": list(document.get("edges") or []),
    }


@router.post("/api/v2/projects")
@bind_db
def create_episode_project(
    body: EpisodeProjectCreate,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Create a project and store its one-episode dependency graph."""
    with _errors():
        project = ProjectStore.create(conn, body.name, budget_cap=body.budget_cap)
        try:
            document = seed_episode_graph(
                conn,
                project["id"],
                idea=body.idea or "",
                models=body.model_dump(),
            )
        except Exception:
            ProjectStore.delete(conn, project["id"])
            raise
        return {"project": project, "episode_id": "ep1", **_graph_view(document)}


@router.delete("/api/v2/projects/{project_id}")
@bind_db
def delete_project(
    project_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        if _delete_blocked(conn, project_id):
            raise HTTPException(409, _DELETE_BUSY)
        removed = ProjectStore.delete(conn, project_id)
    if not removed:
        raise HTTPException(404, "project not found")
    return {"status": "deleted", "id": project_id}


@router.post("/api/v2/projects/{project_id}/stop")
@bind_db
def stop_project(
    project_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        body = _executor(conn).stop(project_id)
    if body["refused"] and not body["stopped"]:
        reasons = "；".join(item["reason"] for item in body["refused"])
        raise HTTPException(409, f"{body['message']}：{reasons}")
    return body


@router.patch("/api/v2/projects/{project_id}/models")
@bind_db
def update_models(
    project_id: str,
    body: ModelUpdateRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    roles = ("llm_model", "image_model", "video_model", "tts_model")
    updates = {name: getattr(body, name) for name in roles if name in body.model_fields_set}
    with _errors():
        return _executor(conn).update_models(project_id, updates, body.nodes)


@router.patch("/api/v2/projects/{project_id}/nodes/{node_id}/prompt")
@bind_db
def update_prompt(
    project_id: str,
    node_id: str,
    body: PromptUpdateRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).update_prompt(project_id, node_id, body.text)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/run")
@bind_db
def run_node(
    project_id: str,
    node_id: str,
    body: NodeRunRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).run_node(project_id, node_id, _run_body(body))


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/confirm")
@bind_db
def confirm_node(
    project_id: str,
    node_id: str,
    force_new: Optional[str] = Query(None),
    body: ConfirmRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Accept a held quote.

    ``force_new`` is never accepted here. If the field appears in the query
    string or the JSON body, including ``false``, the route returns 400 and
    does not confirm. Omitting the field is a normal confirm.
    """
    if force_new is not None or (body is not None and "force_new" in body.model_fields_set):
        raise HTTPException(400, _FORCE_NEW_REJECTED)
    with _errors():
        executor = _executor(conn)
        _refuse_if_no_vendor(executor, project_id, node_id)
        return executor.confirm(project_id, node_id)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/retry_video_submit")
@bind_db
def retry_video_submit(
    project_id: str,
    node_id: str,
    body: RetryVideoSubmitRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Retry a submit that may have reached the vendor. ``force_new`` defaults to false."""
    force_new = False if body is None else bool(body.force_new)
    with _errors():
        executor = _executor(conn)
        _refuse_if_no_vendor(executor, project_id, node_id)
        return executor.retry_video_submit(project_id, node_id, force_new=force_new)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/decline")
@bind_db
def decline_node(
    project_id: str,
    node_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).decline_node(project_id, node_id)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/regenerate")
@bind_db
def regenerate_node(
    project_id: str,
    node_id: str,
    body: NodeRunRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).regenerate(project_id, node_id, _run_body(body))


@router.post("/api/v2/projects/{project_id}/budget/{entry_id}/raise")
@bind_db
def raise_budget(
    project_id: str,
    entry_id: str,
    body: BudgetRaiseRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        entry = LedgerStore.get(conn, entry_id)
        executor = _executor(conn)
        node_id = entry.get("node_id")
        if isinstance(node_id, str) and node_id.strip():
            _refuse_if_no_vendor(executor, project_id, node_id.strip())
        return executor.raise_budget(project_id, entry_id, body.budget_cap)


@router.post("/api/v2/projects/{project_id}/budget/{entry_id}/decline")
@bind_db
def decline_budget(
    project_id: str,
    entry_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).decline_budget(project_id, entry_id)


@router.get("/api/v2/projects/{project_id}/status")
@bind_db
def project_status(
    project_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).status(project_id)


@router.post("/api/v2/projects/{project_id}/looks/{look_id}/impact")
@bind_db
def impact_quote(
    project_id: str,
    look_id: str,
    body: ImpactRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    payload = body or ImpactRequest()
    with _errors():
        return _executor(conn).impact(
            project_id,
            look_id,
            model=payload.model,
            unit_cost=payload.unit_cost,
        )


@router.post("/api/v2/projects/{project_id}/looks/{look_id}/impact/confirm")
@bind_db
def impact_confirm(
    project_id: str,
    look_id: str,
    body: ImpactConfirmRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).confirm_impact(
            project_id,
            look_id,
            body.confirm_token,
            model=body.model,
            unit_cost=body.unit_cost,
        )


@router.post("/api/v2/projects/{project_id}/impact/regenerate")
@bind_db
def impact_regenerate(
    project_id: str,
    body: ImpactRegenerateRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Regenerate nodes from a confirmed impact set. Does not call a provider."""
    params = body.model_dump(exclude={"confirm_token", "node_ids"})
    with _errors():
        return _executor(conn).regenerate_impact(
            project_id,
            body.confirm_token,
            body.node_ids,
            params,
        )
