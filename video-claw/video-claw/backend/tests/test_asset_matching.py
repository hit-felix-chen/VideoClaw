"""Name matching for characters and settings. No image generation."""

import os

from core.agents.reference_agent import ReferenceGeneratorAgent, ratio_to_size
from core.agents.storyboard_agent import StoryboardAgent
from core.agents.video_agent import VideoDirectorAgent


CHARACTERS = [
    {"id": "char_li", "name": "李", "description": "短发同学", "selected": "/tmp/li.png"},
    {"id": "char_liming", "name": "李明", "description": "高个子", "selected": "/tmp/liming.png"},
]
SETTINGS = [
    {"id": "set_room", "name": "高二三班教室", "description": "朝南的教室", "selected": "/tmp/room.png"},
    {"id": "set_hall", "name": "礼堂", "description": "空礼堂", "selected": "/tmp/hall.png"},
]


class TestVideoNameMatching:
    def test_exact_name_beats_a_shorter_prefix(self):
        found = VideoDirectorAgent._match_asset_by_name("李明", CHARACTERS)
        assert found["id"] == "char_liming"

    def test_exact_id_in_the_name_map(self):
        mapping = VideoDirectorAgent._build_name_asset_map(CHARACTERS)
        found = VideoDirectorAgent._match_asset_by_name("char_li", CHARACTERS, mapping)
        assert found["id"] == "char_li"

    def test_blank_and_unknown_names_do_not_invent_an_asset(self):
        assert VideoDirectorAgent._match_asset_by_name("  ", CHARACTERS) is None
        assert VideoDirectorAgent._match_asset_by_name("王五", CHARACTERS) is None

    def test_fuzzy_contains_uses_the_first_partial_name(self):
        found = VideoDirectorAgent._match_asset_by_name("李明的同学", CHARACTERS)
        assert found["id"] == "char_li"

    def test_prompt_includes_only_the_exact_character_description(self):
        agent = VideoDirectorAgent()
        segment = {
            "characters": ["李明"],
            "shots": [{"duration": 3, "content": "挥手"}],
        }
        prompt = agent._assemble_prompt(segment, "写实", {"characters": CHARACTERS})
        assert "高个子" in prompt
        assert "短发同学" not in prompt
        assert prompt.startswith("风格控制：写实")
        assert "分镜1：[3秒] 挥手" in prompt
        assert prompt.rstrip().endswith("不要生成字幕或水印")

    def test_reference_asset_paths_follow_names_that_exist_on_disk(self, tmp_path):
        character_file = tmp_path / "liming.png"
        setting_file = tmp_path / "room.png"
        character_file.write_bytes(b"png")
        setting_file.write_bytes(b"png")
        characters = [
            {"id": "char_liming", "name": "李明", "selected": str(character_file), "versions": []},
        ]
        settings = [
            {"id": "set_room", "name": "高二三班教室", "selected": "", "versions": [str(setting_file)]},
        ]
        agent = VideoDirectorAgent()
        paths = agent._get_segment_reference_assets(
            {"characters": ["李明"], "location": "高二三班教室"},
            {"characters": characters, "settings": settings},
        )
        assert paths == [str(setting_file), str(character_file)]

    def test_selected_path_falls_back_to_an_existing_version(self, tmp_path):
        missing = tmp_path / "gone.png"
        kept = tmp_path / "kept.png"
        kept.write_bytes(b"png")
        path = VideoDirectorAgent._asset_selected_path(
            {"selected": str(missing), "versions": [str(missing), str(kept)]}
        )
        assert path == str(kept)

    def test_video_model_selection_reads_the_mode_specific_field(self):
        mode, model = VideoDirectorAgent._select_video_model(
            {"video_generation_mode": "reference", "video_reference_model": "wan2.7-r2v"},
            {},
        )
        assert (mode, model) == ("reference", "wan2.7-r2v")

    def test_video_model_selection_falls_back_to_legacy_video_model(self):
        mode, model = VideoDirectorAgent._select_video_model(
            {},
            {"video_model": "wan2.7-i2v"},
        )
        assert mode == "first_frame"
        assert model == "wan2.7-i2v"

    def test_missing_video_model_raises(self):
        try:
            VideoDirectorAgent._select_video_model({}, {})
        except ValueError as exc:
            assert "video_model" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestReferenceNameMatching:
    def test_ratio_to_size(self):
        assert ratio_to_size("9:16") == "1080*1920"
        assert ratio_to_size("1:1") == "1024*1024"
        assert ratio_to_size("not-a-ratio") == "1920*1080"

    def test_asset_map_requires_an_id_and_a_selected_path(self):
        mapped = ReferenceGeneratorAgent._build_asset_map({
            "characters": [
                {"id": "char_liming", "selected": "/tmp/liming.png"},
                {"character_id": "char_li", "selected": ""},
                {"name": "无名"},
            ],
            "settings": [
                {"setting_id": "set_room", "selected": "/tmp/room.png"},
            ],
        })
        assert mapped == {
            "characters": {"char_liming": "/tmp/liming.png"},
            "settings": {"set_room": "/tmp/room.png"},
        }

    def test_collect_refs_exact_name_and_fuzzy_location(self):
        agent = ReferenceGeneratorAgent()
        asset_map = {
            "characters": {"char_liming": "liming.png", "char_li": "li.png"},
            "settings": {"set_room": "room.png"},
        }
        refs = agent._collect_refs(
            {"segment_id": "seg_01", "characters": ["李明"], "location": "高二三班教室门口"},
            asset_map,
            {"李明": "char_liming", "李": "char_li"},
            {"高二三班教室": "set_room"},
        )
        assert refs == [os.path.abspath("liming.png"), os.path.abspath("room.png")]

    def test_collect_refs_skips_unknown_names_and_caps_at_ten(self):
        agent = ReferenceGeneratorAgent()
        names = {f"角色{index}": f"id_{index}" for index in range(12)}
        asset_map = {"characters": {f"id_{index}": f"{index}.png" for index in range(12)}, "settings": {}}
        refs = agent._collect_refs(
            {"characters": [f"角色{index}" for index in range(12)] + ["不存在"], "location": ""},
            asset_map,
            names,
            {},
        )
        assert len(refs) == 10
        assert all(path.endswith(".png") for path in refs)

    def test_storyboard_matcher_does_not_invent_names(self):
        assert StoryboardAgent._match_characters("路人甲路过", ["张三"]) == []
