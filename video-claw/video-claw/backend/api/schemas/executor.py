"""Request bodies for the graph executor. Responses are executor records."""

from typing import Optional

from pydantic import BaseModel


class NodeRunRequest(BaseModel):
    model_id: Optional[str] = None
    modality: Optional[str] = None
    resolution: Optional[str] = None
    includes_video_input: Optional[bool] = None
    asset_count: Optional[float] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    completion_tokens: Optional[float] = None
    input_tokens: Optional[float] = None
    output_tokens: Optional[float] = None
    text: Optional[str] = None
    asset_id: Optional[str] = None
    look_id: Optional[str] = None
    # Accepted and ignored for video nodes. Duration comes from the model.
    duration: Optional[float] = None


class BudgetRaiseRequest(BaseModel):
    budget_cap: float


class ImpactRequest(BaseModel):
    model: Optional[str] = None
    unit_cost: Optional[float] = None


class ImpactConfirmRequest(BaseModel):
    confirm_token: str
    model: Optional[str] = None
    unit_cost: Optional[float] = None


class ImpactRegenerateRequest(NodeRunRequest):
    confirm_token: str
    node_ids: list[str]
