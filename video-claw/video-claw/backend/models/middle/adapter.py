"""Provider adapter interface.

A new vendor subclasses ``ProviderAdapter``, registers one instance, and
sets ``owns_submit`` when it should replace the legacy name-substring
router. Existing image and VLM adapters keep ``owns_submit`` false so the
current clients stay on their call path. Video adapters own submit because
reference-image requests have to reach Seedance.
"""

import logging
from typing import Any, Optional

from models.middle.errors import UnsupportedParam

logger = logging.getLogger(__name__)

# Adapter cancel outcomes. Only ``cancelled`` means the vendor task is gone
# and a reserved quote may be released. ``completed`` means the task finished
# and must be booked. The other three leave the quote and the node alone.
CANCEL_CANCELLED = "cancelled"
CANCEL_COMPLETED = "completed"
CANCEL_REJECTED = "rejected"
CANCEL_UNSUPPORTED = "unsupported"
CANCEL_UNCERTAIN = "uncertain"
CANCEL_OUTCOMES = frozenset(
    {
        CANCEL_CANCELLED,
        CANCEL_COMPLETED,
        CANCEL_REJECTED,
        CANCEL_UNSUPPORTED,
        CANCEL_UNCERTAIN,
    }
)


def cancel_result(
    outcome: str,
    message: str,
    *,
    video_url: Optional[str] = None,
    task_status: Optional[str] = None,
    http_status: Optional[int] = None,
) -> dict:
    """One cancel attempt. ``http_status`` is the observed code, not a guess."""
    return {
        "outcome": outcome,
        "message": message,
        "video_url": video_url,
        "task_status": task_status,
        "http_status": http_status,
    }


class ProviderAdapter:
    """Normalize a unified request and hand it to one vendor client."""

    provider = ""
    providers = frozenset()
    modalities = frozenset()
    owns_submit = False
    resolution_case = "lower"

    def matches(self, spec, modality: Optional[str] = None) -> bool:
        if spec is None or spec.provider not in self.providers:
            return False
        if modality is None:
            return any(mode in spec.modalities for mode in self.modalities)
        return modality in self.modalities and modality in spec.modalities

    def specs(self) -> list:
        from models.middle.registry import ModelRegistry

        return [
            spec
            for spec in ModelRegistry.all()
            if any(self.matches(spec, mode) for mode in self.modalities)
        ]

    def normalize(self, spec, params: dict) -> dict:
        from models.middle.normalize import ParamNormalizer

        return ParamNormalizer.limits(spec, dict(params))

    def submit(self, spec, params: dict, *, transport: Any = None) -> Any:
        raise NotImplementedError(f"{type(self).__name__} does not submit")

    def poll(self, spec, task_id: str, *, transport: Any = None) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__} has no separate poll; submit waits for the asset"
        )

    def fetch(self, spec, ref: Any, *, transport: Any = None) -> Any:
        raise NotImplementedError(f"{type(self).__name__} has no separate fetch")

    def cancel(self, task_id: str, *, transport: Any = None) -> dict:
        """Vendor cancel. The default is unsupported: no HTTP call is made.

        A concrete adapter may override this only for an officially documented
        cancel. ``outcome`` is ``cancelled``, ``completed``, ``rejected``,
        ``unsupported``, or ``uncertain``. Callers release a reserved quote
        only for ``cancelled``.
        """
        del task_id, transport
        return cancel_result(
            CANCEL_UNSUPPORTED,
            "该视频厂商没有已核实的取消接口，报价保持保留，节点状态不变。",
        )

    def quote(self, spec, usage: Optional[dict] = None):
        from models.middle.pricing import PriceBook

        usage = usage or {}
        return PriceBook.quote(
            spec.id,
            unit=usage.get("unit"),
            resolution=usage.get("resolution"),
            includes_video_input=usage.get("includes_video_input"),
        )

    @staticmethod
    def classify_error(error: BaseException) -> str:
        if isinstance(error, UnsupportedParam):
            return "unsupported_param"
        if isinstance(error, TimeoutError):
            return "timeout"
        if isinstance(error, FileNotFoundError):
            return "missing_input"
        text = str(error).lower()
        if "401" in text or "unauthorized" in text or "api key" in text or "api_key" in text:
            return "auth"
        if "429" in text or "rate limit" in text or "rate_limit" in text:
            return "rate_limit"
        return "provider_error"


class ProviderDispatch:
    """Send a request to an adapter that owns submit. Otherwise return None."""

    @staticmethod
    def run(transport: Any, modality: str, model: str, params: dict) -> Any:
        from models.middle.registry import ModelRegistry

        spec = ModelRegistry.get(model)
        adapter = ModelRegistry.adapter_for(spec, modality)
        if adapter is None or not adapter.owns_submit:
            return None
        normalized = adapter.normalize(spec, params)
        logger.info("ProviderDispatch %s %s via %s", modality, spec.id, type(adapter).__name__)
        return adapter.submit(spec, normalized, transport=transport)
