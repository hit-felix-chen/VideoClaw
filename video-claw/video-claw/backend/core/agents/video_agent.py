# -*- coding: utf-8 -*-
"""
阶段5: 视频生成智能体
- 从编排器注入的 artifacts["storyboard"] 读取拍摄片段(Segments)
- 视频提示词：风格控制 + 人物列表 + 分镜列表(分镜1: [时长] content...)
- 参考图：从 artifacts["reference_generation"] 读取
- 支持逐项并发生成、实时预览、重新生成
"""

import os
import re
import asyncio
import logging
import threading
from typing import Any, Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base_agent import AgentInterface

logger = logging.getLogger(__name__)


class VideoDirectorAgent(AgentInterface):
    """视频生成：拍摄片段(Segments) → 组装提示词 → 视频片段"""

    def __init__(self):
        super().__init__(name="VideoDirector")

    # ─── 版本管理 ───

    @staticmethod
    def _video_base(sid: str) -> str:
        return os.path.join('code/result/video', str(sid))

    def _list_versions(self, sid: str, segment_id: str) -> List[str]:
        """List version files for this segment id only.

        ``seg_1`` does not include ``seg_10``. Final cuts (``*_final.mp4``)
        are not versions. This is not the reference-to-video path.
        """
        video_dir = self._video_base(sid)
        if not os.path.isdir(video_dir):
            return []
        pattern = re.compile(
            rf"^{re.escape(segment_id)}(?:_v\d+)?\.mp4$",
            re.IGNORECASE,
        )
        files = [
            os.path.join(video_dir, name)
            for name in os.listdir(video_dir)
            if pattern.match(name)
        ]
        return sorted(set(files), key=os.path.getmtime)

    def _next_version_path(self, sid: str, segment_id: str) -> str:
        """获取下一个版本路径"""
        video_dir = self._video_base(sid)
        os.makedirs(video_dir, exist_ok=True)

        existing = self._list_versions(sid, segment_id)
        if not existing:
            return os.path.join(video_dir, f"{segment_id}.mp4")

        max_v = 1
        for fp in existing:
            bn = os.path.splitext(os.path.basename(fp))[0]
            m = re.search(r'_v(\d+)$', bn)
            if m:
                max_v = max(max_v, int(m.group(1)))

        return os.path.join(video_dir, f"{segment_id}_v{max_v + 1}.mp4")

    # ─── 视频生成 ───

    def _generate_video_with_doctor(self, client, *, prompt: str, model: str,
                                    llm_model: str, context: dict, **kwargs) -> tuple:
        """Generate once; retry with a doctor-rewrite prompt only for prompt issues."""
        try:
            client.generate_video(prompt=prompt, model=model, **kwargs)
            return prompt, None, None
        except Exception as exc:
            from .doctor_agent import DoctorAgent

            doctor = DoctorAgent(llm_model=llm_model)
            rewrite, diagnosis, rewrite_result = doctor.maybe_rewrite_prompt(
                stage="video_generation",
                model=model,
                prompt=prompt,
                error=str(exc),
                context=context,
            )
            if not rewrite:
                logger.info("Doctor skipped video prompt rewrite: %s", diagnosis.get("reason"))
                raise

            logger.info("Doctor rewrote video prompt: reason_type=%s reason=%s",
                        diagnosis.get("reason_type"), diagnosis.get("reason"))
            client.generate_video(prompt=rewrite, model=model, **kwargs)
            return rewrite, diagnosis, rewrite_result

    def _generate_one(self, sid: str, segment_id: str, prompt: str,
                      img_path: Optional[str], video_model: str,
                      duration: int = 10, sound: str = "",
                      shot_type: str = "multi",
                      video_ratio: str = "16:9",
                      video_resolution: str = "720P",
                      video_generation_mode: str = "first_frame",
                      last_image_path: Optional[str] = None,
                      reference_image_paths: Optional[List[str]] = None,
                      llm_model: str = "") -> tuple:
        """生成单个视频片段，返回 (segment_id, path_or_None, rewrite_result_or_None)。"""
        if self.cancellation_check and self.cancellation_check():
            logger.info(f"VideoDirectorAgent: {segment_id} 跳过（用户取消）")
            return segment_id, None, None

        reference_image_paths = reference_image_paths or []
        if video_generation_mode == "reference":
            missing_refs = [path for path in reference_image_paths if not os.path.exists(path)]
            if not reference_image_paths or missing_refs:
                logger.warning("Reference images missing for %s: %s", segment_id, missing_refs or reference_image_paths)
                return segment_id, None, None
        elif not img_path or not os.path.exists(img_path):
            logger.warning(f"Image missing for {segment_id}: {img_path}")
            return segment_id, None, None

        save_path = self._next_version_path(sid, segment_id)
        try:
            from models.video_client import VideoClient
            client = VideoClient()
            _, _, rewrite_result = self._generate_video_with_doctor(
                client,
                prompt=prompt,
                model=video_model,
                llm_model=llm_model,
                context={
                    "segment_id": segment_id,
                    "video_generation_mode": video_generation_mode,
                    "duration": duration,
                    "video_ratio": video_ratio,
                    "video_resolution": video_resolution,
                },
                image_path=img_path,
                save_path=save_path,
                duration=duration,
                sound=sound,
                shot_type=shot_type,
                video_ratio=video_ratio,
                resolution=video_resolution,
                last_image_path=last_image_path if video_generation_mode == "start_end_frame" else None,
                reference_image_paths=reference_image_paths if video_generation_mode == "reference" else None,
            )
            return segment_id, save_path, rewrite_result
        except Exception as e:
            logger.error(f"Video gen failed for {segment_id}: {e}")
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
        return segment_id, None, None

    # ─── 提示词组装 ───

    def _format_shot_section(self, segment: dict) -> str:
        prompt = "分镜列表："
        shots = segment.get("shots", [])
        for i, shot in enumerate(shots, 1):
            dur = shot.get("duration", 5)
            content = shot.get("content", "").strip()
            prompt += f"\n分镜{i}：[{dur}秒] {content}"
        return prompt

    @staticmethod
    def _extract_shot_section(prompt: str) -> str:
        text = (prompt or "").strip()
        if not text:
            return "分镜列表："
        marker = "分镜列表："
        alt_marker = "分镜列表:"
        if marker in text:
            text = text[text.index(marker):].strip()
        elif alt_marker in text:
            text = marker + text[text.index(alt_marker) + len(alt_marker):].strip()
        else:
            text = f"{marker}\n{text}"
        no_caption_marker = "\n不要生成字幕或水印"
        if no_caption_marker in text:
            text = text[:text.index(no_caption_marker)].rstrip()
        return text

    def _display_prompt(self, segment: dict, video_data: Optional[dict] = None) -> str:
        """前端只展示可编辑的分镜列表；风格和人物信息在调用视频模型前即时拼接。"""
        if video_data and video_data.get("description"):
            return self._extract_shot_section(video_data["description"])
        return self._format_shot_section(segment)

    def _build_character_section(self, segment: dict, character_artifact: Optional[dict]) -> str:
        characters = character_artifact.get("characters", []) if isinstance(character_artifact, dict) else []
        if not characters:
            return "人物列表：无"

        character_map = self._build_name_asset_map(characters)
        selected_assets = []
        seen = set()

        for character_name in segment.get("characters") or []:
            asset = self._match_asset_by_name(str(character_name), characters, character_map)
            asset_key = str(asset.get("id") or asset.get("name") or "") if asset else ""
            if asset and asset_key not in seen:
                selected_assets.append(asset)
                seen.add(asset_key)

        lines = []
        for asset in selected_assets:
            name = str(asset.get("name") or asset.get("id") or "").strip()
            desc = str(asset.get("description") or "").strip()
            if name and desc:
                lines.append(f"- {name}：{desc}")
            elif name:
                lines.append(f"- {name}")

        return "人物列表：\n" + "\n".join(lines) if lines else "人物列表：无"

    def _assemble_prompt(self, segment: dict, style_prompt: str,
                         character_artifact: Optional[dict] = None,
                         video_data: Optional[dict] = None) -> str:
        """组装视频提示词
        格式：
        风格控制：用户选择的风格提示词, 电影质感
        人物列表：从第二阶段人物描述读取
        分镜列表：分镜1:[时长] content... 分镜2:[时长] content...
        """
        if video_data and "description" in video_data:
            # 前端修改后的提示词会存入 artifacts.video_generation.clips.description。
            # 兼容旧格式：保留用户修改过的分镜部分，重新补齐最新风格和人物列表。
            shot_section = self._extract_shot_section(video_data["description"])
        else:
            shot_section = self._format_shot_section(segment)

        return (
            f"风格控制：{style_prompt}\n"
            f"{self._build_character_section(segment, character_artifact)}\n"
            f"{shot_section}\n"
            "不要生成字幕或水印"
        )

    def _get_style_keywords(self, session_data: dict) -> str:
        """从会话数据获取风格关键词"""
        style = session_data.get('style', 'realistic').lower()
        
        STYLE_MAP = {
            "anime": "anime style, vibrant colors, clean lines,",
            "realistic": "photorealistic, cinematic lighting, high-detail textures,",
            "cartoon": "cartoon style, thick outlines, bold colors,",
            "3d-disney": "3D CGI animation, Disney/Pixar style, smooth textures,",
            "oil-painting": "oil painting, artistic brushstrokes, rich textures,",
            "chinese-ink": "Chinese ink wash painting, traditional style, soft strokes,"
        }
        return STYLE_MAP.get(style, "cinematic, high quality,")

    # ─── 参考图获取 ───

    def _get_reference_image(self, sid: str, segment_id: str, scene_map: dict) -> str:
        """获取参考图路径：优先用选中的版本，次之用最新版本"""
        # 1. 检查 session 中 artifacts.reference_generation.scenes 里的 selected
        if segment_id in scene_map and scene_map[segment_id].get("selected"):
            path = scene_map[segment_id]["selected"]
            if os.path.exists(path):
                return path

        # 2. 回退：扫描磁盘 Scenes 目录
        from .reference_agent import ReferenceGeneratorAgent
        versions = ReferenceGeneratorAgent._list_versions_static(sid, segment_id)
        if versions:
            return versions[-1]

        # 3. 默认路径
        return os.path.abspath(os.path.join('code/result/image', str(sid), 'Scenes', f"{segment_id}.jpg"))

    def _get_next_reference_image(self, sid: str, segment_index: int, segments: list, scene_map: dict) -> Optional[str]:
        """首尾帧模式下，优先用下一个片段参考图作为尾帧。"""
        if segment_index + 1 >= len(segments):
            return None
        next_segment_id = segments[segment_index + 1].get("segment_id")
        if not next_segment_id:
            return None
        path = self._get_reference_image(sid, next_segment_id, scene_map)
        return path if path and os.path.exists(path) else None

    @staticmethod
    def _asset_selected_path(asset: dict) -> str:
        selected = asset.get("selected") or ""
        if selected and os.path.exists(selected):
            return selected
        # Legacy session compatibility: some old artifacts only have versions and no selected field.
        for path in reversed(asset.get("versions") or []):
            if path and os.path.exists(path):
                return path
        return ""

    @staticmethod
    def _build_name_asset_map(assets: list[dict]) -> dict[str, dict]:
        """Build name -> asset mapping from character_design characters/settings."""
        mapping = {}
        for asset in assets:
            name = str(asset.get("name") or "").strip()
            asset_id = str(asset.get("id") or "").strip()
            if name:
                mapping[name] = asset
            if asset_id:
                mapping[asset_id] = asset
        return mapping

    @staticmethod
    def _match_asset_by_name(name: str, assets: list[dict], asset_map: Optional[dict[str, dict]] = None) -> Optional[dict]:
        clean_name = (name or "").strip()
        if not clean_name:
            return None
        if asset_map and clean_name in asset_map:
            return asset_map[clean_name]
        for asset in assets:
            asset_name = str(asset.get("name") or "").strip()
            if asset_name and asset_name == clean_name:
                return asset
        for asset in assets:
            asset_name = str(asset.get("name") or "").strip()
            if asset_name and (asset_name in clean_name or clean_name in asset_name):
                return asset
        return None

    def _get_segment_reference_assets(self, segment: dict, character_artifact: dict) -> List[str]:
        """参考图生视频：按 segment.characters/location 读取第二阶段用户选中的人物图和场景图。"""
        characters = character_artifact.get("characters", []) if isinstance(character_artifact, dict) else []
        settings = character_artifact.get("settings", []) if isinstance(character_artifact, dict) else []
        character_map = self._build_name_asset_map(characters)
        setting_map = self._build_name_asset_map(settings)
        reference_paths: List[str] = []
        seen = set()

        def add_asset(asset: Optional[dict]) -> None:
            if not asset:
                return
            path = self._asset_selected_path(asset)
            if path and path not in seen:
                reference_paths.append(path)
                seen.add(path)

        location = str(segment.get("location") or "").strip()
        add_asset(self._match_asset_by_name(location, settings, setting_map))

        for character_name in segment.get("characters") or []:
            add_asset(self._match_asset_by_name(str(character_name), characters, character_map))

        return reference_paths

    @staticmethod
    def _select_video_model(input_data: dict, session_meta: dict) -> tuple[str, str]:
        mode = (
            input_data.get("video_generation_mode")
            or session_meta.get("video_generation_mode")
            or "first_frame"
        )
        model_key = {
            "first_frame": "video_first_frame_model",
            "start_end_frame": "video_start_end_model",
            "reference": "video_reference_model",
        }.get(mode, "video_first_frame_model")
        model = input_data.get(model_key) or session_meta.get(model_key)
        if not model:
            # Legacy session compatibility: sessions created before mode-specific video models only have video_model.
            model = input_data.get("video_model") or session_meta.get("video_model")
        if not model:
            raise ValueError("Missing required model configuration: video_model")
        return mode, model

    # ─── 预览 / Payload ───

    @staticmethod
    def _collect_rewrite_results(video_clips: Optional[list]) -> dict:
        results = {}
        for clip in video_clips or []:
            if isinstance(clip, dict) and clip.get("id") and clip.get("rewrite_result"):
                results[clip["id"]] = clip["rewrite_result"]
        return results

    def _build_preview(self, sid: str, segments: list, scene_map: dict,
                       video_clips: Optional[list] = None) -> list:
        preview = []
        clip_map = {c.get("id"): c for c in (video_clips or []) if isinstance(c, dict) and c.get("id")}
        existing_rewrite_results = self._collect_rewrite_results(video_clips)
        for idx, seg in enumerate(segments, 1):
            segment_id = seg["segment_id"]
            versions = self._list_versions(sid, segment_id)
            ep_n = seg.get('episode_number', 1)
            seg_n = seg.get('segment_number', idx)
            item = {
                "id": segment_id,
                "name": f"第{ep_n}集-片段{seg_n}",
                "episode": ep_n,
                "index": seg_n,
                "description": self._display_prompt(seg, clip_map.get(segment_id)),
                "duration": seg.get('total_duration', 10),
                "selected": versions[-1] if versions else "",
                "versions": versions,
                "status": "done" if versions else "pending",
            }
            if existing_rewrite_results.get(segment_id):
                item["rewrite_result"] = existing_rewrite_results[segment_id]
            preview.append(item)
        return preview

    def _build_payload(self, sid: str, segments: list, video_clips: Optional[list] = None,
                       rewrite_results: Optional[dict] = None, *,
                       stale_ids: Optional[list] = None,
                       fresh_ids: Optional[list] = None) -> dict:
        clips = []
        clip_map = {c.get("id"): c for c in (video_clips or []) if isinstance(c, dict) and c.get("id")}
        existing_rewrite_results = self._collect_rewrite_results(video_clips)
        rewrite_results = rewrite_results or {}
        for idx, seg in enumerate(segments, 1):
            segment_id = seg["segment_id"]
            versions = self._list_versions(sid, segment_id)
            ep_n = seg.get('episode_number', 1)
            seg_n = seg.get('segment_number', idx)
            item = {
                "id": segment_id,
                "name": f"第{ep_n}集-片段{seg_n}",
                "episode": ep_n,
                "index": seg_n,
                "description": self._display_prompt(seg, clip_map.get(segment_id)),
                "duration": seg.get('total_duration', 10),
                "selected": versions[-1] if versions else "",
                "versions": versions,
                "status": "done" if versions else "failed",
            }
            rewrite_result = rewrite_results.get(segment_id) or existing_rewrite_results.get(segment_id)
            if rewrite_result:
                item["rewrite_result"] = rewrite_result
            existing_clip = clip_map.get(segment_id)
            prior_stale = isinstance(existing_clip, dict) and bool(existing_clip.get("stale"))
            fresh = set(fresh_ids) if fresh_ids is not None else None
            stale = set(stale_ids or [])
            if fresh is not None and segment_id in fresh:
                item["stale"] = False
            elif segment_id in stale or prior_stale:
                item["stale"] = True
            clips.append(item)
        return {
            "payload": {
                "session_id": sid,
                "clips": clips,
            },
            "stage_completed": True,
        }


    def execute_node(self, call: dict) -> dict:
        """Build a Seedance reference-to-video request.

        Reference ids come from ``SegmentRefStore.resolve`` output already
        stored on the call. File-name prefixes are not searched.
        """
        from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
        from models.middle.normalize import ParamNormalizer
        from models.middle.registry import ModelRegistry

        model_id = call.get("model_id") or DEFAULT_REFERENCE_VIDEO_MODEL
        spec = ModelRegistry.get(model_id)
        if spec is None:
            return {
                "provider_called": False,
                "modality": "video",
                "model_id": model_id,
                "error": "model is not registered",
            }
        paths = _reference_asset_ids(call.get("references"))
        limit = spec.max_reference_images
        truncated = False
        if isinstance(limit, int) and not isinstance(limit, bool) and len(paths) > limit:
            paths = paths[:limit]
            truncated = True
        params = {
            "prompt": call.get("text") or "",
            "reference_image_paths": paths,
            "resolution": call.get("resolution") or spec.default_resolution,
        }
        duration = _capability_duration(spec, call.get("shot_duration"))
        if duration is not None:
            params["duration"] = duration
        normalized = ParamNormalizer.video(spec, params)
        return {
            "provider_called": False,
            "modality": "video",
            "model_id": spec.id,
            "ability": "reference_to_video",
            "generation_mode": normalized.get("generation_mode"),
            "reference_image_paths": list(normalized.get("reference_image_paths") or []),
            "duration": normalized.get("duration"),
            "resolution": normalized.get("resolution"),
            "truncated": truncated,
            "reference_limit": limit,
        }


def _reference_asset_ids(references) -> list:
    """Mention order, first resolved asset only, no filename matching."""
    if not isinstance(references, dict):
        return []
    found = []
    for mention in references.get("mentions") or []:
        if not isinstance(mention, dict) or not mention.get("resolved"):
            continue
        for match in mention.get("matches") or []:
            if not isinstance(match, dict):
                continue
            asset_id = match.get("asset_id")
            if isinstance(asset_id, str) and asset_id and asset_id not in found:
                found.append(asset_id)
                break
    return found


def _capability_duration(spec, shot_duration):
    """Use a shot length only when it sits inside the model's verified range."""
    limit = spec.duration
    if limit is None or not limit.verified:
        return None
    if isinstance(shot_duration, (int, float)) and not isinstance(shot_duration, bool):
        number = float(shot_duration)
        if not limit.integer or number.is_integer():
            seconds = int(number) if limit.integer else number
            lower_ok = limit.min_seconds is None or seconds >= limit.min_seconds
            upper_ok = limit.max_seconds is None or seconds <= limit.max_seconds
            if lower_ok and upper_ok:
                return seconds
    default = limit.default
    if isinstance(default, int) and not isinstance(default, bool) and default > 0:
        return default
    if limit.allow_auto:
        return -1
    return None
