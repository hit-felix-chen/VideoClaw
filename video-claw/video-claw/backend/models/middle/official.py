"""Official vendor models fetched 2026-11-03.

``vendor_models.json`` is the captured price page. Rows with ``verified``
false are registered with no price. Ids the vendors do not publish
(``qwen3.8-plus``, ``qwen3.6-max-preview``, unverified Kling display names)
are not in the file and do not resolve.

Token windows use decimal K: the captured context lengths are 1,000,000 and
1,024,000, and the Ark label ``1024k`` matches 1,024,000. So ``256K`` is
256,000 tokens, not 262,144.
"""

import json
import re
from pathlib import Path
from typing import Optional

from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.spec import CapabilityText, ModelSpec, PriceQuote, PriceUnit

FETCHED = "2026-11-03"
_DATA = Path(__file__).with_name("vendor_models.json")

# Chat completions on the OpenAI-compatible client. Kling's own REST and
# Gemini's video model are not on that path.
_CHAT_PROVIDERS = frozenset({"deepseek", "dashscope", "openai", "gemini", "moonshot", "ark"})

_VENDOR_PROVIDER = {
    "deepseek": "deepseek",
    "alibaba-bailian": "dashscope",
    "volcengine-ark": "ark",
    "openai": "openai",
    "google-gemini": "gemini",
    "moonshot-kimi": "moonshot",
    "kling": "kling",
}

_SCHEDULE = {
    "deepseek": "deepseek",
    "alibaba-bailian": "bailian",
    "volcengine-ark": "ark",
}

_ABSENT = (
    "qwen3.8-plus",
    "qwen3.6-max-preview",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
)

_WEB_SEARCH = "extra_body.enable_search"
_TOKEN = re.compile(r"(\d+(?:\.\d+)?)([kKmM])")
_BOUND = re.compile(r"([\[\(])([^,\]]+),([^\]\)]+)([\]\)])")
_RES = re.compile(r"(480|720|1080|4k)", re.IGNORECASE)


def absent_ids() -> tuple:
    """Retired or nonexistent ids. They are not registered and have no alias."""
    return _ABSENT


def load_records() -> list:
    return json.loads(_DATA.read_text(encoding="utf-8"))


def apply_official(specs: dict) -> None:
    """Insert official specs. An existing media row keeps its capabilities."""
    for spec in official_specs():
        if spec.id == DEFAULT_REFERENCE_VIDEO_MODEL:
            continue
        current = specs.get(spec.id)
        specs[spec.id] = spec if current is None else _merge(current, spec)


def official_specs() -> list:
    return [spec_from_record(row) for row in load_records()]


def spec_from_record(row: dict) -> ModelSpec:
    provider = _VENDOR_PROVIDER[row["vendor"]]
    kind = row.get("type") or ""
    image = row.get("image_input")
    notes = row.get("notes") or ""
    video_in = _chat_video_input(kind, notes)
    audio_in = _chat_audio_input(kind, notes)
    types = _types(kind, image is True)
    modalities = _input_modalities(kind, image is True, video_in, audio_in)
    chat = kind in {"llm", "vlm"}
    openai_compatible = bool(chat and provider in _CHAT_PROVIDERS and provider != "kling")
    if kind == "video" and (provider == "kling" or provider == "gemini" or str(row["id"]).startswith("kling/")):
        openai_compatible = False
    if kind in {"image", "video", "tts"}:
        openai_compatible = False
    if provider == "kling":
        openai_compatible = False

    prices = _prices(row) if row.get("verified") else []
    schedule = ""
    if any(price.tier in {"peak", "off-peak"} for price in prices):
        schedule = _SCHEDULE.get(row["vendor"], "")
    deprecated, deprecated_on = _deprecated(notes)
    abilities = _abilities(types, modalities)
    base_url = _base_url(row)
    todos = []
    if image is None and kind in {"llm", "vlm"}:
        todos.append("image input: vendor page did not confirm image_url for this id")
    if not row.get("verified"):
        todos.append(
            "price: listed by the vendor but no number was confirmed from the source page on "
            + FETCHED
        )
    if "2026-12-01" in notes:
        todos.append("scheduled price change 2026-12-01: list price doubles")
    if "2027-01-01" in notes:
        todos.append("scheduled price change 2027-01-01: list price doubles")
    if not openai_compatible and kind in {"video", "image", "tts"}:
        todos.append("openai_compatible: not callable through chat.completions")
    name = "deepseek-4.1-flash" if row["id"] == "deepseek-flash" else row["id"]
    sources = [row["source_url"]] if row.get("source_url") else []
    for extra in row.get("extra_sources") or []:
        if extra not in sources:
            sources.append(extra)
    return ModelSpec(
        id=row["id"],
        name=name,
        provider=provider,
        family=_family(row["id"], provider),
        types=types,
        abilities=abilities,
        adapter_abilities=list(abilities),
        ability_type=abilities[0] if abilities else "",
        input_modalities=modalities,
        adapter_input_modalities=list(modalities),
        web_search=_web_search(row, provider, chat),
        output=_output(kind),
        concurrency=10 if chat else 5 if kind in {"image", "video"} else 1,
        prices=prices,
        api_contract_verified=bool(row.get("verified") and openai_compatible),
        currency=_one_currency(prices),
        sources=sources,
        todos=todos,
        context_window=row.get("context"),
        max_output_tokens=row.get("max_output"),
        base_url=base_url,
        openai_compatible=openai_compatible,
        deprecated=deprecated,
        deprecated_on=deprecated_on,
        price_schedule=schedule,
    )


