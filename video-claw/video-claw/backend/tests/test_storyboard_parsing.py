"""Pure storyboard parsing. These tests never call a model."""

import json

import pytest

from core.agents.storyboard_agent import StoryboardAgent


class ScriptUnits:
    @staticmethod
    @staticmethod
    def make(unit_id, duration=2, scene_key=1, is_entry=False, text="动作"):
        return {
            "unit_id": unit_id,
            "duration": duration,
            "scene_key": scene_key,
            "is_entry": is_entry,
            "text": text,
            "characters": ["张三"],
            "scene_characters": ["张三"],
            "location": "教室",
            "scene_time": "日",
            "scene_space": "内",
            "scene_context": "第1集-第1场 日 内 教室",
        }


class TestDialogueParts:
    def test_splits_speaker_tone_and_line(self):
        assert StoryboardAgent._dialogue_parts("张三（低声）：你来了。") == ("张三", "低声", "你来了。")

    def test_halfwidth_parentheses_and_colon(self):
        assert StoryboardAgent._dialogue_parts("John (whisper): Hello there") == ("John", "whisper", "Hello there")

    def test_space_before_parenthetical_is_stripped_from_speaker(self):
        assert StoryboardAgent._dialogue_parts("张三 （低声）：你好") == ("张三", "低声", "你好")

    def test_plain_line_without_colon_is_not_dialogue(self):
        assert StoryboardAgent._dialogue_parts("张三走进教室") is None

    def test_section_labels_are_not_dialogue(self):
        for label in ("人物", "场景", "画面", "镜头", "地点", "时间"):
            assert StoryboardAgent._dialogue_parts(f"{label}：张三") is None

    def test_empty_line_after_colon_is_not_dialogue(self):
        assert StoryboardAgent._dialogue_parts("张三：") is None
        assert StoryboardAgent._dialogue_parts("张三：   ") is None


class TestDurationAndMarkup:
    def test_action_duration_is_two_seconds(self):
        assert StoryboardAgent._duration_for_script_unit("张三走进教室", False) == 2

    def test_short_dialogue_is_clamped_to_two_seconds(self):
        assert StoryboardAgent._duration_for_script_unit("张三：你好", True) == 2

    def test_long_dialogue_grows_then_clamps_at_fifteen(self):
        line = "张三：" + ("啊" * 80)
        assert StoryboardAgent._duration_for_script_unit(line, True) == 15

    def test_clean_script_line_strips_bullets_and_action_tags(self):
        assert StoryboardAgent._clean_script_line("  - <action>  张三  走进 </action> ") == "张三 走进"

    def test_strip_markup_removes_markdown_emphasis(self):
        assert StoryboardAgent._strip_markup("**第1集-第1场 日 内 教室**") == "第1集-第1场 日 内 教室"

    def test_metadata_and_end_markers(self):
        assert StoryboardAgent._is_metadata_line("剧本名称：测试")
        assert StoryboardAgent._is_metadata_line("时长：3分钟")
        assert not StoryboardAgent._is_metadata_line("张三：你好")
        assert StoryboardAgent._is_end_marker("（第1集完）")
        assert StoryboardAgent._is_end_marker("(THE END)")
        assert not StoryboardAgent._is_end_marker("张三：完了")


class TestJsonExtraction:
    def test_fenced_json_array(self):
        text = '```json\n[{"shot": 1}]\n```'
        assert StoryboardAgent._extract_json_array(text) == [{"shot": 1}]

    def test_array_embedded_in_prose(self):
        text = '结果如下：\n[{"a": 1}, {"a": 2}]\n以上。'
        assert StoryboardAgent._extract_json_array(text) == [{"a": 1}, {"a": 2}]

    def test_object_is_not_an_array(self):
        assert StoryboardAgent._extract_json_array('{"a": 1}') is None

    def test_fenced_json_object(self):
        text = '```json\n{"location": "教室"}\n```'
        assert StoryboardAgent._extract_json_object(text) == {"location": "教室"}

    def test_object_embedded_in_prose(self):
        text = '说明 {"ok": true} 结束'
        assert StoryboardAgent._extract_json_object(text) == {"ok": True}

    def test_invalid_json_returns_none(self):
        assert StoryboardAgent._extract_json_array("not json") is None
        assert StoryboardAgent._extract_json_object("not json") is None


