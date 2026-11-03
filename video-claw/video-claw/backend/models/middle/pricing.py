"""Documented prices, pre-call ranges, and the unknown-model list.

Reuse: unit lookup on ``PriceBook``, Seedance rows that already cite the
Volcengine price page, and the 1..6 image-call span. This module is the
only price source. ``MODEL_CONFIG`` does not carry price fields.

A row is an API price only when it is verified, has a currency, and has an
http(s) ``source_url``. ``MODEL_CONFIG`` amounts fail that test. ``quote``
does not turn them into a number. Those models are listed by
``unknown_models``.
"""

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from models.middle.ids import ModelIds
from models.middle.spec import CapabilityText, PriceQuote, PriceUnit

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MILLION_UNITS = {
    PriceUnit.PER_MILLION_TOKENS,
    PriceUnit.PER_MILLION_INPUT_TOKENS,
    PriceUnit.PER_MILLION_OUTPUT_TOKENS,
    PriceUnit.PER_MILLION_CACHED_INPUT_TOKENS,
    PriceUnit.PER_MILLION_CACHE_WRITE_TOKENS,
}
_INPUT_UNITS = (PriceUnit.PER_1K_INPUT_TOKEN, PriceUnit.PER_MILLION_INPUT_TOKENS)
_OUTPUT_UNITS = (PriceUnit.PER_1K_OUTPUT_TOKEN, PriceUnit.PER_MILLION_OUTPUT_TOKENS)

# _generate_one loops up to 3 times, and each pass can call the image API
# twice through the doctor rewrite.
MIN_IMAGE_CALLS_PER_ASSET = 1
MAX_IMAGE_CALLS_PER_ASSET = 6

_HTTP = ("http://", "https://")


def money(value: float) -> float:
    return round(float(value), 6)


def image_cost_range(asset_count: float, unit_price: Optional[float]) -> Optional[dict]:
    """Price ``asset_count`` image assets at 1..6 calls each.

    ``unit_price is None`` means the price is missing. The result is None,
    not a zero, so a caller cannot publish the missing part as free.
    """
    if asset_count <= 0:
        return {"min": 0.0, "max": 0.0}
    if unit_price is None:
        return None
    return {
        "min": money(asset_count * MIN_IMAGE_CALLS_PER_ASSET * unit_price),
        "max": money(asset_count * MAX_IMAGE_CALLS_PER_ASSET * unit_price),
    }


def source_url(row: PriceQuote) -> str:
    url = (row.source_url or "").strip()
    if url:
        return url
    source = (row.source or "").strip()
    if source.startswith(_HTTP):
        return source
    return ""


def documented(row: PriceQuote) -> bool:
    """True only for a verified vendor price. Unlabeled config rows are not."""
    if not isinstance(row, PriceQuote) or not row.verified or row.currency is None:
        return False
    return source_url(row).startswith(_HTTP)


def _number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


@dataclass(frozen=True)
class CostQuote:
    """A computed charge. Built only when ``PriceBook`` has exactly one price."""

    model_id: str
    amount: float
    currency: Optional[str]
    unit: str
    total: float
    quantity: float
    verified: bool
    resolution: Optional[str] = None
    includes_video_input: Optional[bool] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QuoteRequest:
    """One generation call to price before it is submitted."""

    model_id: str
    modality: str = ""
    quantity: Optional[float] = None
    unit: Optional[str] = None
    resolution: Optional[str] = None
    includes_video_input: Optional[bool] = None
    asset_count: Optional[float] = None
    input_tokens: Optional[float] = None
    output_tokens: Optional[float] = None
    completion_tokens: Optional[float] = None
    at: Optional[datetime] = None
    region: Optional[str] = None


def _basis(quote: PriceQuote, quantity: Optional[float], completion_tokens: Optional[float]):
    if quote.unit == PriceUnit.PER_MILLION_TOKENS:
        if completion_tokens is None:
            return None
        tokens = float(completion_tokens)
        return tokens, quote.amount * tokens / 1_000_000
    if quote.unit in _MILLION_UNITS:
        if quantity is None:
            return None
        tokens = float(quantity)
        return tokens, quote.amount * tokens / 1_000_000
    if quote.unit in {PriceUnit.PER_1K_INPUT_TOKEN, PriceUnit.PER_1K_OUTPUT_TOKEN}:
        if quantity is None:
            return None
        tokens = float(quantity)
        return tokens, quote.amount * tokens / 1000.0
    if quote.unit == PriceUnit.PER_10K_CHARACTERS:
        if quantity is None:
            return None
        count = float(quantity)
        return count, quote.amount * count / 10_000
    if quantity is None:
        return None
    count = float(quantity)
    return count, quote.amount * count


