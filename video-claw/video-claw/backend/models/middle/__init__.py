"""Model middle layer.

Import concrete names from the submodules, or use ``models.middle.<Name>``.
The package init stays light so ``config`` can import an id constant without
loading adapters.
"""

from typing import Any

_EXPORTS = {
    "CapabilityText": "models.middle.spec",
    "CostQuote": "models.middle.pricing",
    "DEFAULT_REFERENCE_VIDEO_MODEL": "models.middle.ids",
    "DurationLimit": "models.middle.spec",
    "EdgeTtsAdapter": "models.middle.providers",
    "ModelIds": "models.middle.ids",
    "ModelRegistry": "models.middle.registry",
    "ModelSpec": "models.middle.spec",
    "OpenAICompatibleAdapter": "models.middle.providers",
    "PriceBook": "models.middle.pricing",
    "PriceQuote": "models.middle.spec",
    "PriceUnit": "models.middle.spec",
    "ProviderAdapter": "models.middle.adapter",
    "ProviderDispatch": "models.middle.adapter",
    "UnsupportedParam": "models.middle.errors",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = __import__(module_name, fromlist=[name])
    return getattr(module, name)


__all__ = sorted(_EXPORTS)