class TestSceneHeadersAndNames:
    def test_character_and_setting_names_drop_blanks_and_sort_longest_first(self):
        names = StoryboardAgent._character_names([
            {"name": "李"},
            {"name": "  "},
            {"name": "李明"},
            {"name": "李明"},
        ])
        assert names == ["李明", "李"]
        assert StoryboardAgent._setting_names([{"name": "教室"}, {"name": ""}]) == ["教室"]

    def test_scene_header_binds_known_setting(self):
        header = StoryboardAgent._parse_scene_header_parts(
            "**第1集-第1场 日 内 高二三班教室**",
            ["高二三班教室", "办公室"],
        )
        assert header["location"] == "高二三班教室"
        assert header["scene_time"] == "日"
        assert header["scene_space"] == "内"
        assert StoryboardAgent._parse_scene_header("1-1 夜 外 操场", ["操场"]) == "操场"

    def test_scene_header_keeps_unknown_location_text(self):
        header = StoryboardAgent._parse_scene_header_parts(
            "第2集-第3场 晨 外 无人小巷",
            ["教室"],
        )
        assert header["location"] == "无人小巷"
        assert header["scene_time"] == "晨"

    def test_non_header_returns_none(self):
        assert StoryboardAgent._parse_scene_header_parts("张三：你好", ["教室"]) is None

    def test_setting_match_ignores_parenthetical_suffix(self):
        assert StoryboardAgent._setting_matches_text("高二三班教室（日）", "众人站在高二三班教室里")
        assert not StoryboardAgent._setting_matches_text("办公室", "教室里只有张三")

    def test_resolve_location_prefers_named_setting_then_current(self):
        assert StoryboardAgent._resolve_location("回到办公室", "教室", ["教室", "办公室"]) == "办公室"
        assert StoryboardAgent._resolve_location("没有地名", "教室", ["教室", "办公室"]) == "教室"
        assert StoryboardAgent._resolve_location("没有地名", "", ["教室"]) == "教室"
        assert StoryboardAgent._resolve_location("没有地名", "", []) == ""


class TestEntryAndAnnotation:
    def test_entry_action_requires_a_known_character_and_a_verb(self):
        assert StoryboardAgent._is_entry_action("张三走进教室", ["张三"])
        assert not StoryboardAgent._is_entry_action("张三：我进来了", ["张三"])
        assert not StoryboardAgent._is_entry_action("李四走进教室", ["张三"])
        assert not StoryboardAgent._is_entry_action("张三坐下", ["张三"])

    def test_annotate_episode_script_builds_units_without_a_model(self):
        script = "\n".join([
            "剧本名称：测试",
            "**第1集-第1场 日 内 高二三班教室**",
            "人物：张三、李四",
            "<action>张三走进教室</action>",
            "张三（低声）：你来了。",
            "李四：我一直在。",
            "（第1集完）",
        ])
        annotated, units = StoryboardAgent._annotate_episode_script(
            script,
            [{"name": "张三"}, {"name": "李四"}, {"name": "王五"}],
            [{"name": "高二三班教室"}],
        )

        assert [unit["unit_id"] for unit in units] == ["U001", "U002", "U003"]
        assert "王五" not in annotated
        assert units[0]["is_dialogue"] is False
        assert units[0]["is_entry"] is True
        assert units[0]["characters"] == ["张三"]
        assert units[0]["location"] == "高二三班教室"
        assert units[0]["line_number"] == 4
        assert units[1]["speaker"] == "张三"
        assert units[1]["tone"] == "低声"
        assert units[1]["is_dialogue"] is True
        assert set(units[1]["scene_characters"]) == {"张三", "李四"}
        assert units[2]["speaker"] == "李四"
        assert units[2]["tone"] == ""
        assert "[2秒][U001]" in annotated
        assert "剧本名称：测试" in annotated
        assert "（第1集完）" in annotated

    def test_match_characters_uses_only_the_given_names(self):
        matched = StoryboardAgent._match_characters("李明拉着李走进教室", ["李明", "李", "王五"])
        assert matched == ["李明", "李"]


