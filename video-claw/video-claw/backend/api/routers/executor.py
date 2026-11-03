"""Project API for one-node runs, video confirm, and the budget gate.

Routes call :class:`core.executor.GraphExecutor`. They do not call a provider.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException

from api.routers.library import get_library_connection
from api.schemas.executor import (
    BudgetRaiseRequest,
    ImpactConfirmRequest,
    ImpactRequest,
    NodeRunRequest,
)
from core.executor import GraphExecutor
from graph.errors import GraphError, NodeNotFoundError
from storage.database import (
    LookNotFoundError,
    ProjectNotFoundError,
    StorageError,
    StoreConnection,
)

router = APIRouter(tags=["Executor"])


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
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return _executor(conn).confirm(project_id, node_id)


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