def _merge(existing: ModelSpec, official: ModelSpec) -> ModelSpec:
    """Keep a pipeline row's verified media contract; take the vendor price."""
    official.abilities = list(existing.abilities)
    official.adapter_abilities = list(existing.adapter_abilities)
    official.ability_type = existing.ability_type
    if existing.resolutions:
        official.resolutions = list(existing.resolutions)
        official.resolutions_verified = existing.resolutions_verified
    if existing.aspect_ratios:
        official.aspect_ratios = list(existing.aspect_ratios)
        official.aspect_ratios_verified = existing.aspect_ratios_verified
    if existing.duration is not None:
        official.duration = existing.duration
    if existing.input_modalities:
        official.input_modalities = list(existing.input_modalities)
        official.adapter_input_modalities = list(
            existing.adapter_input_modalities or existing.input_modalities
        )
    official.concurrency = existing.concurrency or official.concurrency
    official.api_contract_verified = existing.api_contract_verified or official.api_contract_verified
    official.max_reference_images = (
        existing.max_reference_images
        if existing.max_reference_images is not None
        else official.max_reference_images
    )
    official.max_reference_images_verified = existing.max_reference_images_verified
    official.max_reference_videos = existing.max_reference_videos
    official.max_reference_audios = existing.max_reference_audios
    official.default_resolution = existing.default_resolution or official.default_resolution
    official.default_ratio = existing.default_ratio or official.default_ratio
    official.ratio_by_mode = dict(existing.ratio_by_mode or official.ratio_by_mode)
    official.family = existing.family or official.family
    official.defaults = list(existing.defaults or official.defaults)
    official.price_formula = existing.price_formula or official.price_formula
    if existing.name and existing.name != existing.id:
        official.name = existing.name
    if not official.prices:
        official.prices = list(existing.prices)
        official.currency = existing.currency
    kept = list(existing.todos)
    for item in official.todos:
        if item not in kept:
            kept.append(item)
    official.todos = kept
    if existing.sources:
        sources = list(existing.sources)
        for item in official.sources:
            if item not in sources:
                sources.append(item)
        official.sources = sources
    return official


def _types(kind: str, image: bool) -> list:
    if kind == "llm":
        return ["llm", "vlm"] if image else ["llm"]
    if kind == "vlm":
        return ["vlm", "llm"]
    if kind == "image":
        return ["t2i", "i2i"] if image else ["t2i"]
    if kind == "video":
        return ["video"]
    if kind == "tts":
        return ["tts"]
    return [kind] if kind else []


def _input_modalities(kind: str, image: bool, video: bool, audio: bool) -> list:
    if kind in {"image", "video", "tts"}:
        modes = ["text"]
        if image and kind != "tts":
            modes.append("image")
        return modes
    modes = ["text"]
    if image:
        modes.append("image")
    if video:
        modes.append("video")
    if audio:
        modes.append("audio")
    return modes


def _abilities(types: list, modalities: list) -> list:
    abilities = []
    if "llm" in types:
        abilities.append("text_generation")
    if "vlm" in types:
        abilities.extend(["vision_language", "image_understanding"])
        if "video" in modalities:
            abilities.append("video_understanding")
    if "t2i" in types or "image" in types:
        abilities.append("text_to_image")
    if "i2i" in types:
        abilities.append("image_to_image")
    if "video" in types:
        abilities.append("text_to_video")
        if "image" in modalities:
            abilities.append("image_to_video")
    if "tts" in types:
        abilities.append("text_to_speech")
    return abilities


def _output(kind: str) -> str:
    if kind == "video":
        return "video"
    if kind == "image":
        return "image"
    if kind == "tts":
        return "audio"
    if kind in {"llm", "vlm"}:
        return "text"
    return ""


