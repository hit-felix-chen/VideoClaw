from typing import Optional

from fastapi import APIRouter, Query

from models.config_model import get_models_by_type, list_api_models, model_type_capabilities
from models.middle.registry import ModelRegistry

router = APIRouter(tags=["Models"])


@router.get("/api/models")
async def get_api_models(
    media_type: Optional[str] = Query(None, pattern="^(image|video)$"),
    model_type: Optional[str] = Query(None, pattern="^(llm|vlm|t2i|i2i|video|tts)$"),
    ability: Optional[str] = Query(None),
    verified_only: bool = False,
):
    """List registered models. Same URL and payload the six-stage UI and sandbox already call."""
    if model_type:
        models = []
        for model in get_models_by_type(model_type):
            capabilities = model_type_capabilities(model_type, model)
            models.append(ModelRegistry.enrich_api_row({
                "id": model["id"],
                "label": model.get("name") or model["id"],
                "provider": model.get("provider"),
                "family": model.get("family"),
                "model_type": model_type,
                "type": model.get("type", []),
                "concurrency": model.get("concurrency"),
                "ability_type": capabilities.get("ability_type"),
                "ability_types": capabilities.get("ability_types", []),
                "adapter_ability_types": capabilities.get("adapter_ability_types", []),
                "input_modalities": capabilities.get("input_modalities", []),
                "adapter_input_modalities": capabilities.get("adapter_input_modalities", []),
                "api_contract_verified": capabilities.get("api_contract_verified", False),
                "capabilities": capabilities,
            }))
        return {
            "models": models
        }

    required = [ability] if ability else None
    workflows = list_api_models(
        media_type=media_type,
        required_adapter_abilities=required,
        verified_only=verified_only,
    )
    return {
        "models": [
            ModelRegistry.enrich_api_row({
                "id": workflow["model"],
                "label": workflow.get("display_name") or workflow["model"],
                "provider": workflow.get("provider"),
                "family": workflow.get("family"),
                "media_type": workflow.get("media_type"),
                "ability_type": workflow.get("ability_type"),
                "ability_types": workflow.get("ability_types", []),
                "adapter_ability_types": workflow.get("adapter_ability_types", []),
                "input_modalities": workflow.get("input_modalities", []),
                "adapter_input_modalities": workflow.get("adapter_input_modalities", []),
                "api_contract_verified": workflow.get("api_contract_verified", False),
                "capabilities": workflow.get("capabilities", {}),
            })
            for workflow in workflows
        ]
    }
