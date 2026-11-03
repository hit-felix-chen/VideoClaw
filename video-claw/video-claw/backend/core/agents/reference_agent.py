# -*- coding: utf-8 -*-
"""
阶段4: 参考图生成智能体
- 基于阶段3分镜(shots)，为每个分镜生成「首帧图像提示词」，再据此生成参考图
- 首帧提示词由 LLM 根据 shot 的 plot、visual_prompt、duration 生成
- 阶段5生视频时使用阶段3的原始分镜描述，而非首帧提示词
- 支持逐项实时预览、重新生成、多版本管理
"""

import os
import re
import json
import asyncio
import logging
from typing import Any, Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base_agent import AgentInterface
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)


def ratio_to_size(ratio: str) -> str:
    """将视频比例转换为图像尺寸"""
    size_map = {
        "16:9": "1920*1080",
        "9:16": "1080*1920",
        "1:1": "1024*1024",
        "4:3": "1024*768",
        "3:4": "768*1024",
        "21:9": "2560*1080",
    }
    return size_map.get(ratio, "1920*1080")


class ReferenceGeneratorAgent(AgentInterface):
    """参考图生成：分镜(阶段3) → 首帧提示词(LLM) → 参考图(图像模型)"""

    def __init__(self):
        super().__init__(name="ReferenceGenerator")

    # ─── 版本管理 ───

    @staticmethod
    def _scenes_base(sid: str) -> str:
        return os.path.join('code/result/image', str(sid), 'Scenes')

    def _list_versions(self, sid: str, shot_id: str) -> List[str]:
        """列出某个分镜的所有历史版本
        命名: shot_001_01.jpg, shot_001_01_v2.jpg, ...
        """
        return self._list_versions_static(sid, shot_id)

    @staticmethod
    def _list_versions_static(sid: str, shot_id: str) -> List[str]:
        """List version files for this shot id only. ``shot_1`` does not include ``shot_10``."""
        scenes_dir = os.path.join('code/result/image', str(sid), 'Scenes')
        if not os.path.isdir(scenes_dir):
            return []
        pattern = re.compile(
            rf"^{re.escape(shot_id)}(?:_v\d+)?\.(?:png|jpg|jpeg|webp|bmp)$",
            re.IGNORECASE,
        )
        files = [
            os.path.join(scenes_dir, name)
            for name in os.listdir(scenes_dir)
            if pattern.match(name)
        ]
        return sorted(set(files), key=os.path.getmtime)

    def _next_version_path(self, sid: str, shot_id: str) -> str:
        """获取下一个版本路径"""
        scenes_dir = self._scenes_base(sid)
        os.makedirs(scenes_dir, exist_ok=True)

        existing = self._list_versions(sid, shot_id)
        if not existing:
            return os.path.join(scenes_dir, f"{shot_id}.jpg")

        max_v = 1
        for fp in existing:
            bn = os.path.splitext(os.path.basename(fp))[0]
            m = re.search(r'_v(\d+)$', bn)
            if m:
                max_v = max(max_v, int(m.group(1)))

        return os.path.join(scenes_dir, f"{shot_id}_v{max_v + 1}.jpg")

    # ─── 素材匹配 ───

    @staticmethod
    def _build_asset_map(character_design: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
        """从阶段2生成的素材数据中构建映射，不再直接扫描磁盘"""
        am: Dict[str, Dict[str, str]] = {'characters': {}, 'settings': {}}
        
        # 处理角色
        for char in character_design.get('characters', []):
            cid = char.get('id') or char.get('character_id')
            selected = char.get('selected')
            if cid and selected:
                am['characters'][cid] = selected
                
        # 处理场景
        for setting in character_design.get('settings', []):
            sid = setting.get('id') or setting.get('setting_id')
            selected = setting.get('selected')
            if sid and selected:
                am['settings'][sid] = selected
                
        return am

    def _collect_refs(self, segment: dict, asset_map: dict,
                      char_id_map: dict, setting_id_map: dict) -> List[str]:
        """为一个片段(Segment)收集参考原图路径（角色 + 场景素材）"""
        refs = []
        # 1. 角色匹配
        for cn in segment.get('characters', []):
            cid = char_id_map.get(cn)
            # 如果名称不直接匹配，尝试模糊匹配（部分包含）
            if not cid:
                for name, _id in char_id_map.items():
                    if name in cn or cn in name:
                        cid = _id
                        break
            
            if cid and cid in asset_map['characters']:
                refs.append(os.path.abspath(asset_map['characters'][cid]))
                logger.info(f"[{segment.get('segment_id', '')}] 添加角色参考图: {cn} -> {cid}")

        # 2. 场景匹配
        loc = segment.get('location', '')
        set_id = setting_id_map.get(loc)
        # 如果名称不直接匹配，尝试模糊匹配
        if not set_id and loc:
            for name, _id in setting_id_map.items():
                if name in loc or loc in name:
                    set_id = _id
                    logger.info(f"[{segment.get('segment_id', '')}] 场景模糊匹配成功: {loc} -> {name} ({set_id})")
                    break

        if set_id and set_id in asset_map['settings']:
            refs.append(os.path.abspath(asset_map['settings'][set_id]))
            logger.info(f"[{segment.get('segment_id', '')}] 添加场景参考图: {loc} -> {set_id}")
        else:
            logger.warning(f"[{segment.get('segment_id', '')}] 未找到场景参考图: location={loc}, set_id={set_id}, available_settings={list(asset_map['settings'].keys())}")
        
        logger.info(f"[{segment.get('segment_id', '')}] 共收集 {len(refs)} 张参考图")
        return refs[:10]

    def _get_descriptions(self, segment: dict, char_id_map: dict, setting_id_map: dict,
                          character_json: dict) -> tuple:
        """获取片段中涉及的角色和场景描述

        Returns:
            (character_description, setting_description)
        """
        # 角色描述
        char_descs = []
        for cn in segment.get('characters', []):
            cid = char_id_map.get(cn, '')
            if cid:
                for c in character_json.get('characters', []):
                    if (c.get('id') or c.get('character_id')) == cid:
                        desc = c.get('description', '')
                        if desc:
                            char_descs.append(f"{cn}: {desc}")
                        break

        # 场景描述
        loc = segment.get('location', '')
        set_id = setting_id_map.get(loc)
        setting_desc = ""
        if set_id:
            for s in character_json.get('settings', []):
                if (s.get('id') or s.get('setting_id')) == set_id:
                    setting_desc = s.get('description', '')
                    break

        return "； ".join(char_descs), setting_desc

    # ─── 首帧提示词生成 ───

    # ─── 预览构建 ───

    def _build_preview(self, sid: str, segments: list, session_data: dict = None) -> list:
        """构建片段预览列表（含当前状态）"""
        preview = []

        # 建立 scene_id 到 selected 路径的映射
        selected_map = {}
        if session_data and "artifacts" in session_data:
            ref_gen = session_data["artifacts"].get("reference_generation", {})
            for scene in ref_gen.get("scenes", []):
                sid_in_json = scene.get("id")
                if sid_in_json:
                    selected_map[sid_in_json] = scene.get("selected", "")

        for idx, seg in enumerate(segments, 1):
            segment_id = seg.get('segment_id', f'seg_unk_{idx}')
            versions = self._list_versions(sid, segment_id)

            # 优先从 artifacts 中读取 selected 字段，如果没有则回退到最后一个版本
            selected_path = selected_map.get(segment_id)
            if not selected_path:
                selected_path = versions[-1] if versions else ""

            # 获取该段下第一个镜头的 content 作为片段描述
            plot = seg.get('shots', [])[0].get('content', '') if seg.get('shots') else ""
            ep_n = seg.get('episode_number', 1)
            seg_n = seg.get('segment_number', idx)

            preview.append({
                "id": segment_id,
                "name": f"第{ep_n}集-片段{seg_n}",
                "episode": ep_n,
                "index": seg_n,
                "description": plot,
                "selected": selected_path,
                "versions": versions,
                "status": "done" if versions else "pending",
            })
        return preview

    # ─── 单张生成 ───

    def _generate_image_with_doctor(self, img_client, *, prompt: str, model: str,
                                    llm_model: str, context: dict, **kwargs) -> tuple:
        """Generate once; if doctor rewrites a prompt-related failure, retry once."""
        try:
            paths = img_client.generate_image(prompt=prompt, model=model, **kwargs)
            if not paths:
                raise RuntimeError("Image generation returned no output")
            return paths, prompt, None
        except Exception as exc:
            from .doctor_agent import DoctorAgent

            doctor = DoctorAgent(llm_model=llm_model)
            rewrite, diagnosis, rewrite_result = doctor.maybe_rewrite_prompt(
                stage="reference_generation",
                model=model,
                prompt=prompt,
                error=str(exc),
                context=context,
            )
            if not rewrite:
                logger.info("Doctor skipped reference prompt rewrite: %s", diagnosis.get("reason"))
                raise

            logger.info("Doctor rewrote reference prompt: reason_type=%s reason=%s",
                        diagnosis.get("reason_type"), diagnosis.get("reason"))
            paths = img_client.generate_image(prompt=rewrite, model=model, **kwargs)
            if not paths:
                raise RuntimeError("Image generation returned no output after doctor rewrite")
            return paths, rewrite, rewrite_result

    @staticmethod
    def _apply_eval_feedback_to_visual_prompt(current_prompt: str, eval_result: dict, version: int) -> str:
        suggested_prompt = (eval_result.get('suggested_prompt') or '').strip()
        if suggested_prompt:
            return suggested_prompt

        hard_failures = eval_result.get('hard_failures') or []
        soft_issues = eval_result.get('soft_issues') or []
        issues = eval_result.get('issues') or []
        suggestion = (eval_result.get('suggestion') or '').strip()

        feedback_lines = []
        if hard_failures:
            feedback_lines.append("硬性失败项：" + "；".join(map(str, hard_failures)))
        if issues:
            feedback_lines.append("主要问题：" + "；".join(map(str, issues)))
        if soft_issues:
            feedback_lines.append("软性问题：" + "；".join(map(str, soft_issues)))
        if suggestion:
            feedback_lines.append("修改建议：" + suggestion)
        if not feedback_lines:
            return current_prompt

        return (
            f"{current_prompt}\n\n"
            f"【第{version + 1}轮VLM评估反馈】\n"
            f"上一轮参考图未通过评估，请在下一轮生成时优先修正以下问题；不要改变角色核心外貌和场景核心设定：\n"
            + "\n".join(f"- {line}" for line in feedback_lines)
        )

    def _generate_one(self, img_client, sid: str, segment: dict,
                      first_frame_prompt: str, refs: List[str],
                      style: str, it2i_model: str, t2i_model: str,
                      video_ratio: str = "16:9", resolution: str = "1080P", vlm_model: str = "qwen3.7-plus",
                      character_description: str = "", setting_description: str = "",
                      llm_model: str = "",
                      max_versions: int = 3) -> tuple:
        """生成单个片段参考图，返回 (segment_id, path_or_None, eval_result)

        最多生成 max_versions 个版本，如果所有版本都没有达到硬性合格标准，
        使用 VLM 选择最好的一张作为最终参考图。
        """
        segment_id = segment.get('segment_id', '')

        # 仅提取第一个镜头的描述作为当前 Plot
        plot = segment.get('shots', [])[0].get('content', '') if segment.get('shots') else ""
        current_visual_prompt = first_frame_prompt

        # 取消时直接跳过，不抛异常，以保留已生成的部分结果
        if self.cancellation_check and self.cancellation_check():
            logger.info(f"ReferenceGeneratorAgent: {segment_id} 跳过（用户取消）")
            return segment_id, None, None, None

        model = it2i_model if refs else t2i_model
        logger.info(f"[{segment_id}] 使用模型: {model}, 参考图数量: {len(refs) if refs else 0}")
        if refs:
            for i, r in enumerate(refs):
                logger.info(f"[{segment_id}] 参考图[{i}]: {r}")

        # 收集所有生成的版本
        all_versions = []
        all_eval_results = []

        for version in range(max_versions):
            self._check_cancel()

            style_prompt = self._get_style_prompt(style)
            full_prompt = f"{style_prompt}, {current_visual_prompt}"

            save_path = self._next_version_path(sid, segment_id)
            save_dir = os.path.dirname(save_path)

            try:
                paths, used_prompt, rewrite_result = self._generate_image_with_doctor(
                    img_client,
                    prompt=full_prompt,
                    model=model,
                    llm_model=llm_model,
                    context={
                        "segment_id": segment_id,
                        "location": segment.get("location", ""),
                        "characters": segment.get("characters", []),
                        "plot": plot,
                    },
                    image_paths=refs if refs else None,
                    session_id=str(sid),
                    save_dir=save_dir,
                    video_ratio=video_ratio,
                    resolution=resolution,
                )

                gen = paths[0]
                if gen != save_path:
                    if os.path.exists(save_path):
                        os.remove(save_path)
                    os.rename(gen, save_path)

                # VLM 评估
                eval_result = self._evaluate_with_vlm(save_path, segment, plot, current_visual_prompt,
                                                      character_description=character_description,
                                                      setting_description=setting_description,
                                                      vlm_model=vlm_model)

                score = eval_result.get('score', 0)
                hard_failures = eval_result.get('hard_failures') or []
                if 'is_acceptable' in eval_result:
                    is_acceptable = bool(eval_result.get('is_acceptable')) and not hard_failures
                else:
                    is_acceptable = not hard_failures and score >= 7

                logger.info(f"[{segment_id}] 版本{version + 1}: 评分 {score}/10, {'✓通过' if is_acceptable else '✗不通过'}")
                if hard_failures:
                    logger.warning(f"[{segment_id}] 硬性失败项: {hard_failures}")

                # 记录版本信息
                eval_result["final_visual_prompt"] = current_visual_prompt
                if used_prompt != full_prompt:
                    eval_result["doctor_rewrite_prompt"] = used_prompt
                if rewrite_result:
                    eval_result["rewrite_result"] = rewrite_result
                all_versions.append(save_path)
                all_eval_results.append(eval_result)

                # 如果 VLM 判定达到硬性标准，立即返回
                if is_acceptable:
                    return segment_id, save_path, eval_result, rewrite_result

                # 报告进度
                if version < max_versions - 1:
                    current_visual_prompt = self._apply_eval_feedback_to_visual_prompt(current_visual_prompt, eval_result, version)
                    logger.info(f"[{segment_id}] 下一轮将使用VLM反馈优化首帧提示词")
                    self._report_progress("参考图", f"重新生成中 ({version + 2}/{max_versions}): {segment_id}", 0)

            except Exception as e:
                logger.error(f"Segment {segment_id} image generation failed: {e}")

        # 所有版本都没有达到硬性标准，使用 VLM 选择最好的
        if all_versions:
            logger.warning(f"[{segment_id}] 所有版本都未达到硬性合格标准，使用VLM选择最佳...")
            best_path, best_eval = self._select_best_with_vlm(
                all_versions, segment, plot, current_visual_prompt,
                character_description=character_description,
                setting_description=setting_description,
                vlm_model=vlm_model
            )
            if best_path and isinstance(best_eval, dict):
                best_eval["final_visual_prompt"] = current_visual_prompt
                hard_failures = best_eval.get("hard_failures") or []
                is_acceptable = bool(best_eval.get("is_acceptable")) and not hard_failures
                if is_acceptable:
                    return segment_id, best_path, best_eval, best_eval.get("rewrite_result") if isinstance(best_eval, dict) else None
                if hard_failures:
                    logger.warning(
                        "[%s] VLM选择的最佳图仍有硬性失败项，保留为候选图供人工确认: %s",
                        segment_id,
                        hard_failures,
                    )
                    return segment_id, best_path, best_eval, best_eval.get("rewrite_result") if isinstance(best_eval, dict) else None
                # 低分但已有候选图属于可人工确认的软失败，保留最佳图作为候选，避免把阶段误标为 failed。
                logger.warning(
                    "[%s] VLM选择的最佳图未达分数阈值，仍作为低分候选图保留: %s",
                    segment_id,
                    best_eval.get("issues", []),
                )
                return segment_id, best_path, best_eval, best_eval.get("rewrite_result") if isinstance(best_eval, dict) else None

        # 如果没有任何生成成功
        logger.warning(f"[{segment_id}] 没有成功生成任何图片")
        if all_eval_results and isinstance(all_eval_results[-1], dict):
            all_eval_results[-1]["final_visual_prompt"] = current_visual_prompt
        return segment_id, None, None, None

    def _select_best_with_vlm(self, image_paths: List[str], segment: dict, plot: str, visual_prompt: str,
                              character_description: str = "", setting_description: str = "",
                              vlm_model: str = "qwen3.7-plus") -> tuple:
        """使用 VLM 从多个版本中选择最好的一张"""
        from models.vlm_client import VLM

        if not image_paths:
            return None, None

        segment_id = segment.get('segment_id', '')

        # 加载评估提示词
        select_prompt = load_prompt('reference', 'eval_select_best', 'zh').format(
            num_images=len(image_paths),
            num_images_minus_1=len(image_paths) - 1,
            plot=plot,
            visual_prompt=visual_prompt,
            character_description=character_description,
            setting_description=setting_description,
            images_list="\n".join([f"图片{i}: {p}" for i, p in enumerate(image_paths)])
        )

        try:
            vlm = VLM()
            result = vlm.query(select_prompt, image_paths=image_paths, model=vlm_model)
            logger.info(f"[{segment_id}] VLM选择结果: {result}")

            # 解析 JSON 结果
            import re
            json_match = re.search(r'\{[^{}]*\}', result, re.DOTALL)
            if json_match:
                selected = json.loads(json_match.group())
                selected_idx = selected.get('selected_index', 0)
                if 0 <= selected_idx < len(image_paths):
                    best_path = image_paths[selected_idx]
                    logger.info(f"[{segment_id}] VLM选择第{selected_idx + 1}张作为最佳图片")
                    hard_failures = selected.get('hard_failures') or []
                    score = selected.get('score', 5)
                    # 构建评估结果
                    best_eval = {
                        "score": score,
                        "hard_failures": hard_failures,
                        "soft_issues": selected.get('soft_issues', []),
                        "issues": selected.get('issues', []),
                        "is_acceptable": not hard_failures and score >= 7,
                        "selected_by_vlm": True,
                        "reason": selected.get('reason', '')
                    }
                    return best_path, best_eval

        except Exception as e:
            logger.error(f"[{segment_id}] VLM选择最佳图片失败: {e}")

        # 如果 VLM 选择失败，仍保留第一张候选图，避免“已有参考图”被误标成失败。
        return image_paths[0], {
            "score": 0,
            "hard_failures": ["VLM选择最佳图片失败，无法确认硬性标准"],
            "issues": ["VLM选择最佳图片失败"],
            "is_acceptable": False,
            "selected_by_vlm": False,
        }

    def _evaluate_with_vlm(self, image_path: str, segment: dict, plot: str, visual_prompt: str,
                          character_description: str = "", setting_description: str = "",
                          vlm_model: str = "qwen3.7-plus") -> dict:
        """使用 VLM 评估首帧参考图"""
        try:
            from models.vlm_client import VLM
            vlm = VLM()

            eval_prompt = load_prompt('reference', 'eval_first_frame', 'zh').format(
                plot=plot,
                visual_prompt=visual_prompt,
                character_description=character_description,
                setting_description=setting_description
            )

            result = vlm.query(
                prompt=eval_prompt,
                image_paths=[image_path],
                model=vlm_model
            )

            if result and isinstance(result, list):
                result_text = result[0] if result else ""
            elif isinstance(result, str):
                result_text = result
            else:
                result_text = str(result)

            import json
            try:
                import re
                json_match = re.search(r'\{[^{}]*\}', result_text, re.DOTALL)
                if json_match:
                    eval_result = json.loads(json_match.group())
                    return eval_result
            except:
                pass

            return {
                "score": 0,
                "hard_failures": ["VLM评估解析失败，无法确认硬性标准"],
                "issues": ["评估解析失败"],
                "is_acceptable": False,
            }

        except Exception as e:
            logger.warning(f"VLM evaluation failed: {e}")
            return {
                "score": 0,
                "hard_failures": ["VLM评估失败，无法确认硬性标准"],
                "issues": [str(e)],
                "is_acceptable": False,
            }

    # ─── 构建最终 payload ───

    def _build_payload(
        self,
        sid: str,
        segments: list,
        session_data: dict = None,
        prompts_map: dict = None,
        selected_images: dict = None,
        rewrite_results: dict = None,
    ) -> dict:
        """构建最终 payload"""
        scenes = []
        if prompts_map is None:
            prompts_map = {}
        if selected_images is None:
            selected_images = {}
        rewrite_results = rewrite_results or {}

        # 建立 scene_id 到 selected 路径的映射
        selected_map = {}
        existing_prompts = {}
        existing_rewrite_results = {}
        if session_data and "artifacts" in session_data:
            ref_gen = session_data["artifacts"].get("reference_generation", {})
            for scene in ref_gen.get("scenes", []):
                sid_in_json = scene.get("id")
                if sid_in_json:
                    selected_map[sid_in_json] = scene.get("selected", "")
                    existing_prompts[sid_in_json] = scene.get("visual_prompt", "")
                    if scene.get("rewrite_result"):
                        existing_rewrite_results[sid_in_json] = scene.get("rewrite_result")

        for idx, seg in enumerate(segments, 1):
            segment_id = seg.get('segment_id', f'seg_unk_{idx}')
            versions = self._list_versions(sid, segment_id)
            
            # 优先从本轮生成的 selected_images 中读取，如果找不到，再从 session 的 artifacts 中读取 selected 字段
            selected_path = selected_images.get(segment_id)
            if not selected_path:
                selected_path = selected_map.get(segment_id)
            if not selected_path:
                selected_path = versions[-1] if versions else ""

            # 获取该段下第一个镜头的 content 作为片段描述
            shots_summary = seg.get('shots', [])[0].get('content', '') if seg.get('shots') else ""
            
            # 取提示词
            visual_prompt = prompts_map.get(segment_id) or existing_prompts.get(segment_id) or ""

            # 最终 payload 表示阶段已跑完；仍没有图片的片段应标记为 failed，避免覆盖实时失败状态。
            status = "done" if selected_path or versions else "failed"
            item = {
                "id": segment_id,
                "name": f"第{seg.get('episode_number', 1)}集-片段{seg.get('segment_number', idx)}",
                "index": idx,
                "description": shots_summary,
                "visual_prompt": visual_prompt,
                "selected": selected_path,
                "versions": versions,
                "status": status,
            }
            rewrite_result = rewrite_results.get(segment_id) or existing_rewrite_results.get(segment_id)
            if rewrite_result:
                item["rewrite_result"] = rewrite_result
            scenes.append(item)
        return {
            "payload": {
                "session_id": sid,
                "scenes": scenes,
            },
            "stage_completed": True,
        }


    def execute_node(self, call: dict) -> dict:
        return {
            "provider_called": False,
            "modality": "image",
            "kind": "reference",
            "model_id": call.get("model_id") or "",
            "asset_id": call.get("asset_id"),
        }