def _family(model_id: str, provider: str) -> str:
    head = model_id.split("/", 1)[-1]
    for prefix, family in (
        ("qwen", "qwen"),
        ("wan", "wan"),
        ("happyhorse", "happyhorse"),
        ("cosyvoice", "cosyvoice"),
        ("deepseek", "deepseek"),
        ("doubao-seedance", "seedance"),
        ("doubao-seedream", "seedream"),
        ("doubao-seed", "doubao"),
        ("gpt", "gpt"),
        ("gemini", "gemini"),
        ("kimi", "kimi"),
        ("glm", "glm"),
        ("kling", "kling"),
    ):
        if head.startswith(prefix) or model_id.startswith(prefix):
            return family
    return provider


def _base_url(row: dict) -> str:
    raw = row.get("base_url") or ""
    if "dashscope.aliyuncs.com/compatible-mode/v1" in raw:
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    if "api-beijing.klingai.com" in raw:
        return "https://api-beijing.klingai.com"
    token = raw.split()[0] if raw else ""
    return token.rstrip("/")


def _chat_video_input(kind: str, notes: str) -> bool:
    if kind not in {"llm", "vlm"}:
        return False
    text = notes.lower()
    return "image+video" in text or "video input" in text or "audio/video" in text


def _chat_audio_input(kind: str, notes: str) -> bool:
    if kind not in {"llm", "vlm"}:
        return False
    return "audio/video" in notes.lower() or "text/image/audio/video" in notes.lower()


def _web_search(row: dict, provider: str, chat: bool) -> Optional[dict]:
    if not chat:
        return None
    source = row.get("source_url") or ""
    if provider == "dashscope" and row.get("web_search") is True:
        return {
            "supported": True,
            "verified": True,
            "transport": _WEB_SEARCH,
            "source": source,
        }
    return {
        "supported": False,
        "verified": True,
        "transport": None,
        "source": source,
    }


def _deprecated(notes: str) -> tuple:
    if "BEING RETIRED" in notes:
        return (
            "being retired on the vendor pricing page fetched "
            + FETCHED
            + "; no shutdown date published",
            None,
        )
    if "closed to new users" in notes or "offline end of Dec 2026" in notes:
        return (
            "closed to new users 2026-10-31; K2 series offline end of Dec 2026",
            "2026-12-31",
        )
    if "deprecated" in notes.lower():
        return (
            "deprecated; announced 2025-12-17; no shutdown date published",
            "2025-12-17",
        )
    return "", None


def _one_currency(prices: list) -> Optional[str]:
    currencies = {price.currency for price in prices if price.currency}
    if len(currencies) == 1:
        return next(iter(currencies))
    return None


def _prices(row: dict) -> list:
    quotes = []
    for item in row.get("prices") or []:
        quotes.extend(_one_price(row, item))
    return quotes


def _one_price(row: dict, item: dict) -> list:
    amount = item.get("price")
    amounts = amount if isinstance(amount, list) else [amount]
    if not amounts or any(not isinstance(value, (int, float)) for value in amounts):
        return []
    parsed = _parse_unit(str(item.get("unit") or ""))
    window = _parse_window(item.get("tier"), item.get("effective"))
    if parsed is None:
        return []
    unit, cache, meter, resolutions, includes_video = parsed
    if isinstance(amount, list) and "layer split" in str(item.get("unit") or "").lower():
        labels = ("<=2.61MP", ">2.61MP")
        built = []
        for label, value in zip(labels, amounts):
            built.extend(
                _emit(
                    row,
                    value,
                    unit,
                    cache,
                    label,
                    resolutions,
                    includes_video,
                    window,
                    item.get("currency"),
                )
            )
        return built
    if isinstance(amount, list):
        built = []
        for index, value in enumerate(amounts):
            built.extend(
                _emit(
                    row,
                    value,
                    unit,
                    cache,
                    f"unparsed-{index}",
                    resolutions,
                    includes_video,
                    window,
                    item.get("currency"),
                )
            )
        return built
    return _emit(
        row,
        amounts[0],
        unit,
        cache,
        meter or window.get("meter") or "",
        resolutions,
        includes_video,
        window,
        item.get("currency"),
    )


def _emit(
    row,
    amount,
    unit,
    cache,
    meter,
    resolutions,
    includes_video,
    window,
    currency,
) -> list:
    targets = resolutions or [None]
    quotes = []
    for resolution in targets:
        quotes.append(
            PriceQuote(
                model_id=row["id"],
                amount=float(amount),
                currency=currency,
                unit=unit,
                verified=True,
                resolution=resolution,
                includes_video_input=includes_video,
                source=row.get("source_url") or "",
                source_url=row.get("source_url") or "",
                list_price=True,
                cache=cache,
                tier=window.get("tier"),
                region=window.get("region"),
                input_min=window.get("input_min"),
                input_min_exclusive=bool(window.get("input_min_exclusive")),
                input_max=window.get("input_max"),
                input_max_exclusive=bool(window.get("input_max_exclusive")),
                effective_from=window.get("effective_from"),
                effective_until=window.get("effective_until"),
                fetched=row.get("fetched") or FETCHED,
                meter=meter or "",
            )
        )
    return quotes