def _blank(
    model_id: str,
    *,
    unknown: bool,
    reason: str,
) -> dict:
    listed = []
    if unknown and model_id:
        listed = [model_id]
    return {
        "available": False,
        "model_id": model_id,
        "currency": None,
        "unit": None,
        "resolution": None,
        "includes_video_input": None,
        "min": None,
        "max": None,
        "unit_amount": None,
        "source_url": "",
        "effective_date": None,
        "verified": False,
        "unknown_models": listed,
        "reason": reason,
    }


def _ready(row: PriceQuote, low: float, high: float, *, unit: Optional[str] = None) -> dict:
    return {
        "available": True,
        "model_id": row.model_id,
        "currency": row.currency,
        "unit": unit or row.unit,
        "resolution": row.resolution,
        "includes_video_input": row.includes_video_input,
        "min": money(low),
        "max": money(high),
        "unit_amount": row.amount,
        "source_url": source_url(row),
        "effective_date": row.effective_date,
        "verified": True,
        "unknown_models": [],
        "reason": "",
    }


class PriceBook:
    """Read typed prices off the registry. Does not call a provider."""

    @staticmethod
    def prices(model_id: str) -> list:
        from models.middle.registry import ModelRegistry

        if not ModelIds.normalize(model_id):
            return []
        spec = ModelRegistry.get(model_id)
        if spec is None:
            return []
        return list(spec.prices)

    @staticmethod
    def table() -> list:
        """Documented price rows only. Config amounts without a source URL are omitted."""
        from models.middle.registry import ModelRegistry

        rows = []
        for spec in ModelRegistry.all():
            for price in spec.prices:
                if not documented(price):
                    continue
                rows.append(
                    {
                        "model_id": price.model_id,
                        "amount": price.amount,
                        "currency": price.currency,
                        "unit": price.unit,
                        "resolution": price.resolution,
                        "includes_video_input": price.includes_video_input,
                        "effective_date": price.effective_date,
                        "source_url": source_url(price),
                        "fetched": price.fetched,
                        "tier": price.tier,
                        "region": price.region,
                        "cache": price.cache,
                        "meter": price.meter,
                        "effective_from": price.effective_from,
                        "effective_until": price.effective_until,
                        "verified": True,
                    }
                )
        rows.sort(
            key=lambda item: (
                item["model_id"],
                item["unit"],
                item["resolution"] or "",
                item["tier"] or "",
                item["region"] or "",
                item["meter"] or "",
                item["amount"],
            )
        )
        return rows

    @staticmethod
    def unknown_models() -> list:
        """Registered models that have no documented API price."""
        from models.middle.registry import ModelRegistry

        unknown = []
        for spec in ModelRegistry.all():
            if not any(documented(price) for price in spec.prices):
                unknown.append(spec.id)
        return sorted(unknown)

    @staticmethod
    def price_todos() -> list:
        """TODO lines for missing vendor prices. Does not fill in a number."""
        from models.middle.registry import ModelRegistry

        unknown = set(PriceBook.unknown_models())
        notes = []
        for spec in ModelRegistry.all():
            for item in spec.todos:
                text = str(item)
                if "price" in text.lower() or "effective_date" in text.lower():
                    notes.append(f"{spec.id}: {text}")
            if spec.id in unknown:
                notes.append(
                    f"TODO: {spec.id} has no documented API price with source_url"
                )
        return notes

    @staticmethod
    def _matches(
        row: PriceQuote,
        rows: list,
        *,
        unit: Optional[str],
        wanted_resolution: Optional[str],
        includes_video_input: Optional[bool],
    ) -> bool:
        if unit and row.unit != unit:
            return False
        resolution_specific = any(item.resolution for item in rows)
        if wanted_resolution is not None and resolution_specific and row.resolution != wanted_resolution:
            return False
        video_flags = {item.includes_video_input for item in rows if item.includes_video_input is not None}
        if len(video_flags) > 1:
            if includes_video_input is None or row.includes_video_input != includes_video_input:
                return False
        return True

    @staticmethod
    def quote(
        model_id: str,
        *,
        unit: Optional[str] = None,
        resolution: Optional[str] = None,
        includes_video_input: Optional[bool] = None,
        documented_only: bool = False,
        at: Optional[datetime] = None,
        input_tokens: Optional[float] = None,
        cache: str = "miss",
        region: Optional[str] = None,
    ) -> Optional[PriceQuote]:
        rows = PriceBook.prices(model_id)
        if documented_only:
            rows = [row for row in rows if documented(row)]
        if not rows:
            return None
        wanted_resolution = None
        if resolution is not None and str(resolution).strip() != "":
            wanted_resolution = CapabilityText.resolution(resolution)
        matched = [
            row
            for row in rows
            if PriceBook._matches(
                row,
                rows,
                unit=unit,
                wanted_resolution=wanted_resolution,
                includes_video_input=includes_video_input,
            )
        ]
        from models.middle.registry import ModelRegistry

        spec = ModelRegistry.get(model_id)
        schedule = getattr(spec, "price_schedule", "") if spec is not None else ""
        matched = _narrow(
            matched,
            at=at,
            input_tokens=input_tokens,
            cache=cache,
            region=region,
            schedule=schedule,
        )
        if len(matched) != 1:
            return None
        return matched[0]

    @staticmethod
    def cost(
        model_id: str,
        *,
        quantity: Optional[float] = None,
        completion_tokens: Optional[float] = None,
        unit: Optional[str] = None,
        resolution: Optional[str] = None,
        includes_video_input: Optional[bool] = None,
    ) -> Optional[CostQuote]:
        if not ModelIds.normalize(model_id):
            return None
        quote_row = PriceBook.quote(
            model_id,
            unit=unit,
            resolution=resolution,
            includes_video_input=includes_video_input,
        )
        if quote_row is None:
            return None
        priced = _basis(quote_row, quantity, completion_tokens)
        if priced is None:
            return None
        basis, total = priced
        return CostQuote(
            model_id=quote_row.model_id,
            amount=quote_row.amount,
            currency=quote_row.currency,
            unit=quote_row.unit,
            total=round(float(total), 6),
            quantity=basis,
            verified=quote_row.verified,
            resolution=quote_row.resolution,
            includes_video_input=quote_row.includes_video_input,
        )


