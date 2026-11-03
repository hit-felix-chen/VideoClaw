"""Seed the one-episode dependency graph stored on a v2 project.

The shape matches the graph the executor already runs: script and storyboard
first, then character and reference images, then one video node, then TTS.
Nothing here calls a model.
"""

from __future__ import annotations

from typing import Any

from graph.dependency_graph import DependencyGraph
from storage.projects import GraphStore

EPISODE_ID = "ep1"

# Same token quantity the one-episode executor test uses to price Seedance.
_VIDEO_COMPLETION_TOKENS = 1_000_000
_TTS_CHARACTERS = 10000


def _model_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _metadata(idea: str, **extra: object) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if idea:
        data["text"] = idea
    for key, value in extra.items():
        if value is None:
            continue
        data[key] = value
    return data


def seed_episode_graph(
    conn,
    project_id: str,
    *,
    idea: str = "",
    models: dict | None = None,
) -> dict[str, Any]:
    """Write one episode graph onto ``project_id`` and return the stored document."""
    models = models or {}
    text = idea.strip() if isinstance(idea, str) else ""
    llm = _model_id(models.get("llm_model"))
    image = _model_id(models.get("image_model"))
    video = _model_id(models.get("video_model"))
    tts = _model_id(models.get("tts_model"))

    graph = DependencyGraph()
    graph.add_node(
        "script",
        kind="script",
        episode_id=EPISODE_ID,
        label="剧本",
        metadata=_metadata(text, model_id=llm),
    )
    graph.add_node(
        "storyboard",
        kind="storyboard",
        episode_id=EPISODE_ID,
        label="分镜",
        metadata=_metadata(text, model_id=llm),
    )
    image_meta = _metadata(text, model_id=image, resolution="720p")
    graph.add_node(
        "character",
        kind="character",
        episode_id=EPISODE_ID,
        label="角色",
        metadata=image_meta,
    )
    graph.add_node(
        "reference",
        kind="keyframe",
        episode_id=EPISODE_ID,
        label="参考图",
        metadata=dict(image_meta),
    )
    graph.add_node(
        "video",
        kind="shot_segment",
        episode_id=EPISODE_ID,
        label="视频",
        metadata=_metadata(
            text,
            model_id=video,
            resolution="720p",
            includes_video_input=False,
            completion_tokens=_VIDEO_COMPLETION_TOKENS,
            shot_duration=6,
        ),
    )
    graph.add_node(
        "tts",
        kind="tts",
        episode_id=EPISODE_ID,
        label="配音",
        metadata=_metadata(
            text,
            model_id=tts,
            quantity=_TTS_CHARACTERS,
            unit="per_10k_characters",
        ),
    )
    for predecessor, successor in (
        ("script", "storyboard"),
        ("storyboard", "character"),
        ("storyboard", "reference"),
        ("character", "video"),
        ("reference", "video"),
        ("video", "tts"),
    ):
        graph.add_edge(predecessor, successor)
    return GraphStore.save(conn, project_id, graph.to_dict())
