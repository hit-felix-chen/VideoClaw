"""Build ``ModelSpec`` rows from ``MODEL_CONFIG`` and from official docs.

Seedance 2.5 numbers come from the pages named in ``SEEDANCE_25_SOURCES``.
Fields those pages do not state stay unset and are listed in
``SEEDANCE_25_TODO``. Other models reuse the registry row as written,
with resolution and ratio spellings folded to the unified form.
"""

from typing import Any, Optional

from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.spec import CapabilityText, DurationLimit, ModelSpec, PriceQuote, PriceUnit

TASK_DOC = "https://ark.volcengine.com/region:cn-beijing/docs/create-video-generation-task-api"
TASK_DOC_MIRROR = "https://ai.volcengine.com/docs/ark-aihub/2647417?lang=zh"
PRICE_DOC = "https://docs.volcengine.com/docs/82379/1544106"

SEEDANCE_25_SOURCES = (TASK_DOC, TASK_DOC_MIRROR, PRICE_DOC)

# The price page's 1080p 72% window ended 2026-10-17 14:00 UTC+8.
# These rows store the listed 原价, not that expired discount.
SEEDANCE_25_TODO = (
    "price_per_second: docs bill CNY per million tokens, not a flat rate per second",
    "minimum token floor when the input includes video: the price page links a separate table",
    "billing frame rate: the token formula uses output fps; 24 on the task page only rounds returned duration",
    "offline inference price: the price page says Seedance 2.5 offline inference is unsupported",
    "effective_date: the price page does not state a list-price effective date; the field is left empty",
)

_CONFIG_PRICE_FIELDS = (
    ("price_per_1k_input_token", PriceUnit.PER_1K_INPUT_TOKEN),
    ("price_per_1k_output_token", PriceUnit.PER_1K_OUTPUT_TOKEN),
    ("price_per_image", PriceUnit.PER_IMAGE),
    ("price_per_second", PriceUnit.PER_SECOND),
)

# Official chat rows set web_search in ``official.py``. A DashScope row that
# is still only in MODEL_CONFIG is not on the 2026-11-03 chat list, so
# enable_search stays off. Bailian is the only vendor that documents
# extra_body.enable_search on compatible-mode chat.completions.
# https://www.alibabacloud.com/help/en/model-studio/web-search
_DASHSCOPE_WEB_SEARCH_DOC = "https://www.alibabacloud.com/help/en/model-studio/web-search"
_DASHSCOPE_CHAT_WEB_SEARCH = frozenset()
_CONFIG_DEPRECATED = {
    "seedance-1-0-pro": (
        "being retired; official id doubao-seedance-1-0-pro-250528 "
        "(Volcengine pricing page fetched 2026-11-03, no shutdown date published)"
    ),
}
_LLM_IMAGE_PROVIDERS = frozenset({"openai", "gemini"})


