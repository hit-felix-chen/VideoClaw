# -*- coding: utf-8 -*-
from fastapi import APIRouter, HTTPException

from api.dependencies import workflow_engine
from api.schemas.project import AssetImportRequest, AssetUpdateRequest
from assets.library import AssetLibrary

router = APIRouter(tags=["Assets"])


@router.get("/api/assets")
async def list_assets():
    return {"assets": AssetLibrary.list_assets()}


@router.post("/api/assets")
async def import_asset(req: AssetImportRequest):
    try:
        return workflow_engine.import_session_asset(req.session_id, req.item_type, req.item_id)
    except KeyError as exc:
        detail = exc.args[0] if exc.args else "Not found"
        raise HTTPException(404, str(detail)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/api/assets/{asset_id}")
async def update_asset(asset_id: str, req: AssetUpdateRequest):
    try:
        asset = AssetLibrary.update(
            asset_id,
            name=req.name,
            description=req.description,
            source_path=req.source_path,
        )
    except KeyError:
        raise HTTPException(404, "Library asset not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"asset": asset}


@router.delete("/api/assets/{asset_id}")
async def delete_asset(asset_id: str):
    try:
        deleted = AssetLibrary.delete(asset_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "Library asset not found")
    return {"status": "deleted", "id": asset_id}
