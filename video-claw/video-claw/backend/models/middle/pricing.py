"""Documented prices, pre-call ranges, and the unknown-model list.

Reuse: unit lookup on ``PriceBook``, Seedance rows that already cite the
Volcengine price page, and the 1..6 image-call span previously inlined in
``CostEstimate``. The session ``cost_estimate`` route is not this surface
and is left as it was.

A row is an API price only when it is verified, has a currency, and has an
http(s) ``source_url``. ``MODEL_CONFIG`` amounts fail that test. ``quote``
does not turn them into a number. Those models are listed by
``unknown_models``.
"""

import math
from dataclasses import asdict, dataclass
from typing import Optional

from models.middle.ids import ModelIds
from models.middle.spec import CapabilityText, PriceQuote, PriceUnit

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


def _basis(quote: PriceQuote, quantity: Optional[float], completion_tokens: Optional[float]):
    if quote.unit == PriceUnit.PER_MILLION_TOKENS:
        if completion_tokens is None:
            return None
        tokens = float(completion_tokens)
        return tokens, quote.amount * tokens / 1_000_000
    if quote.unit in {PriceUnit.PER_1K_INPUT_TOKEN, PriceUnit.PER_1K_OUTPUT_TOKEN}:
        if quantity is None:
            return None
        tokens = float(quantity)
        return tokens, quote.amount * tokens / 1000.0
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
                        "verified": True,
                    }
                )
        rows.sort(key=lambda item: (item["model_id"], item["resolution"] or "", item["amount"]))
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
    input_row = PriceBook.quote(
        model_id,
        unit=PriceUnit.PER_1K_INPUT_TOKEN,
        documented_only=True,
    )
    output_row = PriceBook.quote(
        model_id,
        unit=PriceUnit.PER_1K_OUTPUT_TOKEN,
        documented_only=True,
    )
    if input_row is None or output_row is None:
        return _blank(model_id, unknown=False, reason="no documented per-1k-token price")
    if input_row.currency != output_row.currency:
        return _blank(model_id, unknown=False, reason="input and output prices use different currencies")
    total = input_row.amount * incoming / 1000.0 + output_row.amount * outgoing / 1000.0
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
    )
    if row is None:
        return _blank(
            spec.id,
            unknown=False,
            reason="no single documented price matches this request",
        )
    return _scale(row, request)
