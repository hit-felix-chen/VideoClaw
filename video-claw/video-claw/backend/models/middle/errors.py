"""Errors the middle layer raises instead of silently changing a request."""

from typing import Any, Sequence


class UnsupportedParam(ValueError):
    """A caller asked for a value this model's spec does not allow."""

    def __init__(self, name: str, value: Any, allowed: Sequence[Any]):
        self.name = name
        self.value = value
        self.allowed = list(allowed)
        shown = ", ".join(str(item) for item in self.allowed) or "(none)"
        super().__init__(f"Unsupported {name}={value!r}. Allowed: {shown}")