class SpecCatalog:
    """Assemble specs. The registry is the only caller."""

    @staticmethod
    def from_config(model_id: str, metadata: dict) -> ModelSpec:
        caps = metadata.get("capabilities") or {}
        types = list(metadata.get("type") or [])
        contract = bool(caps.get("api_contract_verified", False))
        resolutions = CapabilityText.resolutions(caps.get("resolutions"))
        ratios = CapabilityText.ratios(caps.get("ratios"))
        duration = SpecCatalog._duration(caps.get("duration"))
        abilities = list(caps.get("ability_types") or [])
        adapter_abilities = list(caps.get("adapter_ability_types") or [])
        if not abilities and adapter_abilities:
            abilities = list(adapter_abilities)
        input_modalities = list(caps.get("input_modalities") or [])
        adapter_input_modalities = list(caps.get("adapter_input_modalities") or [])
        input_modalities, adapter_input_modalities, web_search = SpecCatalog._llm_contract(
            model_id,
            metadata.get("provider") or "unknown",
            types,
            input_modalities,
            adapter_input_modalities,
        )
        return ModelSpec(
            id=model_id,
            name=metadata.get("name") or model_id,
            provider=metadata.get("provider") or "unknown",
            family=metadata.get("family"),
            types=types,
            abilities=abilities,
            adapter_abilities=adapter_abilities,
            ability_type=caps.get("ability_type") or (abilities[0] if abilities else ""),
            input_modalities=input_modalities,
            adapter_input_modalities=adapter_input_modalities,
            web_search=web_search,
            output=SpecCatalog._output(types),
            concurrency=int(metadata.get("concurrency") or 1),
            resolutions=resolutions,
            aspect_ratios=ratios,
            duration=duration,
            api_contract_verified=contract,
            resolutions_verified=contract and bool(resolutions),
            aspect_ratios_verified=contract and bool(ratios),
            prices=SpecCatalog._config_prices(model_id, metadata),
            max_reference_images=SpecCatalog._optional_int(caps.get("max_reference_images")),
            max_reference_images_verified=bool(caps.get("max_reference_images_verified", False)),
            deprecated=_CONFIG_DEPRECATED.get(model_id, ""),
        )

    @staticmethod
    def _llm_contract(
        model_id: str,
        provider: str,
        types: list,
        input_modalities: list,
        adapter_input_modalities: list,
    ) -> tuple:
        """Image input and web search for LLM rows. Other modalities stay untouched."""
        if "llm" not in types:
            return input_modalities, adapter_input_modalities, None
        if not input_modalities:
            # OpenAI and Gemini already sent image_url parts. DashScope text
            # generation dropped image_urls; reject those instead of dropping
            # them. A model registered as both llm and vlm keeps image parts
            # on this same chat.completions call.
            if "vlm" in types or provider in _LLM_IMAGE_PROVIDERS:
                input_modalities = ["text", "image"]
            else:
                input_modalities = ["text"]
        if not adapter_input_modalities:
            adapter_input_modalities = list(input_modalities)
        return input_modalities, adapter_input_modalities, SpecCatalog._llm_web_search(model_id, provider)

    @staticmethod
    def _llm_web_search(model_id: str, provider: str) -> dict:
        if provider == "dashscope" and model_id in _DASHSCOPE_CHAT_WEB_SEARCH:
            return {
                "supported": True,
                "verified": True,
                "transport": "extra_body.enable_search",
                "source": _DASHSCOPE_WEB_SEARCH_DOC,
            }
        if provider == "dashscope":
            return {
                "supported": False,
                "verified": True,
                "transport": None,
                "source": _DASHSCOPE_WEB_SEARCH_DOC,
                "note": (
                    "compatible-mode chat.completions accepts extra_body.enable_search; "
                    "this model id is not on the documented China (Beijing) chat list"
                ),
            }
        if provider == "gemini":
            return {
                "supported": False,
                "verified": True,
                "transport": None,
                "source": "Gemini chat.completions has no web_search parameter",
            }
        if provider == "deepseek":
            return {
                "supported": False,
                "verified": True,
                "transport": None,
                "source": "DeepSeek chat.completions has no verified web_search parameter",
            }
        if provider == "openai":
            return {
                "supported": False,
                "verified": True,
                "transport": None,
                "source": "OpenAI chat.completions has no enable_search parameter",
            }
        return {"supported": False, "verified": False, "transport": None, "source": ""}

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        return int(value)

    @staticmethod
    def _output(types: list) -> str:
        modes = CapabilityText.modalities(types)
        if "video" in modes:
            return "video"
        if "image" in modes:
            return "image"
        if "tts" in modes:
            return "audio"
        if "vlm" in modes:
            return "text"
        if "llm" in modes:
            return "text"
        return ""

    @staticmethod
    def _duration(raw: Any) -> Optional[DurationLimit]:
        if not isinstance(raw, dict):
            return None
        default = raw.get("default")
        return DurationLimit(
            min_seconds=raw.get("min"),
            max_seconds=raw.get("max"),
            integer=bool(raw.get("integer", True)),
            default=default,
            allow_auto=bool(raw.get("allow_auto", default == -1)),
            verified=bool(raw.get("verified", False)),
        )

    @staticmethod
    def _config_prices(model_id: str, metadata: dict) -> list:
        rows = []
        for field_name, unit in _CONFIG_PRICE_FIELDS:
            if metadata.get(field_name) is None:
                continue
            rows.append(
                PriceQuote(
                    model_id=model_id,
                    amount=float(metadata[field_name]),
                    currency=None,
                    unit=unit,
                    verified=False,
                    source="model_config",
                )
            )
        return rows

    @staticmethod
    def seedance_25() -> ModelSpec:
        prices = []
        for resolution, with_video, amount in (
            ("480p", False, 70.0),
            ("720p", False, 70.0),
            ("1080p", False, 77.0),
            ("480p", True, 42.0),
            ("720p", True, 42.0),
            ("1080p", True, 46.0),
        ):
            prices.append(
                PriceQuote(
                    model_id=DEFAULT_REFERENCE_VIDEO_MODEL,
                    amount=amount,
                    currency="CNY",
                    unit=PriceUnit.PER_MILLION_TOKENS,
                    verified=True,
                    resolution=resolution,
                    includes_video_input=with_video,
                    source=PRICE_DOC,
                    source_url=PRICE_DOC,
                    list_price=True,
                    fetched="2026-11-03",
                )
            )
        abilities = [
            "text_to_video",
            "image_to_video",
            "first_frame_i2v",
            "start_end_frame_i2v",
            "reference_to_video",
            "native_audio",
        ]
        return ModelSpec(
            id=DEFAULT_REFERENCE_VIDEO_MODEL,
            name="Seedance 2.5",
            provider="ark",
            family="seedance",
            types=["video"],
            abilities=abilities,
            adapter_abilities=[
                "text_to_video",
                "first_frame_i2v",
                "start_end_frame_i2v",
                "reference_to_video",
                "native_audio",
            ],
            ability_type="reference_to_video",
            input_modalities=["text", "image", "audio", "video"],
            adapter_input_modalities=["text", "image", "audio", "video"],
            output="video",
            # Same local scheduler cap as the other Ark video rows. Not in the task or price docs.
            concurrency=10,
            resolutions=["480p", "720p", "1080p"],
            aspect_ratios=["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"],
            default_resolution="720p",
            default_ratio="adaptive",
            duration=DurationLimit(
                min_seconds=4,
                max_seconds=30,
                integer=True,
                default=-1,
                allow_auto=True,
                verified=True,
            ),
            max_reference_images=30,
            max_reference_videos=10,
            max_reference_audios=10,
            max_reference_images_verified=True,
            ratio_by_mode={
                "first_frame": ["adaptive"],
                "start_end_frame": ["adaptive"],
            },
            api_contract_verified=True,
            resolutions_verified=True,
            aspect_ratios_verified=True,
            prices=prices,
            currency="CNY",
            sources=list(SEEDANCE_25_SOURCES),
            defaults=["reference_to_video"],
            price_formula=(
                "(input_video_seconds + output_video_seconds) * width * height * fps / 1024"
            ),
            todos=list(SEEDANCE_25_TODO),
        )

    @staticmethod
    def edge_tts() -> ModelSpec:
        return ModelSpec(
            id="edge-tts",
            name="Edge TTS",
            provider="edge",
            family="edge-tts",
            types=["tts"],
            abilities=["text_to_speech"],
            adapter_abilities=["text_to_speech"],
            ability_type="text_to_speech",
            input_modalities=["text"],
            adapter_input_modalities=["text"],
            output="audio",
            concurrency=1,
            api_contract_verified=True,
            prices=[],
            currency=None,
            sources=[],
            defaults=["tts"],
            todos=(
                "price: edge-tts has no vendor price in this registry",
                "voice list, sample rate, and max characters are not declared on the client",
            ),
        )
