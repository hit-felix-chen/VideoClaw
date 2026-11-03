"""资产库 HTTP API：Asset → Look → Image，以及片段显式绑定。

Routes sit on the R4 SQLite store. Look edits can mark dependents stale and
return a regenerate quote. Nothing here calls a model or spends money.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.schemas.library import (
    AssetCreate,
    AssetUpdate,
    ImageCreate,
    ImageSelect,
    LookCreate,
    LookRollback,
    LookUpdate,
    ProjectCreate,
    RegenerateConfirmRequest,
    RegenerateQuoteRequest,
    ResolveRequest,
    SegmentRefCreate,
)
from storage import (
    AssetNotFoundError,
    AssetStore,
    Database,
    ImageNotFoundError,
    ImageStore,
    LookCycleError,
    LookNotFoundError,
    LookStore,
    LookVersionNotFoundError,
    ProjectNotFoundError,
    ProjectStore,
    RegenerationQuote,
    SegmentRefNotFoundError,
    SegmentRefStore,
    StorageError,
    StoreConnection,
)

router = APIRouter(tags=["Library"])


def get_library_connection() -> Iterator[StoreConnection]:
    """One SQLite connection per request. Tests override this dependency."""
    conn = Database.connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _errors():
    try:
        yield
    except LookCycleError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (
        AssetNotFoundError,
        LookNotFoundError,
        LookVersionNotFoundError,
        ImageNotFoundError,
        ProjectNotFoundError,
        SegmentRefNotFoundError,
    ) as exc:
        raise HTTPException(404, str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(400, str(exc)) from exc


def _set_fields(body, names: tuple[str, ...]) -> dict:
    fields = body.model_fields_set
    return {name: getattr(body, name) for name in names if name in fields}


@router.post("/api/library/projects")
def create_project(body: ProjectCreate, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return ProjectStore.create(conn, body.name, budget_cap=body.budget_cap)


@router.get("/api/library/projects")
def list_projects(conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return {"projects": ProjectStore.list(conn)}


@router.get("/api/library/projects/{project_id}")
def get_project(project_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return ProjectStore.open(conn, project_id)


@router.post("/api/library/assets")
def create_asset(body: AssetCreate, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return AssetStore.create(
            conn,
            name=body.name,
            kind=body.kind,
            scope=body.scope,
            project_id=body.project_id,
            description=body.description,
            tags=body.tags,
            episode_id=body.episode_id,
        )


@router.get("/api/library/assets")
def list_assets(
    project_id: Optional[str] = None,
    scope: Optional[str] = None,
    kind: Optional[str] = None,
    tag: Optional[list[str]] = Query(default=None),
    include_global: bool = True,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        if tag:
            assets = AssetStore.search(
                conn,
                tags=tag,
                project_id=project_id,
                include_global=include_global,
                kind=kind,
                scope=scope,
            )
        else:
            assets = AssetStore.list(conn, project_id=project_id, scope=scope, kind=kind)
        return {"assets": assets}


@router.get("/api/library/search")
def search_assets(
    tag: list[str] = Query(...),
    project_id: Optional[str] = None,
    include_global: bool = True,
    kind: Optional[str] = None,
    scope: Optional[str] = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return {
            "assets": AssetStore.search(
                conn,
                tags=tag,
                project_id=project_id,
                include_global=include_global,
                kind=kind,
                scope=scope,
            )
        }


@router.get("/api/library/assets/{asset_id}")
def get_asset(asset_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return AssetStore.get(conn, asset_id)


@router.patch("/api/library/assets/{asset_id}")
def update_asset(
    asset_id: str,
    body: AssetUpdate,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return AssetStore.update(
            conn,
            asset_id,
            **_set_fields(body, ("name", "description", "tags", "episode_id")),
        )


@router.delete("/api/library/assets/{asset_id}")
def delete_asset(asset_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        if not AssetStore.delete(conn, asset_id):
            raise AssetNotFoundError(asset_id)
        return {"status": "deleted", "id": asset_id}


@router.post("/api/library/assets/{asset_id}/promote")
def promote_asset(asset_id: str, conn: StoreConnection = Depends(get_library_connection)):
    """Lift a project asset into the global library."""
    with _errors():
        return AssetStore.promote(conn, asset_id)


@router.post("/api/library/assets/{asset_id}/looks")
def create_look(
    asset_id: str,
    body: LookCreate,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return LookStore.create(
            conn,
            asset_id=asset_id,
            name=body.name,
            parent_look_id=body.parent_look_id,
            description=body.description,
            prompt=body.prompt,
            attributes=body.attributes,
        )


@router.get("/api/library/assets/{asset_id}/looks")
def list_looks(asset_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        AssetStore.get(conn, asset_id)
        return {"looks": LookStore.list(conn, asset_id)}


@router.get("/api/library/looks/{look_id}")
def get_look(look_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return LookStore.get(conn, look_id)


@router.patch("/api/library/looks/{look_id}")
def update_look(
    look_id: str,
    body: LookUpdate,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        kwargs = _set_fields(
            body,
            ("name", "parent_look_id", "description", "prompt", "attributes", "reason"),
        )
        if kwargs.get("reason") is None:
            kwargs.pop("reason", None)
        return LookStore.update(conn, look_id, **kwargs)


@router.delete("/api/library/looks/{look_id}")
def delete_look(look_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        if not LookStore.delete(conn, look_id):
            raise LookNotFoundError(look_id)
        return {"status": "deleted", "id": look_id}


@router.get("/api/library/looks/{look_id}/versions")
def list_look_versions(look_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return {"versions": LookStore.list_versions(conn, look_id)}


@router.post("/api/library/looks/{look_id}/rollback")
def rollback_look(
    look_id: str,
    body: LookRollback,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return LookStore.rollback(conn, look_id, body.version)


@router.post("/api/library/looks/{look_id}/select-image")
def select_look_image(
    look_id: str,
    body: ImageSelect,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return LookStore.select_image(conn, look_id, body.image_id)


@router.post("/api/library/looks/{look_id}/regenerate-quote")
def regenerate_quote(
    look_id: str,
    body: RegenerateQuoteRequest | None = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Estimate a stale regenerate. Does not generate and does not spend."""
    payload = body or RegenerateQuoteRequest()
    with _errors():
        return RegenerationQuote.build(
            conn,
            look_id,
            model=payload.model,
            unit_cost=payload.unit_cost,
        )


