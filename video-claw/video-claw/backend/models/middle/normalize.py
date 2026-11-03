"""Map a unified generation request onto one model's spec.

Unsupported values raise ``UnsupportedParam`` with the allowed set. They are
not rewritten to a nearby default. A missing resolution stays missing unless
the caller already chose a default.
"""

from typing import Any, Optional

from models.middle.errors import UnsupportedParam
from models.middle.spec import CapabilityText


class ParamNormalizer:
    @staticmethod
    def paths(value: Any) -> list:
        if not value:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return [str(item) for item in value if str(item).strip()]

    @staticmethod
    def limits(spec, params: dict) -> dict:
        refs = ParamNormalizer.paths(params.get("reference_image_paths"))
        if not refs:
            refs = ParamNormalizer.paths(params.get("image_paths"))
        videos = ParamNormalizer.paths(params.get("reference_video_paths"))
        audios = ParamNormalizer.paths(params.get("reference_audio_paths"))
        if params.get("reference_audio_path"):
            audios = ParamNormalizer.paths([params.get("reference_audio_path"), *audios])
        ParamNormalizer._cap("reference_image_paths", refs, spec.max_reference_images)
        ParamNormalizer._cap("reference_video_paths", videos, spec.max_reference_videos)
        ParamNormalizer._cap("reference_audio_paths", audios, spec.max_reference_audios)
        out = dict(params)
        if "reference_image_paths" in params or refs:
            out["reference_image_paths"] = refs
        return out

    @staticmethod
    def _cap(name: str, values: list, limit: Optional[int]) -> None:
        if limit is None or len(values) <= limit:
            return
        allowed = [f"0..{limit}"]
        raise UnsupportedParam(name, len(values), allowed)

    @staticmethod
    def video(spec, params: dict, *, resolution_case: str = "lower") -> dict:
        incoming = ParamNormalizer.limits(spec, params)
        resolution = ParamNormalizer._resolution(spec, incoming.get("resolution"), resolution_case)
        ratio = ParamNormalizer._ratio(spec, incoming)
        duration = ParamNormalizer._duration(spec, incoming.get("duration"))
        shot_type = ParamNormalizer._shot_type(spec, incoming.get("shot_type"))
        refs = ParamNormalizer.paths(incoming.get("reference_image_paths"))
        mode = ParamNormalizer._mode(incoming, refs)
        ParamNormalizer._exclusive(mode, incoming, refs)
        if ratio is not None:
            ParamNormalizer._ratio_for_mode(spec, mode, ratio)
        out = dict(incoming)
        if resolution is not None:
            out["resolution"] = resolution
        if ratio is not None:
            out["ratio"] = ratio
            out["video_ratio"] = ratio
        if duration is not None:
            out["duration"] = duration
        if shot_type:
            out["shot_type"] = shot_type
        out["reference_image_paths"] = refs
        out["generation_mode"] = mode
        return out

    @staticmethod
    def _resolution(spec, value: Any, case: str) -> Optional[str]:
        if value is None or str(value).strip() == "":
            return None
        token = CapabilityText.resolution(value)
        allowed = list(spec.resolutions or [])
        if allowed and token not in allowed:
            raise UnsupportedParam("resolution", value, allowed)
        return CapabilityText.vendor_resolution(token, case)

    @staticmethod
    def _ratio(spec, params: dict) -> Optional[str]:
        raw = params.get("ratio", params.get("video_ratio"))
        if raw is None or str(raw).strip() == "":
            return None
        token = CapabilityText.ratio(raw)
        allowed = list(spec.aspect_ratios or [])
        if allowed and token not in allowed:
            raise UnsupportedParam("ratio", raw, allowed)
        return token

    @staticmethod
    def _duration(spec, value: Any) -> Any:
        if value is None or value == "":
            return None
        limit = spec.duration
        try:
            number = float(value)
        except (TypeError, ValueError):
            if limit is None:
                return value
            raise UnsupportedParam("duration", value, limit.choices()) from None
        if limit is None:
            return int(number) if number.is_integer() else number
        if limit.allow_auto and number == -1:
            return -1
        if limit.integer and not number.is_integer():
            raise UnsupportedParam("duration", value, limit.choices())
        seconds = int(number) if limit.integer else number
        if limit.min_seconds is not None and seconds < limit.min_seconds:
            raise UnsupportedParam("duration", value, limit.choices())
        if limit.max_seconds is not None and seconds > limit.max_seconds:
            raise UnsupportedParam("duration", value, limit.choices())
        return seconds

    @staticmethod
    def _shot_type(spec, value: Any) -> Any:
        if not value:
            return value
        abilities = set(spec.abilities or [])
        abilities.update(spec.adapter_abilities or [])
        if value == "multi" and "multi_shot" not in abilities:
            return "single"
        return value

    @staticmethod
    def _mode(params: dict, refs: list) -> str:
        if refs:
            return "reference"
        image = params.get("image_path")
        last = params.get("last_image_path")
        if image and last:
            return "start_end_frame"
        if image:
            return "first_frame"
        return "text"

    @staticmethod
    def _exclusive(mode: str, params: dict, refs: list) -> None:
        if mode != "reference":
            return
        if params.get("image_path") or params.get("last_image_path"):
            raise UnsupportedParam(
                "reference_image_paths",
                "mixed with first_frame or last_frame",
                ["reference_image", "first_frame", "start_end_frame"],
            )

    @staticmethod
    def _ratio_for_mode(spec, mode: str, ratio: str) -> None:
        allowed = (spec.ratio_by_mode or {}).get(mode)
        if allowed and ratio not in allowed:
            raise UnsupportedParam("ratio", ratio, allowed)
