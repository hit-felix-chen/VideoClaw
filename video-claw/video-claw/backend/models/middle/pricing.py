"""Price lookup for R3.

``quote`` returns one ``PriceQuote`` or None. ``cost`` multiplies that quote
by a quantity only when the quote is unique. No price, an empty id, or more
than one matching row produces None — never a zero and never another model's
price.
"""

from dataclasses import asdict, dataclass
from typing import Optional

from models.middle.ids import ModelIds
from models.middle.spec import CapabilityText, PriceQuote, PriceUnit


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
    ) -> Optional[PriceQuote]:
        rows = PriceBook.prices(model_id)
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
        quote = PriceBook.quote(
            model_id,
            unit=unit,
            resolution=resolution,
            includes_video_input=includes_video_input,
        )
        if quote is None:
            return None
        priced = _basis(quote, quantity, completion_tokens)
        if priced is None:
            return None
        basis, total = priced
        return CostQuote(
            model_id=quote.model_id,
            amount=quote.amount,
            currency=quote.currency,
            unit=quote.unit,
            total=round(float(total), 6),
            quantity=basis,
            verified=quote.verified,
            resolution=quote.resolution,
            includes_video_input=quote.includes_video_input,
        )