def _token_amount(row: PriceQuote, tokens: float) -> float:
    if row.unit in {PriceUnit.PER_1K_INPUT_TOKEN, PriceUnit.PER_1K_OUTPUT_TOKEN}:
        return row.amount * tokens / 1000.0
    return row.amount * tokens / 1_000_000


def _quote_unit(model_id: str, units: tuple, request: QuoteRequest) -> Optional[PriceQuote]:
    for unit in units:
        row = PriceBook.quote(
            model_id,
            unit=unit,
            documented_only=True,
            at=request.at,
            input_tokens=request.input_tokens,
            region=request.region,
        )
        if row is not None:
            return row
    return None


def _as_utc(at: Optional[datetime]) -> datetime:
    if at is None:
        return datetime.now(timezone.utc)
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def peak_tier(schedule: str, at: Optional[datetime]) -> str:
    """Vendor peak window. DeepSeek is UTC; Bailian and Ark are Beijing time.

    DeepSeek peak is 01:00-04:00 and 06:00-10:00 UTC, Monday-Friday.
    Bailian off-peak is 22:00-08:00 UTC+8 every day.
    Ark peak is 09:00-12:00 and 14:00-18:00 UTC+8, Monday-Friday.
    The end of each window is exclusive.
    """
    moment = _as_utc(at)
    if schedule == "deepseek":
        if moment.weekday() >= 5:
            return "off-peak"
        minutes = moment.hour * 60 + moment.minute
        if (60 <= minutes < 240) or (360 <= minutes < 600):
            return "peak"
        return "off-peak"
    if schedule == "bailian":
        local = moment.astimezone(_SHANGHAI)
        minutes = local.hour * 60 + local.minute
        if minutes >= 22 * 60 or minutes < 8 * 60:
            return "off-peak"
        return "peak"
    if schedule == "ark":
        local = moment.astimezone(_SHANGHAI)
        if local.weekday() >= 5:
            return "off-peak"
        minutes = local.hour * 60 + local.minute
        if (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60):
            return "peak"
        return "off-peak"
    return "off-peak"


