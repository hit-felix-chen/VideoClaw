"""Typed model spec, capability values, and price rows.

Amounts copied from ``MODEL_CONFIG`` keep ``currency=None`` and
``verified=False``. Seedance 2.5 prices are CNY because the Volcengine
price page bills in 元.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class PriceUnit:
    PER_1K_INPUT_TOKEN = "per_1k_input_token"
    PER_1K_OUTPUT_TOKEN = "per_1k_output_token"
    PER_IMAGE = "per_image"
    PER_SECOND = "per_second"
    PER_MILLION_TOKENS = "per_million_tokens"


class CapabilityText:
    """One spelling for resolutions and aspect ratios."""

    _RESOLUTIONS = {
        "480p": "480p",
        "720p": "720p",
        "1080p": "1080p",
        "2k": "2k",
        "4k": "4k",
    }

    @staticmethod
    def resolution(value: Any) -> str:
        text = str(value or "").strip().lower().replace(" ", "")
        return CapabilityText._RESOLUTIONS.get(text, text)

    @staticmethod
    def resolutions(values: Optional[list]) -> list:
        seen = []
        for value in values or []:
            item = CapabilityText.resolution(value)
            if item and item not in seen:
                seen.append(item)
        return seen

    @staticmethod
    def ratio(value: Any) -> str:
        text = str(value or "").strip()
        if text.lower() == "adaptive":
            return "adaptive"
        return text

    @staticmethod
    def ratios(values: Optional[list]) -> list:
        seen = []
        for value in values or []:
            item = CapabilityText.ratio(value)
            if item and item not in seen:
                seen.append(item)
        return seen

    @staticmethod
    def vendor_resolution(token: str, case: str) -> str:
        if not token or case != "upper":
            return token
        if token.endswith("p"):
            return token[:-1] + "P"
        if token.endswith("k"):
            return token[:-1] + "K"
        return token.upper()

    @staticmethod
    def modalities(types: Optional[list]) -> list:
        modes = []
        for item in types or []:
            if item in {"t2i", "i2i", "image"}:
                coarse = "image"
            elif item == "video":
                coarse = "video"
            else:
                coarse = item
            if coarse not in modes:
                modes.append(coarse)
        return modes


@dataclass(frozen=True)
class PriceQuote:
    """One billable unit. R3 reads these fields; it does not invent a currency."""

    model_id: str
    amount: float
    currency: Optional[str]
    unit: str
    verified: bool
    resolution: Optional[str] = None
    includes_video_input: Optional[bool] = None
    source: str = ""
    list_price: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DurationLimit:
    min_seconds: Optional[int] = None
    max_seconds: Optional[int] = None
    integer: bool = True
    default: Optional[int] = None
    allow_auto: bool = False
    verified: bool = False

    def as_dict(self) -> dict:
        payload = {
            "min": self.min_seconds,
            "max": self.max_seconds,
            "integer": self.integer,
            "allow_auto": self.allow_auto,
            "verified": self.verified,
        }
        if self.default is not None:
            payload["default"] = self.default
        return payload

    def choices(self) -> list:
        span = f"{self.min_seconds}..{self.max_seconds}"
        if self.allow_auto:
            return [span, -1]
        return [span]


@dataclass
class ModelSpec:
    id: str
    name: str
    provider: str
    types: list
    prices: list = field(default_factory=list)
    family: Optional[str] = None
    abilities: list = field(default_factory=list)
    adapter_abilities: list = field(default_factory=list)
    ability_type: str = ""
    input_modalities: list = field(default_factory=list)
    adapter_input_modalities: list = field(default_factory=list)
    output: str = ""
    concurrency: int = 1
    resolutions: list = field(default_factory=list)
    aspect_ratios: list = field(default_factory=list)
    default_resolution: Optional[str] = None
    default_ratio: Optional[str] = None
    duration: Optional[DurationLimit] = None
    max_reference_images: Optional[int] = None
    max_reference_videos: Optional[int] = None
    max_reference_audios: Optional[int] = None
    ratio_by_mode: dict = field(default_factory=dict)
    api_contract_verified: bool = False
    resolutions_verified: bool = False
    aspect_ratios_verified: bool = False
    max_reference_images_verified: bool = False
    currency: Optional[str] = None
    sources: list = field(default_factory=list)
    defaults: list = field(default_factory=list)
    ops: tuple = (
        "normalize",
        "submit",
        "poll",
        "fetch",
        "quote",
        "classify_error",
    )
    price_formula: str = ""
    todos: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.modalities = CapabilityText.modalities(self.types)
        if self.currency is None:
            currencies = {price.currency for price in self.prices if price.currency}
            if len(currencies) == 1 and all(price.currency for price in self.prices):
                self.currency = next(iter(currencies))

    def capability_dict(self) -> dict:
        abilities = list(self.abilities)
        adapter_abilities = list(self.adapter_abilities or abilities)
        payload = {
            "ability_type": self.ability_type or (abilities[0] if abilities else ""),
            "ability_types": abilities,
            "adapter_ability_types": adapter_abilities,
            "input_modalities": list(self.input_modalities),
            "adapter_input_modalities": list(self.adapter_input_modalities or self.input_modalities),
            "api_contract_verified": self.api_contract_verified,
            "output": self.output,
            "ops": list(self.ops),
            "verified": {
                "resolutions": self.resolutions_verified,
                "aspect_ratios": self.aspect_ratios_verified,
                "duration": bool(self.duration and self.duration.verified),
                "max_reference_images": self.max_reference_images_verified,
                "price": bool(self.prices) and all(price.verified for price in self.prices),
            },
        }
        if self.resolutions:
            payload["resolutions"] = list(self.resolutions)
        if self.aspect_ratios:
            payload["ratios"] = list(self.aspect_ratios)
            payload["aspect_ratios"] = list(self.aspect_ratios)
        if self.default_resolution:
            payload["default_resolution"] = self.default_resolution
        if self.default_ratio:
            payload["default_ratio"] = self.default_ratio
        if self.duration is not None:
            payload["duration"] = self.duration.as_dict()
        if self.max_reference_images is not None:
            payload["max_reference_images"] = self.max_reference_images
        if self.max_reference_videos is not None:
            payload["max_reference_videos"] = self.max_reference_videos
        if self.max_reference_audios is not None:
            payload["max_reference_audios"] = self.max_reference_audios
        if self.ratio_by_mode:
            payload["ratio_by_mode"] = {key: list(value) for key, value in self.ratio_by_mode.items()}
        if self.sources:
            payload["sources"] = list(self.sources)
        if self.defaults:
            payload["default_for"] = list(self.defaults)
        if self.price_formula:
            payload["price_formula"] = self.price_formula
        if self.todos:
            payload["todo"] = list(self.todos)
        return payload

    def as_metadata(self) -> dict:
        payload = {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "type": list(self.types),
            "concurrency": self.concurrency,
            "capabilities": self.capability_dict(),
            "pricing": [price.as_dict() for price in self.prices],
            "currency": self.currency,
        }
        if self.family:
            payload["family"] = self.family
        return payload

    def workflow_row(self) -> dict:
        capabilities = self.capability_dict()
        media_type = "video" if "video" in self.modalities else "image" if "image" in self.modalities else ""
        provider = self.provider or ""
        return {
            "key": f"api/{provider}/{self.id}",
            "name": self.id,
            "display_name": f"{self.name} - API {provider.title()}",
            "source": "api",
            "provider": provider,
            "family": self.family,
            "model": self.id,
            "media_type": media_type,
            "type": list(self.types),
            "capabilities": capabilities,
            "ability_type": capabilities.get("ability_type"),
            "ability_types": capabilities.get("ability_types", []),
            "adapter_ability_types": capabilities.get("adapter_ability_types", []),
            "input_modalities": capabilities.get("input_modalities", []),
            "adapter_input_modalities": capabilities.get("adapter_input_modalities", []),
            "api_contract_verified": capabilities.get("api_contract_verified", False),
            "concurrency": self.concurrency,
        }

    def price_verified(self) -> bool:
        return bool(self.prices) and all(price.verified for price in self.prices)