@router.post("/api/library/looks/{look_id}/regenerate-confirm")
def regenerate_confirm(
    look_id: str,
    body: RegenerateConfirmRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Record that the user accepted the current quote. Still does not spend."""
    with _errors():
        return RegenerationQuote.confirm(
            conn,
            look_id,
            body.confirm_token,
            model=body.model,
            unit_cost=body.unit_cost,
        )


@router.post("/api/library/images")
def create_image(body: ImageCreate, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return ImageStore.create(
            conn,
            source=body.source,
            asset_id=body.asset_id,
            look_id=body.look_id,
            blob_hash=body.blob_hash,
            model=body.model,
            prompt=body.prompt,
            cost=body.cost,
            width=body.width,
            height=body.height,
        )


@router.get("/api/library/images/{image_id}")
def get_image(image_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        return ImageStore.get(conn, image_id)


@router.get("/api/library/images")
def list_images(
    asset_id: Optional[str] = None,
    look_id: Optional[str] = None,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return {"images": ImageStore.list(conn, asset_id=asset_id, look_id=look_id)}


@router.delete("/api/library/images/{image_id}")
def delete_image(image_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        if not ImageStore.delete(conn, image_id):
            raise ImageNotFoundError(image_id)
        return {"status": "deleted", "id": image_id}


@router.post("/api/library/segments/{segment_asset_id}/refs")
def create_segment_ref(
    segment_asset_id: str,
    body: SegmentRefCreate,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return SegmentRefStore.create(
            conn,
            segment_asset_id=segment_asset_id,
            asset_id=body.asset_id,
            look_id=body.look_id,
            role=body.role,
            alias=body.alias,
        )


@router.get("/api/library/segments/{segment_asset_id}/refs")
def list_segment_refs(
    segment_asset_id: str,
    conn: StoreConnection = Depends(get_library_connection),
):
    with _errors():
        return {"refs": SegmentRefStore.list(conn, segment_asset_id)}


@router.delete("/api/library/refs/{ref_id}")
def delete_segment_ref(ref_id: str, conn: StoreConnection = Depends(get_library_connection)):
    with _errors():
        if not SegmentRefStore.delete(conn, ref_id):
            raise SegmentRefNotFoundError(ref_id)
        return {"status": "deleted", "id": ref_id}


@router.post("/api/library/resolve")
def resolve_mentions(
    body: ResolveRequest,
    conn: StoreConnection = Depends(get_library_connection),
):
    """Resolve ``@资产名`` and ``@资产名/形象名`` against the library."""
    with _errors():
        return SegmentRefStore.resolve(conn, body.text, project_id=body.project_id)