def _in_input_window(row: PriceQuote, tokens: float) -> bool:
    if row.input_min is None and row.input_max is None:
        return True
    if row.input_min is not None:
        if row.input_min_exclusive:
            if tokens <= row.input_min:
                return False
        elif tokens < row.input_min:
            return False
    if row.input_max is not None:
        if row.input_max_exclusive:
            if tokens >= row.input_max:
                return False
        elif tokens > row.input_max:
            return False
    return True


def _covers(day: str, row: PriceQuote) -> bool:
    if row.effective_from and day < row.effective_from:
        return False
    if row.effective_until and day > row.effective_until:
        return False
    return True


def _narrow(
    rows: list,
    *,
    at: Optional[datetime],
    input_tokens: Optional[float],
    cache: str,
    region: Optional[str],
    schedule: str,
) -> list:
    if not rows:
        return rows
    tagged_cache = [row for row in rows if row.cache]
    if tagged_cache:
        wanted = cache or "miss"
        rows = [row for row in rows if row.cache in (None, "", wanted)]
    plain = [row for row in rows if not row.meter]
    if plain and len(plain) < len(rows):
        rows = plain
    regions = {row.region for row in rows if row.region}
    if len(regions) > 1:
        wanted_region = region or "cn-beijing"
        narrowed = [row for row in rows if row.region in (None, "", wanted_region)]
        if narrowed:
            rows = narrowed
    if any(row.effective_from or row.effective_until for row in rows):
        day = _as_utc(at).date().isoformat()
        rows = [row for row in rows if _covers(day, row)]
    tiers = {row.tier for row in rows if row.tier in {"peak", "off-peak"}}
    if tiers and schedule:
        wanted_tier = peak_tier(schedule, at)
        rows = [row for row in rows if row.tier in (None, "", wanted_tier)]
    if any(row.input_min is not None or row.input_max is not None for row in rows):
        if input_tokens is None:
            return rows
        rows = [row for row in rows if _in_input_window(row, float(input_tokens))]
    return rows


def _token_pair(request: QuoteRequest, model_id: str) -> Optional[dict]:
    """Sum documented input and output prices. A missing side is not treated as zero."""
    if request.unit:
        return None
    mode = (request.modality or "").strip().lower()
    if mode in {"image", "video", "tts"}:
        return None
    if request.input_tokens is None and request.output_tokens is None:
        return None
    if request.input_tokens is None or request.output_tokens is None:
        return _blank(model_id, unknown=False, reason="both input and output token counts are required")
    incoming = _number(request.input_tokens)
    outgoing = _number(request.output_tokens)
    if incoming is None or outgoing is None:
        return _blank(model_id, unknown=False, reason="token counts must be finite numbers >= 0")
    input_row = _quote_unit(model_id, _INPUT_UNITS, request)
    output_row = _quote_unit(model_id, _OUTPUT_UNITS, request)
    if input_row is None or output_row is None:
        return _blank(model_id, unknown=False, reason="no documented input and output token price")
    if input_row.currency != output_row.currency:
        return _blank(model_id, unknown=False, reason="input and output prices use different currencies")
    total = _token_amount(input_row, incoming) + _token_amount(output_row, outgoing)
    priced = _ready(input_row, total, total, unit="per_1k_tokens")
    priced["unit_amount"] = None
    priced["source_url"] = source_url(input_row)
    return priced


def _image_assets(request: QuoteRequest) -> Optional[float]:
    if request.asset_count is not None:
        return _number(request.asset_count)
    if request.quantity is not None:
        return _number(request.quantity)
    return 1.0


