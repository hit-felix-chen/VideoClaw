# -*- coding: utf-8 -*-
"""
阶段1: 编剧智能体 (直出一遍过版本)
"""

import os
import re
import json
import asyncio
import logging
from functools import partial
from datetime import datetime, timezone
from typing import Any, Optional, Dict, List

from prompts.loader import load_prompt_with_fallback
from .base_agent import AgentInterface

logger = logging.getLogger(__name__)

def _get_script_prompt(name: str, lang: str = "zh") -> str:
    return load_prompt_with_fallback("script", name, lang, "zh")

class ScriptWriterAgent(AgentInterface):
    MIN_EPISODE_LINES = 25
    MAX_EPISODE_LINES = 30

    def __init__(self):
        super().__init__(name="ScriptWriter")

    @staticmethod
    def _extract_json_from_text(text: str) -> Optional[Any]:
        text = text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试匹配第一个 { 或 [ 到底部对应的 } 或 ]
        start_obj = text.find('{')
        start_arr = text.find('[')
        
        # 确定起始位置
        if start_obj == -1 and start_arr == -1:
            return None
        
        start = start_obj if (start_obj != -1 and (start_arr == -1 or start_obj < start_arr)) else start_arr
        end_char = '}' if start == start_obj else ']'
        end = text.rfind(end_char)

        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return None

    def _gen_id(self, prefix: str = "char") -> str:
        import uuid
        return f"{prefix}_{uuid.uuid4().hex[:6]}"

    def _save_result(self, json_data: dict, sid: str, is_zh: bool):
        from config import settings as app_settings
        os.makedirs(os.path.join(app_settings.RESULT_DIR, 'script'), exist_ok=True)
        out_path = os.path.join(app_settings.RESULT_DIR, 'script', f'{sid}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        logger.info(f"[ScriptWriter] script saved to {out_path}")

    def _save_progress(self, sid: str, phase: str, data: dict):
        pass

    @classmethod
    def _split_episode_blocks(cls, script_text: str) -> List[dict]:
        episode_re = re.compile(
            r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:第\s*(\d+)\s*集(?![-－])|Episode\s+(\d+)\b)",
            re.IGNORECASE,
        )
        blocks: List[dict] = []
        current = {"episode_number": 1, "lines": []}
        seen_header = False

        for line in script_text.splitlines():
            match = episode_re.match(line.strip())
            if match:
                if seen_header and current["lines"]:
                    blocks.append(current)
                ep_no = int(match.group(1) or match.group(2) or len(blocks) + 1)
                current = {"episode_number": ep_no, "lines": [line]}
                seen_header = True
            else:
                current["lines"].append(line)

        if current["lines"]:
            blocks.append(current)
        return blocks

    @staticmethod
    def _is_counted_script_line(line: str) -> bool:
        text = line.strip()
        if not text:
            return False
        if re.match(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:第\s*\d+\s*集(?![-－])|Episode\s+\d+\b)", text, re.IGNORECASE):
            return False
        if re.match(r"^\s*(?:\*\*)?\s*(?:第\s*\d+\s*集[-－]第\s*\d+\s*场|\d+\s*[-－]\s*\d+\b)", text, re.IGNORECASE):
            return False
        if text.startswith("**") and text.endswith("**"):
            return False
        if re.match(r"^(人物|角色|Characters)\s*[:：]", text, re.IGNORECASE):
            return False
        return True

    @classmethod
    def _script_length_stats(cls, script_text: str) -> List[dict]:
        stats: List[dict] = []
        for block in cls._split_episode_blocks(script_text):
            counted = [line for line in block["lines"] if cls._is_counted_script_line(line)]
            stats.append({
                "episode_number": block["episode_number"],
                "line_count": len(counted),
                "too_long": len(counted) > cls.MAX_EPISODE_LINES,
            })
        return stats

    @classmethod
    def _length_feedback(cls, stats: List[dict]) -> str:
        return "\n".join(
            f"- 第{item['episode_number']}集：{item['line_count']}行"
            for item in stats
        )

    @staticmethod
    def _expected_episode_numbers(episodes: int) -> List[int]:
        return list(range(1, max(1, int(episodes)) + 1))

    @classmethod
    def _episode_numbers_from_script(cls, script_text: str) -> List[int]:
        return [
            int(block["episode_number"])
            for block in cls._split_episode_blocks(script_text)
            if block.get("lines")
        ]

    @classmethod
    def _episode_count_feedback(cls, script_text: str, episodes: int) -> str:
        expected = cls._expected_episode_numbers(episodes)
        found = cls._episode_numbers_from_script(script_text)
        missing = [num for num in expected if num not in found]
        extra = [num for num in found if num not in expected]
        return (
            f"目标集数：{episodes}；应包含集号：{expected}；"
            f"当前识别到集号：{found or '无'}；缺失：{missing or '无'}；多余：{extra or '无'}。"
        )

    @classmethod
    def _episode_count_matches(cls, script_text: str, episodes: int) -> bool:
        found = sorted(set(cls._episode_numbers_from_script(script_text)))
        return found == cls._expected_episode_numbers(episodes)

    @classmethod
    def _build_episodes_from_script_text(cls, script_text: str, expected_episodes: int) -> List[dict]:
        episodes: List[dict] = []
        for block in cls._split_episode_blocks(script_text):
            ep_no = int(block.get("episode_number") or len(episodes) + 1)
            if ep_no < 1 or ep_no > expected_episodes:
                continue
            content = "\n".join(block.get("lines") or []).strip()
            if not content:
                continue
            episodes.append({
                "episode_number": ep_no,
                "act_title": f"第{ep_no}集",
                "content": content,
            })
        deduped: Dict[int, dict] = {}
        for ep in episodes:
            deduped[ep["episode_number"]] = ep
        return [deduped[num] for num in cls._expected_episode_numbers(expected_episodes) if num in deduped]


    def execute_node(self, call: dict) -> dict:
        return {
            "provider_called": False,
            "kind": "script",
            "episode_number": 1,
            "title": "第1集",
            "text": call.get("text") or "",
        }
