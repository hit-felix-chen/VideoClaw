"""Request bodies for the asset library. Responses are the storage records."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

AssetKind = Literal["script", "character", "setting", "prop", "segment", "final_cut"]
AssetScope = Literal["global", "project"]
ImageSource = Literal["generated", "uploaded", "extracted_frame"]


class ProjectCreate(BaseModel):
    name: str
    budget_cap: Optional[float] = None


class AssetCreate(BaseModel):
    name: str
    kind: AssetKind
    scope: AssetScope = "project"
    project_id: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    episode_id: Optional[str] = None


class AssetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    episode_id: Optional[str] = None


class LookCreate(BaseModel):
    name: str
    parent_look_id: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None


class LookUpdate(BaseModel):
    name: Optional[str] = None
    parent_look_id: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None


class LookRollback(BaseModel):
    version: int = Field(..., ge=1)


class ImageCreate(BaseModel):
    source: ImageSource
    asset_id: Optional[str] = None
    look_id: Optional[str] = None
    blob_hash: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    cost: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


class ImageSelect(BaseModel):
    image_id: str


class SegmentRefCreate(BaseModel):
    asset_id: str
    look_id: Optional[str] = None
    role: Optional[str] = None
    alias: Optional[str] = None


class ResolveRequest(BaseModel):
    text: str
    project_id: Optional[str] = None


class RegenerateQuoteRequest(BaseModel):
    model: Optional[str] = None
    unit_cost: Optional[float] = None


class RegenerateConfirmRequest(BaseModel):
    confirm_token: str
    model: Optional[str] = None
    unit_cost: Optional[float] = None
