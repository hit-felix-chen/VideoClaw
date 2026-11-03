"""Project API for one-node runs, video confirm, and the budget gate.

Routes call :class:`core.executor.GraphExecutor`. Video submit happens
inside the executor after confirm authorizes, and only when a video
adapter is injected or the model's provider key is configured.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query

from api.routers.library import get_library_connection
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
from core.executor import GraphExecutor
from graph.dependency_graph import DependencyGraph
from graph.errors import GraphError, NodeNotFoundError
from storage.database import (
    LookNotFoundError,
    ProjectNotFoundError,
    StorageError,
    StoreConnection,
)
from storage.projects import ProjectStore

router = APIRouter(tags=["Executor"])

_FORCE_NEW_REJECTED = "force_new is only valid on retry_video_submit"


@contextmanager
def _errors():
    try:
        yield
    except NodeNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LookNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (GraphError, StorageError) as exc:
        raise HTTPException(400, str(exc)) from exc


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
def delete_project(
    project_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        removed = ProjectStore.delete(conn, project_id)
    if not removed:
        raise HTTPException(404, "project not found")
    return {"status": "deleted", "id": project_id}


@router.post("/api/v2/projects/{project_id}/stop")
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
def update_prompt(
    project_id: str,
    node_id: str,
    body: PromptUpdateRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).update_prompt(project_id, node_id, body.text)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/run")
def run_node(
    project_id: str,
    node_id: str,
    body: NodeRunRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).run_node(project_id, node_id, _run_body(body))


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/confirm")
def confirm_node(
    project_id: str,
    node_id: str,
    force_new: bool = Query(False),
    body: ConfirmRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Accept a held quote. ``force_new`` is not honored on this route."""
    if force_new or (body is not None and body.force_new):
        raise HTTPException(400, _FORCE_NEW_REJECTED)
    with _errors():
        return _executor(conn).confirm(project_id, node_id)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/retry_video_submit")
def retry_video_submit(
    project_id: str,
    node_id: str,
    body: RetryVideoSubmitRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Retry a submit that may have reached the vendor. ``force_new`` defaults to false."""
    force_new = False if body is None else bool(body.force_new)
    with _errors():
        return _executor(conn).retry_video_submit(project_id, node_id, force_new=force_new)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/decline")
def decline_node(
    project_id: str,
    node_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).decline_node(project_id, node_id)


@router.post("/api/v2/projects/{project_id}/nodes/{node_id}/regenerate")
def regenerate_node(
    project_id: str,
    node_id: str,
    body: NodeRunRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).regenerate(project_id, node_id, _run_body(body))


@router.post("/api/v2/projects/{project_id}/budget/{entry_id}/raise")
def raise_budget(
    project_id: str,
    entry_id: str,
    body: BudgetRaiseRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).raise_budget(project_id, entry_id, body.budget_cap)


@router.post("/api/v2/projects/{project_id}/budget/{entry_id}/decline")
def decline_budget(
    project_id: str,
    entry_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).decline_budget(project_id, entry_id)


@router.get("/api/v2/projects/{project_id}/status")
def project_status(
    project_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).status(project_id)


@router.post("/api/v2/projects/{project_id}/looks/{look_id}/impact")
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
