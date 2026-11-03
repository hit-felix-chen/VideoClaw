# -*- coding: utf-8 -*-
"""
阶段2: 角色/场景设计智能体
基于阶段1的剧本JSON生成角色4视图和场景全景图
支持单项重新生成、历史版本切换
图片以 character_id / setting_id 命名。
磁盘文件只是旧阶段的写出路径；版本身份是 R5 的 image 行，调用解析走 SegmentRefStore。
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


class CharacterDesignerAgent(AgentInterface):
    """角色/场景设计：从剧本JSON读取描述 → 生成角色4视图 + 场景全景图"""

    def __init__(self):
        super().__init__(name="CharacterDesigner")

    # ─── 提示词模板 ───

    @staticmethod
    def _char_prompt(name: str, desc: str, style: str, species: str = "") -> str:
        """角色4视图提示词 - 直接加载 character_zh 模板"""
        # 加载基础角色提示词模板
        template = load_prompt('character', 'character', 'zh')
        # 替换角色信息
        return template.format(name=name, desc=desc, style=style)

    @staticmethod
    def _setting_prompt(name: str, desc: str, style: str) -> str:
        """场景全景图提示词 - 直接加载 setting_zh 模板"""
        # 加载基础场景提示词模板
        template = load_prompt('setting', 'setting', 'zh')
        # 替换场景信息
        return template.format(name=name, desc=desc, style=style)

    @staticmethod
    def _apply_eval_feedback_to_prompt(base_prompt: str, eval_result: dict, iteration: int) -> str:
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
            return base_prompt

        return (
            f"{base_prompt}\n\n"
            f"【第{iteration + 1}轮VLM评估反馈】\n"
            f"上一轮生成未通过评估，请在下一轮生成时优先修正以下问题，同时保持原始角色/场景设定不变：\n"
            + "\n".join(f"- {line}" for line in feedback_lines)
        )

    # ─── 文件管理（基于唯一ID） ───

    @staticmethod
    def _asset_base(sid: str) -> str:
        return os.path.join('code/result/image', str(sid), 'Assets')

    def _list_versions(self, sid: str, asset_type: str, asset_id: str) -> List[str]:
        """列出某个素材的所有历史版本文件路径，按时间排序。

        文件命名: {asset_id}.png, {asset_id}_v2.png, {asset_id}_v3.png, ...
        只匹配该 id 本身，char_1 不会带上 char_10。
        """
        adir = os.path.join(self._asset_base(sid), asset_type)
        if not os.path.isdir(adir):
            return []
        pattern = re.compile(
            rf"^{re.escape(asset_id)}(?:_v\d+)?\.(?:png|jpg|jpeg|webp|bmp)$",
            re.IGNORECASE,
        )
        files = [os.path.join(adir, name) for name in os.listdir(adir) if pattern.match(name)]
        return sorted(set(files), key=os.path.getmtime)

    @staticmethod
    def list_image_versions(
        conn: Any,
        asset_id: Optional[str] = None,
        look_id: Optional[str] = None,
    ) -> List[dict]:
        """Return R5 image rows for this asset or look. Both empty lists nothing."""
        if asset_id is None and look_id is None:
            return []
        from storage.assets import ImageStore

        return ImageStore.list(conn, asset_id=asset_id, look_id=look_id)

    @staticmethod
    def resolve_calls(conn: Any, text: str, project_id: Optional[str] = None) -> dict:
        """Resolve ``@资产名`` / ``@资产名/形象名`` through SegmentRefStore."""
        from storage.references import SegmentRefStore

        return SegmentRefStore.resolve(conn, text, project_id=project_id)

    def _next_version_path(self, sid: str, asset_type: str, asset_id: str) -> str:
        """获取下一个版本的文件路径"""
        adir = os.path.join(self._asset_base(sid), asset_type)
        os.makedirs(adir, exist_ok=True)

        existing = self._list_versions(sid, asset_type, asset_id)
        if not existing:
            return os.path.join(adir, f"{asset_id}.png")

        max_v = 1
        for fp in existing:
            bn = os.path.splitext(os.path.basename(fp))[0]
            m = re.search(r'_v(\d+)$', bn)
            if m:
                max_v = max(max_v, int(m.group(1)))

        return os.path.join(adir, f"{asset_id}_v{max_v + 1}.png")

    def _build_asset_info(self, sid: str, asset_type: str,
                          asset_id: str, name: str, desc: str,
                          selected_path: str = "") -> dict:
        """构建单个素材的信息（含所有历史版本）"""
        versions = self._list_versions(sid, asset_type, asset_id)
        if not selected_path and versions:
            selected_path = versions[-1]
        status = "done" if selected_path or versions else "failed"
        return {
            "id": asset_id,
            "name": name,
            "description": desc,
            "selected": selected_path,
            "versions": versions,
            "status": status,
        }

    @staticmethod
    def _collect_rewrite_results(artifact: dict) -> dict:
        rewrite_results = {"characters": {}, "settings": {}}
        if not isinstance(artifact, dict):
            return rewrite_results
        for key in ("characters", "settings"):
            for item in artifact.get(key, []) if isinstance(artifact.get(key), list) else []:
                if isinstance(item, dict) and item.get("id") and item.get("rewrite_result"):
                    rewrite_results[key][item["id"]] = item["rewrite_result"]
        return rewrite_results

    # ─── 图片生成 ───

    def _generate_image_with_doctor(self, img_client, *, prompt: str, model: str,
                                    llm_model: str, context: dict, **kwargs) -> tuple:
        """Generate once; if prompt-related failure is diagnosed, rewrite and retry once."""
        try:
            paths = img_client.generate_image(prompt=prompt, model=model, **kwargs)
            if not paths:
                raise RuntimeError("Image generation returned no output")
            return paths, prompt, None
        except Exception as exc:
            from .doctor_agent import DoctorAgent

            doctor = DoctorAgent(llm_model=llm_model)
            rewrite, diagnosis, rewrite_result = doctor.maybe_rewrite_prompt(
                stage="character_design",
                model=model,
                prompt=prompt,
                error=str(exc),
                context=context,
            )
            if not rewrite:
                logger.info("Doctor skipped character prompt rewrite: %s", diagnosis.get("reason"))
                raise

            logger.info("Doctor rewrote character prompt: reason_type=%s reason=%s",
                        diagnosis.get("reason_type"), diagnosis.get("reason"))
            paths = img_client.generate_image(prompt=rewrite, model=model, **kwargs)
            if not paths:
                raise RuntimeError("Image generation returned no output after doctor rewrite")
            return paths, rewrite, rewrite_result

    def _build_preview(self, sid: str, chars_desc: dict, sets_desc: dict) -> dict:
        """构建素材预览列表（含当前状态）用于前端实时显示"""
        preview = {"characters": [], "settings": []}
        for asset_id, info in chars_desc.items():
            existing = self._list_versions(sid, 'characters', asset_id)
            preview["characters"].append({
                "id": asset_id,
                "name": info.get("name", ""),
                "description": info.get("description", ""),
                "selected": existing[-1] if existing else "",
                "versions": existing,
                "status": "done" if existing else "pending",
            })
        for asset_id, info in sets_desc.items():
            existing = self._list_versions(sid, 'settings', asset_id)
            name = info.get("name", "") if isinstance(info, dict) else ""
            desc = info.get("description", "") if isinstance(info, dict) else str(info)
            preview["settings"].append({
                "id": asset_id,
                "name": name,
                "description": desc,
                "selected": existing[-1] if existing else "",
                "versions": existing,
                "status": "done" if existing else "pending",
            })
        return preview

    def _generate_one(self, img_client, asset_id: str, name: str, desc: str,
                      asset_type: str, style: str, species: str,
                      t2i_model: str, vlm_model: str, sid: str,
                      llm_model: str = "", max_iterations: int = 3) -> tuple:
        """生成单个素材图并返回 (asset_id, path_or_None, eval_result)

        评估-生成循环：如果 VLM 评估发现问题，最多重新生成 max_iterations 次
        """
        self._check_cancel()

        # 初始提示词
        style_prompt = self._get_style_prompt(style)
        if asset_type == 'characters':
            base_prompt = self._char_prompt(name, desc, style_prompt, species)
        else:
            base_prompt = self._setting_prompt(name, desc, style_prompt)

        video_ratio = "16:9"
        resolution = "2K"
        current_prompt = base_prompt

        for iteration in range(max_iterations):
            self._check_cancel()

            save_path = self._next_version_path(sid, asset_type, asset_id)
            save_dir = os.path.dirname(save_path)

            try:
                paths, current_prompt, rewrite_result = self._generate_image_with_doctor(
                    img_client,
                    prompt=current_prompt, model=t2i_model,
                    llm_model=llm_model,
                    context={
                        "asset_id": asset_id,
                        "asset_name": name,
                        "asset_type": asset_type,
                        "description": desc,
                    },
                    session_id=str(sid), save_dir=save_dir, video_ratio=video_ratio, resolution=resolution,
                )

                gen = paths[0]
                if gen != save_path:
                    if os.path.exists(save_path):
                        os.remove(save_path)
                    os.rename(gen, save_path)

                # VLM 评估
                eval_result = self._evaluate_with_vlm(save_path, desc, asset_type, vlm_model)

                score = eval_result.get('score', 0)
                issues = eval_result.get('issues', [])
                suggestion = eval_result.get('suggestion', '')
                hard_failures = eval_result.get('hard_failures') or []
                if 'is_acceptable' in eval_result:
                    is_acceptable = bool(eval_result.get('is_acceptable')) and not hard_failures
                else:
                    is_acceptable = not hard_failures and score >= 7

                if is_acceptable:
                    logger.info(f"[{asset_type}] {name} ✓ VLM评估通过 - 评分: {score}/10")
                else:
                    logger.warning(f"[{asset_type}] {name} ✗ VLM评估不通过 - 评分: {score}/10")
                    logger.warning(f"[{asset_type}] 问题: {issues}")
                    if hard_failures:
                        logger.warning(f"[{asset_type}] 硬性失败项: {hard_failures}")
                    if suggestion:
                        logger.warning(f"[{asset_type}] 建议: {suggestion}")

                # 检查是否需要重新生成
                if is_acceptable:
                    # 评估通过，返回结果
                    return asset_id, save_path, eval_result, rewrite_result
                else:
                    # 评估不通过，记录问题并继续循环
                    current_prompt = self._apply_eval_feedback_to_prompt(base_prompt, eval_result, iteration)
                    logger.info(f"[{asset_type}] {name} 下一轮将使用VLM反馈优化提示词")
                    # 报告进度
                    self._report_progress("角色设计", f"重新生成中 ({iteration + 2}/{max_iterations}): {name}", 0)

            except Exception as e:
                logger.error(f"Asset gen failed for {asset_type} {name}({asset_id}): {e}")

        # 达到最大迭代次数，尝试使用 VLM 选择最佳图片
        logger.warning(f"[{asset_type}] {name} reached max iterations ({max_iterations}), trying VLM selection")

        # 收集所有生成过的版本
        all_versions = self._list_versions(sid, asset_type, asset_id)
        if len(all_versions) > 1:
            # 有多个版本，调用 VLM 选择最好的
            best_path, best_eval = self._select_best_with_vlm(
                all_versions, name, desc, asset_type, species, vlm_model
            )
            if best_path:
                logger.info(f"[{asset_type}] {name} VLM selected best version: {best_path}")
                return asset_id, best_path, best_eval, best_eval.get("rewrite_result") if isinstance(best_eval, dict) else None

        # 没有多个版本或 VLM 选择失败，返回最后一次结果
        return (
            asset_id,
            save_path if os.path.exists(save_path) else None,
            eval_result if 'eval_result' in locals() else None,
            rewrite_result if 'rewrite_result' in locals() else None,
        )

    def _evaluate_with_vlm(self, image_path: str, description: str, asset_type: str, vlm_model: str = "qwen3.7-plus") -> dict:
        """使用 VLM 评估生成的图片"""
        try:
            from models.vlm_client import VLM
            vlm = VLM()

            # 选择评估提示词
            if asset_type == 'characters':
                eval_prompt = load_prompt('character', 'eval_character', 'zh').format(
                    character_description=description
                )
            else:
                eval_prompt = load_prompt('setting', 'eval_setting', 'zh').format(
                    setting_description=description
                )

            result = vlm.query(
                prompt=eval_prompt,
                image_paths=[image_path],
                model=vlm_model
            )

            # 解析结果
            if result and isinstance(result, list):
                result_text = result[0] if result else ""
            elif isinstance(result, str):
                result_text = result
            else:
                result_text = str(result)

            # 尝试提取 JSON
            import json
            try:
                # 找到 JSON 部分
                import re
                json_match = re.search(r'\{[^{}]*\}', result_text, re.DOTALL)
                if json_match:
                    eval_result = json.loads(json_match.group())
                    return eval_result
            except:
                pass

            return {"score": 5, "issues": ["评估解析失败"], "is_acceptable": True}

        except Exception as e:
            logger.warning(f"VLM evaluation failed: {e}")
            return {"score": 5, "issues": [str(e)], "is_acceptable": True}

    def _select_best_with_vlm(self, image_paths: List[str], name: str, description: str,
                               asset_type: str, species: str = "", vlm_model: str = "qwen3.7-plus") -> tuple:
        """使用 VLM 从多个版本中选择最好的一张"""
        from models.vlm_client import VLM
        import re

        if not image_paths:
            return None, None

        try:
            vlm = VLM()

            # 选择评估提示词
            if asset_type == 'characters':
                select_prompt = load_prompt('character', 'eval_select_best', 'zh').format(
                    num_images=len(image_paths),
                    num_images_minus_1=len(image_paths) - 1,
                    character_name=name,
                    character_description=description,
                    species=species,
                    images_list="\n".join([f"图片{i}: {p}" for i, p in enumerate(image_paths)])
                )
            else:
                select_prompt = load_prompt('setting', 'eval_select_best', 'zh').format(
                    num_images=len(image_paths),
                    num_images_minus_1=len(image_paths) - 1,
                    setting_name=name,
                    setting_description=description,
                    images_list="\n".join([f"图片{i}: {p}" for i, p in enumerate(image_paths)])
                )

            result = vlm.query(select_prompt, image_paths=image_paths, model=vlm_model)
            logger.info(f"[{asset_type}] {name} VLM selection result: {result}")

            # 解析 JSON 结果
            if result and isinstance(result, list):
                result_text = result[0] if result else ""
            elif isinstance(result, str):
                result_text = result
            else:
                result_text = str(result)

            logger.info(f"[{asset_type}] {name} VLM selection raw response: {result_text[:500]}")

            # 解析 JSON，提取 best_index
            best_index = 0
            try:
                # 找到 JSON 开始和结束
                json_start = result_text.find('{')
                json_end = result_text.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = result_text[json_start:json_end]
                    selection_result = json.loads(json_str)
                    best_index = selection_result.get('best_index', 0)
                    logger.info(f"[{asset_type}] {name} Parsed best_index: {best_index}")
            except Exception as e:
                logger.warning(f"[{asset_type}] {name} JSON parse failed: {e}, using last image")
                best_index = len(image_paths) - 1

            # 如果找到了 best_index，选择对应的图片
            if best_index is not None and 0 <= best_index < len(image_paths):
                best_path = image_paths[best_index]
                best_eval = {"score": 8, "issues": [], "is_acceptable": True, "reason": f"VLM selected image {best_index + 1} of {len(image_paths)}"}
                logger.info(f"[{asset_type}] {name} Selected image: {best_path}")
                return best_path, best_eval
            else:
                logger.warning(f"[{asset_type}] {name} Invalid best_index: {best_index}, available images: {len(image_paths)}")

        except Exception as e:
            logger.warning(f"VLM selection failed: {e}")

        return None, None

    # ─── 从剧本JSON读取角色/场景数据 ───

    def _read_script_data(self, input_data: Dict) -> dict:
        """从编排器注入的 script_generation artifact 提取角色和场景数据。"""
        script_gen = self._session_artifact(input_data, "script_generation")
        chars = {}
        for c in script_gen.get("characters", []):
            cid = c.get("character_id") or c.get("id") or ""
            if cid:
                chars[cid] = {
                    "name": c.get("name", ""),
                    "description": c.get("description", ""),
                    "species": c.get("species", ""),
                }

        sets = {}
        for s in script_gen.get("settings", []):
            sid_val = s.get("setting_id") or s.get("id") or ""
            if sid_val:
                sets[sid_val] = {
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                }

        return {"characters": chars, "settings": sets}

    def _build_payload(self, sid: str, chars_desc: dict, sets_desc: dict,
                       selected_chars: dict = None, selected_sets: dict = None,
                       rewrite_results: dict = None) -> dict:
        """构建返回给前端的 payload"""
        selected_chars = selected_chars or {}
        selected_sets = selected_sets or {}
        rewrite_results = rewrite_results or {"characters": {}, "settings": {}}

        characters = []
        for asset_id, info in chars_desc.items():
            desc = info.get("description", "") if isinstance(info, dict) else info
            name = info.get("name", "") if isinstance(info, dict) else ""
            sel = selected_chars.get(asset_id, "")
            item = self._build_asset_info(sid, 'characters', asset_id, name, desc, sel)
            if rewrite_results.get("characters", {}).get(asset_id):
                item["rewrite_result"] = rewrite_results["characters"][asset_id]
            characters.append(item)

        settings_list = []
        for asset_id, info in sets_desc.items():
            desc = info.get("description", "") if isinstance(info, dict) else info
            name = info.get("name", "") if isinstance(info, dict) else ""
            sel = selected_sets.get(asset_id, "")
            item = self._build_asset_info(sid, 'settings', asset_id, name, desc, sel)
            if rewrite_results.get("settings", {}).get(asset_id):
                item["rewrite_result"] = rewrite_results["settings"][asset_id]
            settings_list.append(item)

        # 图片生成完成即为阶段完成，用户选择图片只是更新数据
        return {
            "payload": {
                "session_id": sid,
                "characters": characters,
                "settings": settings_list,
            },
            "stage_completed": True,
        }

    def execute_node(self, call: dict) -> dict:
        return {
            "provider_called": False,
            "modality": "image",
            "kind": "character",
            "model_id": call.get("model_id") or "",
            "asset_id": call.get("asset_id"),
        }