class TestSegmentPlan:
    def test_validate_accepts_an_in_order_plan(self):
        units = [ScriptUnits.make("U001", duration=4), ScriptUnits.make("U002", duration=4)]
        plans = StoryboardAgent._validate_segment_plan(1, [{"unit_ids": ["U001", "U002"]}], units)
        assert len(plans) == 1
        assert plans[0]["segment_number"] == 1
        assert plans[0]["total_duration"] == 8
        assert [item["unit_id"] for item in plans[0]["items"]] == ["U001", "U002"]

    def test_validate_rejects_reorder_unknown_ids_and_overflow(self):
        units = [ScriptUnits.make("U001"), ScriptUnits.make("U002")]
        with pytest.raises(ValueError, match="乱序"):
            StoryboardAgent._validate_segment_plan(1, [{"unit_ids": ["U002", "U001"]}], units)
        with pytest.raises(ValueError, match="未知"):
            StoryboardAgent._validate_segment_plan(1, [{"unit_ids": ["U001", "U999"]}], units)
        long_units = [ScriptUnits.make("U001", duration=8), ScriptUnits.make("U002", duration=8)]
        with pytest.raises(ValueError, match="超出上限"):
            StoryboardAgent._validate_segment_plan(1, [{"unit_ids": ["U001", "U002"]}], long_units)

    def test_validate_rejects_entry_that_is_not_first_and_cross_scene(self):
        units = [ScriptUnits.make("U001"), ScriptUnits.make("U002", is_entry=True)]
        with pytest.raises(ValueError, match="入场"):
            StoryboardAgent._validate_segment_plan(1, [{"unit_ids": ["U001", "U002"]}], units)
        units = [ScriptUnits.make("U001", scene_key=1), ScriptUnits.make("U002", scene_key=2)]
        with pytest.raises(ValueError, match="跨场景"):
            StoryboardAgent._validate_segment_plan(1, [{"unit_ids": ["U001", "U002"]}], units)

    def test_fallback_splits_on_overflow_entry_and_scene_change(self):
        units = [
            ScriptUnits.make("U001", duration=10, scene_key=1),
            ScriptUnits.make("U002", duration=10, scene_key=1),
            ScriptUnits.make("U003", duration=2, scene_key=1, is_entry=True),
            ScriptUnits.make("U004", duration=2, scene_key=2),
        ]
        plans = StoryboardAgent._fallback_segment_plan(3, units)
        grouped = [[item["unit_id"] for item in plan["items"]] for plan in plans]
        assert grouped == [["U001"], ["U002"], ["U003"], ["U004"]]
        assert [plan["episode_number"] for plan in plans] == [3, 3, 3, 3]
        assert [plan["segment_number"] for plan in plans] == [1, 2, 3, 4]

    def test_extract_plan_unit_ids_reads_items_and_raw_text(self):
        assert StoryboardAgent._extract_plan_unit_ids({"unit_ids": ["U001", "nope"]}) == ["U001"]
        assert StoryboardAgent._extract_plan_unit_ids({"items": [{"unit_id": "U010"}]}) == ["U010"]
        raw = json.dumps({"note": "see U003 then U003 again"})
        assert StoryboardAgent._extract_plan_unit_ids(json.loads(raw) | {"blob": "U003"}) == ["U003"]

    def test_segment_from_shots_pads_short_segments(self):
        shots = [{"duration": 2, "shot_type": "中景", "content": "中景，张三站着"}]
        segment = StoryboardAgent._segment_from_shots(1, 1, "教室", shots, ["张三"])
        assert segment["segment_id"] == "seg_01_01"
        assert segment["total_duration"] == StoryboardAgent.MIN_SEGMENT_DURATION
        assert segment["shots"][0]["duration"] == StoryboardAgent.MIN_SEGMENT_DURATION
        assert sum(shot["duration"] for shot in segment["shots"]) == segment["total_duration"]

    def test_segment_total_matches_shot_durations_when_shots_sum_past_the_cap(self):
        shots = [
            {"duration": 10, "shot_type": "中景", "content": "中景，甲"},
            {"duration": 10, "shot_type": "全景", "content": "全景，乙"},
        ]
        segment = StoryboardAgent._segment_from_shots(1, 2, "教室", shots, ["张三"])
        assert segment["total_duration"] == sum(shot["duration"] for shot in segment["shots"])

    def test_opening_shot_is_normalized_to_a_wide_enough_type(self):
        shots = [{"duration": 5, "shot_type": "特写", "content": "特写，眼睛"}]
        StoryboardAgent._normalize_first_shot_type(shots)
        assert shots[0]["shot_type"] == "中景"
        assert shots[0]["content"].startswith("中景")
