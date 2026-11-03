"""Exact model-id matching.

Empty strings never match. A path prefix (``models/…`` or ``api/…/…``) is
stripped, then the remaining id must equal a registered id. Substring
overlap is not a match: ``"" in key`` used to price every lookup as the
first model.
"""

from typing import Any, Optional

# API model id from the official create-task sample. The price page calls
# the same model doubao-seedance-2.5; that name is an explicit alias below.
DEFAULT_REFERENCE_VIDEO_MODEL = "doubao-seedance-2-5-260628"

_PREFIXES = {"api", "models"}
_ALIASES = {
    "doubao-seedance-2.5": DEFAULT_REFERENCE_VIDEO_MODEL,
    # Display name only. No vendor publishes this string as an API id.
    "deepseek-4.1-flash": "deepseek-flash",
}


class ModelIds:
    """Normalize and resolve model ids. All methods are stateless."""

    @staticmethod
    def normalize(model_id: Optional[Any]) -> str:
        if model_id is None:
            return ""
        text = str(model_id).strip()
        if not text:
            return ""
        parts = [part.strip() for part in text.replace("\\", "/").split("/") if part.strip()]
        if len(parts) >= 2 and parts[0].casefold() in _PREFIXES:
            return parts[-1]
        return text

    @staticmethod
    def alias(model_id: Optional[Any]) -> str:
        """Return a canonical id for an explicit alias, or ``""``."""
        key = ModelIds.normalize(model_id)
        if not key:
            return ""
        if key in _ALIASES:
            return _ALIASES[key]
        folded = key.casefold()
        for alias, canonical in _ALIASES.items():
            if alias.casefold() == folded:
                return canonical
        return ""

    @staticmethod
    def exact(models: dict, model_id: Optional[Any]) -> Optional[Any]:
        """Return the metadata stored under this id, or None.

        Case-insensitive only when exactly one registered id folds to the
        same string. An empty id returns None instead of the first row.
        """
        key = ModelIds.normalize(model_id)
        if not key or not isinstance(models, dict):
            return None
        if key in models:
            return models[key]
        folded = key.casefold()
        hits = [models[candidate] for candidate in models if str(candidate).casefold() == folded]
        if len(hits) == 1:
            return hits[0]
        return None