def _parse_window(tier: Optional[str], effective: Optional[str]) -> dict:
    window = {
        "tier": None,
        "region": None,
        "meter": "",
        "input_min": None,
        "input_min_exclusive": False,
        "input_max": None,
        "input_max_exclusive": False,
        "effective_from": None,
        "effective_until": None,
    }
    if effective:
        text = str(effective).strip()
        if text.startswith("through "):
            window["effective_until"] = text[len("through ") :]
        elif text.startswith("from "):
            window["effective_from"] = text[len("from ") :]
    if not tier:
        return window
    text = str(tier).strip()
    compact = text.replace(" ", "")
    peak = re.fullmatch(r"(off-peak|peak)(?:\s+(cn-beijing|singapore))?", text)
    if peak:
        window["tier"] = peak.group(1)
        window["region"] = peak.group(2)
        return window
    if text in {"cn-beijing", "singapore"}:
        window["region"] = text
        return window
    upper = re.fullmatch(r"input<=(\d+(?:\.\d+)?[kKmM])", compact)
    if upper:
        window["input_min"] = 0
        window["input_max"] = _token_count(upper.group(1))
        return window
    span = re.fullmatch(r"(\d+(?:\.\d+)?[kKmM])<input<=(\d+(?:\.\d+)?[kKmM])", compact)
    if span:
        window["input_min"] = _token_count(span.group(1))
        window["input_min_exclusive"] = True
        window["input_max"] = _token_count(span.group(2))
        return window
    bound = _BOUND.fullmatch(compact)
    if bound:
        window["input_min"] = _token_count(bound.group(2))
        window["input_min_exclusive"] = bound.group(1) == "("
        window["input_max"] = _token_count(bound.group(3))
        window["input_max_exclusive"] = bound.group(4) == ")"
        return window
    window["meter"] = text
    return window


def _token_count(text: str) -> int:
    match = _TOKEN.fullmatch(text.strip())
    if not match:
        return int(float(text))
    number = float(match.group(1))
    scale = match.group(2).lower()
    factor = 1000 if scale == "k" else 1_000_000
    return int(number * factor)


def _parse_unit(unit: str):
    text = unit.strip()
    low = text.lower()
    resolutions = _resolutions(text) if ("second" in low or "token" in low) else []
    includes_video = None
    if "no video" in low:
        includes_video = False
    elif "video in" in low or "with video" in low:
        includes_video = True
    meter = ""
    if "no audio" in low or "silent" in low:
        meter = "silent"
    elif re.search(r"\baudio\b", low) and "token" not in low:
        meter = "audio"

    if "cache write" in low:
        return PriceUnit.PER_MILLION_CACHE_WRITE_TOKENS, "write", "cache_write", [], None
    if "cache miss" in low:
        return PriceUnit.PER_MILLION_INPUT_TOKENS, "miss", _token_meter(low), [], None
    if "cache hit" in low or "cache-hit" in low or "cached" in low:
        return PriceUnit.PER_MILLION_CACHED_INPUT_TOKENS, "hit", _token_meter(low), [], None
    if "10k characters" in low or "10k character" in low:
        return PriceUnit.PER_10K_CHARACTERS, None, "", [], None
    if low.startswith("second"):
        if "..." in text:
            meter = meter or "unparsed"
        return PriceUnit.PER_SECOND, None, meter, resolutions, includes_video
    if "output" in low and "token" in low:
        return PriceUnit.PER_MILLION_OUTPUT_TOKENS, None, _token_meter(low), [], None
    if "input" in low and "token" in low:
        meter = _token_meter(low)
        if "audio" in low and "text/image" not in low and "video" not in low:
            meter = "audio"
        return PriceUnit.PER_MILLION_INPUT_TOKENS, "miss", meter, [], None
    if "1m tokens" in low or low.startswith("1m tokens"):
        return (
            PriceUnit.PER_MILLION_TOKENS,
            None,
            meter,
            resolutions,
            includes_video,
        )
    if "image" in low or low == "image":
        detail = ""
        if "output" in low or "input" in low or "layer" in low:
            detail = text
        return PriceUnit.PER_IMAGE, None, detail, [], None
    return None


def _token_meter(low: str) -> str:
    if "thinking" in low:
        return "thinking"
    if "video" in low and "text/image" not in low:
        return "video"
    if "audio" in low and "text/image" not in low:
        return "audio"
    if "image" in low and "text/image" not in low:
        return "image"
    if "text" in low and "text/image" not in low:
        return "text"
    return ""


def _resolutions(text: str) -> list:
    found = []
    for match in _RES.finditer(text):
        token = match.group(1).lower()
        item = "4k" if token == "4k" else f"{token}p"
        item = CapabilityText.resolution(item)
        if item not in found:
            found.append(item)
    return found
