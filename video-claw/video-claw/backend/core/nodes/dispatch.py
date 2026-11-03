"""Dispatch one graph call to the agent that owns that node.

The default provider is this function's return value. It does not import a
vendor client and does not open a network connection. Tests read
``submitted`` to see what would have been sent.
"""

from __future__ import annotations

from core.agents.character_agent import CharacterDesignerAgent
from core.agents.reference_agent import ReferenceGeneratorAgent
from core.agents.script_agent import ScriptWriterAgent
from core.agents.storyboard_agent import StoryboardAgent
from core.agents.video_agent import VideoDirectorAgent

submitted: list[dict] = []

_VIDEO_KINDS = frozenset({"shot_segment", "segment", "video", "draft_video", "final_video"})
_IMAGE_KINDS = frozenset({"character", "scene", "setting", "keyframe", "image"})


def dispatch_node(call: dict) -> dict:
    modality = str(call.get("modality") or "").strip().lower()
    kind = str(call.get("kind") or "").strip().lower()
    if modality == "video" or kind in _VIDEO_KINDS:
        result = VideoDirectorAgent().execute_node(call)
    elif modality == "tts" or kind == "tts":
        result = {
            "provider_called": False,
            "modality": "tts",
            "kind": kind or "tts",
            "model_id": call.get("model_id") or "edge-tts",
            "text": call.get("text") or "",
        }
    elif modality == "image" or kind in _IMAGE_KINDS:
        if kind == "character":
            result = CharacterDesignerAgent().execute_node(call)
        else:
            result = ReferenceGeneratorAgent().execute_node(call)
    elif kind == "script":
        result = ScriptWriterAgent().execute_node(call)
    elif kind == "storyboard":
        result = StoryboardAgent().execute_node(call)
    else:
        result = {"provider_called": False, "kind": kind, "modality": modality}
    if not isinstance(result, dict):
        result = {"provider_called": False}
    result.setdefault("provider_called", False)
    submitted.append(
        {
            "node_id": call.get("node_id"),
            "kind": kind,
            "modality": result.get("modality") or modality,
            "model_id": result.get("model_id") or call.get("model_id"),
            "generation_mode": result.get("generation_mode"),
        }
    )
    return result
