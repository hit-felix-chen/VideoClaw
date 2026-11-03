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


class EpisodeProjectCreate(BaseModel):
    name: str
    budget_cap: Optional[float] = None
    idea: Optional[str] = None
    llm_model: Optional[str] = None
    image_model: Optional[str] = None
    video_model: Optional[str] = None
    tts_model: Optional[str] = None


class ModelUpdateRequest(BaseModel):
    llm_model: Optional[str] = None
    image_model: Optional[str] = None
    video_model: Optional[str] = None
    tts_model: Optional[str] = None
    nodes: Optional[dict[str, str]] = None


class PromptUpdateRequest(BaseModel):
    text: str


class ConfirmRequest(BaseModel):
    """Plain confirm. ``force_new`` is rejected by the route."""

    force_new: bool = False


class RetryVideoSubmitRequest(BaseModel):
    """Manual retry. ``force_new`` defaults to false."""

    force_new: bool = False


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