def _scale(row: PriceQuote, request: QuoteRequest) -> dict:
    if row.unit == PriceUnit.PER_IMAGE:
        assets = _image_assets(request)
        if assets is None:
            return _blank(row.model_id, unknown=False, reason="image asset count must be a finite number >= 0")
        span = image_cost_range(assets, row.amount)
        return _ready(row, span["min"], span["max"])
    if row.unit == PriceUnit.PER_SECOND:
        seconds = _number(request.quantity)
        if request.quantity is None:
            return _blank(row.model_id, unknown=False, reason="duration in seconds is required")
        if seconds is None:
            return _blank(row.model_id, unknown=False, reason="duration must be a finite number >= 0")
        total = seconds * row.amount
        return _ready(row, total, total)
    if row.unit == PriceUnit.PER_MILLION_TOKENS:
        if request.completion_tokens is None and request.quantity is None:
            return _blank(
                row.model_id,
                unknown=False,
                reason="completion token quantity is required; no per-second price is documented",
            )
        tokens = _number(
            request.completion_tokens if request.completion_tokens is not None else request.quantity
        )
        if tokens is None:
            return _blank(row.model_id, unknown=False, reason="token quantity must be a finite number >= 0")
        total = row.amount * tokens / 1_000_000
        return _ready(row, total, total)
    if row.unit in {PriceUnit.PER_1K_INPUT_TOKEN, PriceUnit.PER_1K_OUTPUT_TOKEN}:
        raw = request.quantity
        if row.unit == PriceUnit.PER_1K_INPUT_TOKEN and request.input_tokens is not None:
            raw = request.input_tokens
        if row.unit == PriceUnit.PER_1K_OUTPUT_TOKEN and request.output_tokens is not None:
            raw = request.output_tokens
        if raw is None:
            return _blank(row.model_id, unknown=False, reason="token quantity is required")
        tokens = _number(raw)
        if tokens is None:
            return _blank(row.model_id, unknown=False, reason="token quantity must be a finite number >= 0")
        total = row.amount * tokens / 1000.0
        return _ready(row, total, total)
    if row.unit in _MILLION_UNITS:
        raw = request.quantity
        if row.unit == PriceUnit.PER_MILLION_INPUT_TOKENS and request.input_tokens is not None:
            raw = request.input_tokens
        if row.unit == PriceUnit.PER_MILLION_OUTPUT_TOKENS and request.output_tokens is not None:
            raw = request.output_tokens
        if row.unit == PriceUnit.PER_MILLION_TOKENS and request.completion_tokens is not None:
            raw = request.completion_tokens
        if raw is None:
            return _blank(row.model_id, unknown=False, reason="token quantity is required")
        tokens = _number(raw)
        if tokens is None:
            return _blank(row.model_id, unknown=False, reason="token quantity must be a finite number >= 0")
        total = row.amount * tokens / 1_000_000
        return _ready(row, total, total)
    if row.unit == PriceUnit.PER_10K_CHARACTERS:
        characters = _number(request.quantity)
        if request.quantity is None:
            return _blank(row.model_id, unknown=False, reason="character quantity is required")
        if characters is None:
            return _blank(row.model_id, unknown=False, reason="character quantity must be a finite number >= 0")
        total = row.amount * characters / 10_000
        return _ready(row, total, total)
    return _blank(row.model_id, unknown=False, reason="price unit cannot be scaled")


def quote(request: QuoteRequest) -> dict:
    """Price one call as a min/max range.

    Image assets use 1..6 calls. A model with no documented API price is
    returned in ``unknown_models`` with ``min`` and ``max`` left unset.
    """
    if not isinstance(request, QuoteRequest):
        raise TypeError("quote expects a QuoteRequest")
    model_id = ModelIds.normalize(request.model_id)
    if not model_id:
        return _blank("", unknown=False, reason="model id is empty")
    from models.middle.registry import ModelRegistry

    spec = ModelRegistry.get(model_id)
    if spec is None or not any(documented(price) for price in spec.prices):
        return _blank(model_id, unknown=True, reason="no documented API price")
    paired = _token_pair(request, spec.id)
    if paired is not None:
        return paired
    row = PriceBook.quote(
        spec.id,
        unit=request.unit or None,
        resolution=request.resolution,
        includes_video_input=request.includes_video_input,
        documented_only=True,
        at=request.at,
        input_tokens=request.input_tokens,
        region=request.region,
    )
    if row is None:
        return _blank(
            spec.id,
            unknown=False,
            reason="no single documented price matches this request",
        )
    return _scale(row, request)
