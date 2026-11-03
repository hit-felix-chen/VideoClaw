"""In-memory registry of model specs and provider adapters."""

from typing import Optional

from models.middle.ids import ModelIds


class ModelRegistry:
    """Exact-id registry. Call ``ensure`` indirectly through ``get``."""

    _specs = {}
    _adapters = []
    _adapter_keys = set()
    _ready = False

    @staticmethod
    def ensure() -> None:
        if ModelRegistry._ready:
            return
        ModelRegistry._ready = True
        ModelRegistry._load_config()
        ModelRegistry._load_builtin()
        from models.middle.providers import register_adapters

        register_adapters()

    @staticmethod
    def _load_config() -> None:
        from models.config_model import MODEL_CONFIG
        from models.middle.catalog import SpecCatalog

        for model_id, metadata in (MODEL_CONFIG.get("models") or {}).items():
            if not isinstance(metadata, dict):
                continue
            ModelRegistry._specs[model_id] = SpecCatalog.from_config(model_id, metadata)

    @staticmethod
    def _load_builtin() -> None:
        from models.middle.catalog import SpecCatalog

        for spec in (SpecCatalog.seedance_25(), SpecCatalog.edge_tts()):
            ModelRegistry._specs[spec.id] = spec

    @staticmethod
    def all() -> list:
        ModelRegistry.ensure()
        return list(ModelRegistry._specs.values())

    @staticmethod
    def extra_specs() -> list:
        """Specs that are not rows of ``MODEL_CONFIG``."""
        from models.config_model import MODEL_CONFIG

        ModelRegistry.ensure()
        known = set((MODEL_CONFIG.get("models") or {}))
        return [spec for spec in ModelRegistry._specs.values() if spec.id not in known]

    @staticmethod
    def get(model_id: Optional[str]):
        ModelRegistry.ensure()
        key = ModelIds.normalize(model_id)
        if not key:
            return None
        if key in ModelRegistry._specs:
            return ModelRegistry._specs[key]
        folded = key.casefold()
        hits = [spec for spec_id, spec in ModelRegistry._specs.items() if spec_id.casefold() == folded]
        if len(hits) == 1:
            return hits[0]
        alias = ModelIds.alias(key)
        if alias:
            return ModelRegistry._specs.get(alias)
        return None

    @staticmethod
    def register(adapter) -> None:
        ModelRegistry.ensure()
        key = (adapter.provider, tuple(sorted(adapter.modalities)))
        if key in ModelRegistry._adapter_keys:
            return
        ModelRegistry._adapter_keys.add(key)
        ModelRegistry._adapters.append(adapter)

    @staticmethod
    def unregister(adapter) -> None:
        key = (adapter.provider, tuple(sorted(adapter.modalities)))
        ModelRegistry._adapter_keys.discard(key)
        ModelRegistry._adapters = [item for item in ModelRegistry._adapters if item is not adapter]

    @staticmethod
    def add_spec(spec) -> None:
        ModelRegistry.ensure()
        ModelRegistry._specs[spec.id] = spec

    @staticmethod
    def remove_spec(model_id: str) -> None:
        ModelRegistry._specs.pop(model_id, None)

    @staticmethod
    def adapter_for(spec, modality: Optional[str] = None):
        ModelRegistry.ensure()
        if spec is None:
            return None
        found = [adapter for adapter in ModelRegistry._adapters if adapter.matches(spec, modality)]
        if modality is not None:
            exact = [adapter for adapter in found if adapter.modalities == frozenset({modality})]
            if len(exact) == 1:
                return exact[0]
        if len(found) == 1:
            return found[0]
        if not found:
            return None
        owned = [adapter for adapter in found if adapter.provider == spec.provider]
        if len(owned) == 1:
            return owned[0]
        return found[0]

    @staticmethod
    def enrich_api_row(row: dict) -> dict:
        """Add normalized capabilities and typed prices without dropping old keys."""
        if not isinstance(row, dict):
            return row
        spec = ModelRegistry.get(row.get("id") or row.get("model") or "")
        if spec is None:
            row.setdefault("pricing", [])
            row.setdefault("currency", None)
            return row
        capabilities = dict(row.get("capabilities") or {})
        view = spec.capability_dict()
        for key, value in view.items():
            if key in {
                "ability_type",
                "ability_types",
                "adapter_ability_types",
                "input_modalities",
                "adapter_input_modalities",
                "output",
            } and not value:
                continue
            capabilities[key] = value
        row["capabilities"] = capabilities
        row["pricing"] = [price.as_dict() for price in spec.prices]
        row["currency"] = spec.currency
        return row
