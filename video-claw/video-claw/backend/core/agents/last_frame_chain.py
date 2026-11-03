# -*- coding: utf-8 -*-
"""Episode-scoped last-frame continuity for first-frame video generation.

Grouping uses the episode field already stored on segments and clips.
Segment ids such as seg_01_01 are never parsed.
"""

import os
from typing import Callable, Optional


class LastFrameChain:
    """Static helpers for chaining, cascade, and stale flags."""

    @staticmethod
    def active(chain_last_frame, mode: str) -> bool:
        """Chain only in first-frame mode. start_end_frame and reference stay as they are."""
        return bool(chain_last_frame) and mode == "first_frame"

    @staticmethod
    def episode_key(segment: dict):
        if not isinstance(segment, dict):
            return None
        if "episode" in segment and segment.get("episode") not in (None, ""):
            return segment.get("episode")
        if "episode_number" in segment and segment.get("episode_number") not in (None, ""):
            return segment.get("episode_number")
        return None

    @staticmethod
    def group_segments(segments: list) -> list:
        """Preserve input order. Segments with no episode stay in their own group."""
        groups: list = []
        index = {}
        for seg in segments or []:
            if not isinstance(seg, dict) or not seg.get("segment_id"):
                continue
            key = LastFrameChain.episode_key(seg)
            bucket = key if key is not None else ("segment", seg["segment_id"])
            if bucket not in index:
                index[bucket] = len(groups)
                groups.append([])
            groups[index[bucket]].append(seg)
        return groups

    @staticmethod
    def frame_path(sid: str, segment_id: str) -> str:
        return os.path.join(
            "code", "result", "image", str(sid), "Frames", f"{segment_id}_last.jpg",
        )

    @staticmethod
    def plan_first_frames(
        segments: list,
        *,
        chain: bool,
        sid: str,
        reference_for: Callable[[dict], str],
    ) -> list:
        """Shot order and first-frame path.

        With chain off, every shot keeps its reference still and its own
        parallel group, in the flattened list order. With chain on, episodes
        are parallel groups and only the first shot of an episode uses the
        reference still.
        """
        plans = []
        if not chain:
            for position, seg in enumerate(segments or []):
                if not isinstance(seg, dict) or not seg.get("segment_id"):
                    continue
                reference_path = reference_for(seg)
                plans.append({
                    "segment_id": seg["segment_id"],
                    "episode": LastFrameChain.episode_key(seg),
                    "reference_path": reference_path,
                    "img_path": reference_path,
                    "chained": False,
                    "prev_id": None,
                    "parallel_group": position,
                })
            return plans

        for group_index, group in enumerate(LastFrameChain.group_segments(segments)):
            for index, seg in enumerate(group):
                reference_path = reference_for(seg)
                prev = group[index - 1] if index else None
                prev_id = prev.get("segment_id") if prev else None
                plans.append({
                    "segment_id": seg["segment_id"],
                    "episode": LastFrameChain.episode_key(seg),
                    "reference_path": reference_path,
                    "img_path": LastFrameChain.frame_path(sid, prev_id) if prev_id else reference_path,
                    "chained": prev_id is not None,
                    "prev_id": prev_id,
                    "parallel_group": group_index,
                })
        return plans

    @staticmethod
    def expand_cascade(regen_ids: list, segments: list, cascade: bool) -> list:
        """Keep the requested ids. With cascade, append later ids in the same episode."""
        ordered = [rid for rid in (regen_ids or []) if rid]
        if not cascade:
            return ordered
        following = {}
        for group in LastFrameChain.group_segments(segments):
            ids = [seg.get("segment_id") for seg in group]
            for segment_id in ids:
                following[segment_id] = ids
        seen = set(ordered)
        expanded = list(ordered)
        for rid in list(ordered):
            ids = following.get(rid)
            if not ids or rid not in ids:
                continue
            for later in ids[ids.index(rid) + 1:]:
                if later and later not in seen:
                    expanded.append(later)
                    seen.add(later)
        return expanded

    @staticmethod
    def stale_ids(regenerated_ids: list, segments: list) -> list:
        """Later same-episode shots that were not regenerated still use the old frame."""
        regenerated = set(regenerated_ids or [])
        stale = []
        seen = set()
        for group in LastFrameChain.group_segments(segments):
            ids = [seg.get("segment_id") for seg in group if seg.get("segment_id")]
            for index, segment_id in enumerate(ids):
                if segment_id not in regenerated:
                    continue
                for later in ids[index + 1:]:
                    if later not in regenerated and later not in seen:
                        stale.append(later)
                        seen.add(later)
        return stale

    @staticmethod
    def selected_video_path(clip: Optional[dict], versions: Optional[list]) -> str:
        """Prefer the clip's selected file, then the newest existing version."""
        selected = ""
        if isinstance(clip, dict):
            selected = clip.get("selected") or ""
        if selected and os.path.exists(selected):
            return selected
        for path in reversed(versions or []):
            if path and os.path.exists(path):
                return path
        return ""
