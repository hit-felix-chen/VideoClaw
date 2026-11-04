"""First-frame and first-plus-last-frame requests for a video node.

Keyframe images are library asset ids. ``ImageStore`` must already point at
a ``BlobStore`` blob, and that blob must be a file on disk. This module does
not download, extract, or upload a frame.

Seedance 2.5 (the default reference-to-video model) takes those files on the
create-task body as ``content[]`` items. Official Ark fields, not invented
names:

- ``type``: ``image_url``
- ``image_url.url``: a public URL or ``data:image/<format>;base64,...``
- ``role``: ``first_frame`` for the opening image
- ``role``: ``last_frame`` for the closing image (required when both are sent)
- ``ratio``: ``adaptive`` for a first-frame or first-and-last-frame task

First frame, first-and-last frame, and multimodal reference
(``reference_image`` / ``reference_video`` / ``reference_audio``) are three
mutually exclusive tasks. A model whose registered adapter abilities do not
include ``first_frame_i2v`` or ``start_end_frame_i2v`` is refused in Chinese
before a quote is authorized.

Sources checked 2026-11-03:

- https://ark.volcengine.com/region:cn-beijing/docs/seedance-2-5
- https://ark.volcengine.com/region:cn-beijing/docs/create-video-generation-task-api
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

FIRST_FRAME = "first_frame"
START_END = "start_end_frame"
FIRST_ABILITY = "first_frame_i2v"
END_ABILITY = "start_end_frame_i2v"
FRAME_KEYS = ("first_frame_asset_id", "last_frame_asset_id")

MIXED_REFERENCES = "首帧与参考图不能同时使用"
LAST_WITHOUT_FIRST = "只指定了尾帧，没有首帧"


class KeyframeRejected(Exception):
    """The call cannot be quoted. The message is already Chinese."""


def _clean(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def frames_specified(params: dict) -> bool:
    """True when the caller sent a frame field, including an empty string."""
    return any(key in params and params.get(key) is not None for key in FRAME_KEYS)


def frame_id(params: dict, metadata: object, key: str) -> Optional[str]:
    """Explicit empty string clears. A missing field keeps the stored id."""
    if key in params and params.get(key) is not None:
        return _clean(params.get(key))
    stored = metadata.get(key) if isinstance(metadata, dict) else None
    return _clean(stored)


def requested(fields: dict) -> bool:
    return bool(_clean(fields.get("first_frame_asset_id")) or _clean(fields.get("last_frame_asset_id")))


def unsupported_message(model_id: str, mode: str) -> str:
    if mode == START_END:
        return f"模型 {model_id} 不支持首尾帧生视频"
    return f"模型 {model_id} 不支持首帧生视频"


def _abilities(spec) -> set:
    listed = list(getattr(spec, "adapter_abilities", None) or [])
    if not listed:
        listed = list(getattr(spec, "abilities", None) or [])
    return {item for item in listed if isinstance(item, str)}


def supports_mode(spec, mode: str) -> bool:
    needed = END_ABILITY if mode == START_END else FIRST_ABILITY
    return needed in _abilities(spec)


def _mode(first: Optional[str], last: Optional[str]) -> str:
    if last:
        return START_END
    return FIRST_FRAME


def _missing_message(which: str) -> str:
    if which == "last":
        return "尾帧资产不在资产库中"
    return "首帧资产不在资产库中"


def disk_image(conn, asset_id: str) -> Optional[str]:
    """On-disk file for a library image, or None when the asset has none."""
    from storage.assets import ImageStore
    from storage.blobs import BlobStore
    from storage.database import StorageError

    try:
        rows = ImageStore.list(conn, asset_id=asset_id)
    except StorageError:
        return None
    for row in reversed(rows):
        digest = row.get("blob_hash")
        if not isinstance(digest, str) or not digest:
            continue
        try:
            info = BlobStore.info(conn, digest)
        except StorageError:
            continue
        path = info.get("path")
        if isinstance(path, str) and Path(path).is_file():
            return path
    return None


def _require_asset(conn, asset_id: str, which: str) -> str:
    path = disk_image(conn, asset_id)
    if path is None:
        raise KeyframeRejected(_missing_message(which))
    return path


def _ratio_for(spec, mode: str, fields: dict) -> Optional[str]:
    table = getattr(spec, "ratio_by_mode", None) or {}
    allowed = [item for item in (table.get(mode) or []) if isinstance(item, str) and item]
    chosen = _clean(fields.get("ratio")) or _clean(fields.get("video_ratio"))
    if not allowed:
        return chosen
    if chosen and chosen not in allowed:
        label = "首尾帧" if mode == START_END else "首帧"
        joined = "、".join(allowed)
        raise KeyframeRejected(f"模型 {spec.id} 的{label}生视频只允许宽高比 {joined}")
    if chosen:
        return chosen
    if len(allowed) == 1:
        return allowed[0]
    return None


def prepare(conn, fields: dict, references: dict) -> dict:
    """Check a video quote before authorize. Unchanged when no frame was asked for."""
    from core.agents.video_agent import _reference_asset_ids
    from models.middle.registry import ModelRegistry

    first = _clean(fields.get("first_frame_asset_id"))
    last = _clean(fields.get("last_frame_asset_id"))
    if not first and not last:
        return fields
    if last and not first:
        raise KeyframeRejected(LAST_WITHOUT_FIRST)
    _require_asset(conn, first, "first")
    if last:
        _require_asset(conn, last, "last")
    if _reference_asset_ids(references):
        raise KeyframeRejected(MIXED_REFERENCES)
    model_id = _clean(fields.get("model_id")) or ""
    spec = ModelRegistry.get(model_id)
    mode = _mode(first, last)
    if spec is None:
        raise KeyframeRejected(f"模型 {model_id} 未注册，不能提交首帧")
    if not supports_mode(spec, mode):
        raise KeyframeRejected(unsupported_message(spec.id, mode))
    updated = dict(fields)
    updated["first_frame_asset_id"] = first
    updated["last_frame_asset_id"] = last
    ratio = _ratio_for(spec, mode, updated)
    if ratio:
        updated["ratio"] = ratio
        updated["video_ratio"] = ratio
    return updated


def runner_result(spec, call: dict) -> Optional[dict]:
    """The prepared video body when this call uses keyframes. None otherwise.

    Asset ids stay in the result. Disk paths are attached later, immediately
    before ``adapter.submit``.
    """
    from core.agents.video_agent import _capability_duration, _reference_asset_ids
    from models.middle.normalize import ParamNormalizer

    first = _clean(call.get("first_frame_asset_id"))
    last = _clean(call.get("last_frame_asset_id"))
    if not first and not last:
        return None
    model_id = getattr(spec, "id", None) or call.get("model_id") or ""
    if last and not first:
        return _error(model_id, LAST_WITHOUT_FIRST)
    if _reference_asset_ids(call.get("references")):
        return _error(model_id, MIXED_REFERENCES)
    mode = _mode(first, last)
    if not supports_mode(spec, mode):
        return _error(model_id, unsupported_message(str(model_id), mode))
    params = {
        "prompt": call.get("text") or "",
        "reference_image_paths": [],
        "resolution": call.get("resolution") or spec.default_resolution,
    }
    try:
        ratio = _ratio_for(spec, mode, call)
    except KeyframeRejected as exc:
        return _error(model_id, str(exc))
    if ratio:
        params["ratio"] = ratio
    duration = _capability_duration(spec, call.get("shot_duration"))
    if duration is not None:
        params["duration"] = duration
    normalized = ParamNormalizer.video(spec, params)
    ability = END_ABILITY if mode == START_END else FIRST_ABILITY
    body = {
        "provider_called": False,
        "modality": "video",
        "model_id": spec.id,
        "ability": ability,
        "generation_mode": mode,
        "reference_image_paths": [],
        "first_frame_asset_id": first,
        "last_frame_asset_id": last,
        "duration": normalized.get("duration"),
        "resolution": normalized.get("resolution"),
        "truncated": False,
        "reference_limit": spec.max_reference_images,
    }
    sent_ratio = normalized.get("ratio") or ratio
    if sent_ratio:
        body["ratio"] = sent_ratio
        body["video_ratio"] = sent_ratio
    return body


def bind_frame_files(conn, params: dict) -> dict:
    """Replace frame asset ids with on-disk files. Ids are not sent as URLs."""
    mode = params.get("generation_mode")
    if mode not in {FIRST_FRAME, START_END}:
        return params
    first = _clean(params.get("first_frame_asset_id"))
    path = disk_image(conn, first) if first else None
    if path is None:
        raise RuntimeError("reference image file is missing")
    updated = dict(params)
    updated["image_path"] = path
    updated["reference_image_paths"] = []
    if mode == START_END:
        last = _clean(params.get("last_frame_asset_id"))
        last_path = disk_image(conn, last) if last else None
        if last_path is None:
            raise RuntimeError("reference image file is missing")
        updated["last_image_path"] = last_path
    return updated


def _error(model_id: str, message: str) -> dict:
    return {
        "provider_called": False,
        "modality": "video",
        "model_id": model_id,
        "error": message,
    }
