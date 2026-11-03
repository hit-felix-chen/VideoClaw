"""Version file names for characters, reference shots, and video clips."""

import os

from core.agents.character_agent import CharacterDesignerAgent
from core.agents.reference_agent import ReferenceGeneratorAgent
from core.agents.video_agent import VideoDirectorAgent
from tests.support import VersionFiles


class TestCharacterVersions:
    def test_first_file_and_then_increment(self, workspace):
        agent = CharacterDesignerAgent()
        assert agent._next_version_path("sid", "characters", "char_1").endswith(
            os.path.join("sid", "Assets", "characters", "char_1.png")
        )
        VersionFiles.touch(os.path.join("code/result/image/sid/Assets/characters", "char_1.png"), mtime=10)
        VersionFiles.touch(os.path.join("code/result/image/sid/Assets/characters", "char_1_v2.png"), mtime=20)
        VersionFiles.touch(os.path.join("code/result/image/sid/Assets/characters", "char_1_v3.jpg"), mtime=30)
        listed = agent._list_versions("sid", "characters", "char_1")
        assert VersionFiles.names(listed) == ["char_1.png", "char_1_v2.png", "char_1_v3.jpg"]
        assert os.path.basename(agent._next_version_path("sid", "characters", "char_1")) == "char_1_v4.png"

    def test_gap_uses_the_highest_version_number(self, workspace):
        agent = CharacterDesignerAgent()
        VersionFiles.touch(os.path.join("code/result/image/sid/Assets/characters", "char_9_v5.webp"))
        assert os.path.basename(agent._next_version_path("sid", "characters", "char_9")) == "char_9_v6.webp".replace(
            ".webp", ".png"
        )

    def test_asset_info_status_follows_files_on_disk(self, workspace):
        agent = CharacterDesignerAgent()
        failed = agent._build_asset_info("sid", "characters", "missing", "无名", "desc")
        assert failed["status"] == "failed"
        assert failed["versions"] == []
        path = VersionFiles.touch(os.path.join("code/result/image/sid/Assets/settings", "set_1.png"))
        done = agent._build_asset_info("sid", "settings", "set_1", "教室", "desc")
        assert done["status"] == "done"
        assert done["selected"] == path

    def test_sibling_ids_do_not_change_this_asset(self, workspace):
        agent = CharacterDesignerAgent()
        base = "code/result/image/sid/Assets/characters"
        VersionFiles.touch(os.path.join(base, "char_1.png"))
        VersionFiles.touch(os.path.join(base, "char_10.png"))
        VersionFiles.touch(os.path.join(base, "char_10_v9.png"))
        assert VersionFiles.names(agent._list_versions("sid", "characters", "char_1")) == ["char_1.png"]
        assert os.path.basename(agent._next_version_path("sid", "characters", "char_1")) == "char_1_v2.png"


class TestReferenceVersions:
    def test_first_jpg_then_increment(self, workspace):
        agent = ReferenceGeneratorAgent()
        assert os.path.basename(agent._next_version_path("sid", "shot_1")) == "shot_1.jpg"
        scene = "code/result/image/sid/Scenes"
        VersionFiles.touch(os.path.join(scene, "shot_1.jpg"), mtime=1)
        VersionFiles.touch(os.path.join(scene, "shot_1_v2.png"), mtime=2)
        assert VersionFiles.names(agent._list_versions("sid", "shot_1")) == ["shot_1.jpg", "shot_1_v2.png"]
        assert os.path.basename(agent._next_version_path("sid", "shot_1")) == "shot_1_v3.jpg"
        assert ReferenceGeneratorAgent._list_versions_static("sid", "shot_1") == agent._list_versions("sid", "shot_1")

    def test_sibling_shot_ids_are_not_versions(self, workspace):
        agent = ReferenceGeneratorAgent()
        scene = "code/result/image/sid/Scenes"
        VersionFiles.touch(os.path.join(scene, "shot_1.jpg"))
        VersionFiles.touch(os.path.join(scene, "shot_10_v3.jpg"))
        assert VersionFiles.names(agent._list_versions("sid", "shot_1")) == ["shot_1.jpg"]
        assert os.path.basename(agent._next_version_path("sid", "shot_1")) == "shot_1_v2.jpg"


class TestVideoVersions:
    def test_final_cut_is_not_a_version_and_numbers_increment(self, workspace):
        agent = VideoDirectorAgent()
        assert os.path.basename(agent._next_version_path("sid", "seg_1")) == "seg_1.mp4"
        video = "code/result/video/sid"
        VersionFiles.touch(os.path.join(video, "seg_1_final.mp4"), mtime=1)
        assert agent._list_versions("sid", "seg_1") == []
        VersionFiles.touch(os.path.join(video, "seg_1.mp4"), mtime=2)
        VersionFiles.touch(os.path.join(video, "seg_1_v2.mp4"), mtime=3)
        VersionFiles.touch(os.path.join(video, "seg_1_v2_final.mp4"), mtime=4)
        assert VersionFiles.names(agent._list_versions("sid", "seg_1")) == ["seg_1.mp4", "seg_1_v2.mp4"]
        assert os.path.basename(agent._next_version_path("sid", "seg_1")) == "seg_1_v3.mp4"

    def test_sibling_segment_ids_are_not_versions(self, workspace):
        agent = VideoDirectorAgent()
        video = "code/result/video/sid"
        VersionFiles.touch(os.path.join(video, "seg_1.mp4"))
        VersionFiles.touch(os.path.join(video, "seg_10_v4.mp4"))
        assert VersionFiles.names(agent._list_versions("sid", "seg_1")) == ["seg_1.mp4"]
        assert os.path.basename(agent._next_version_path("sid", "seg_1")) == "seg_1_v2.mp4"

    def test_missing_reference_image_does_not_allocate_a_file_or_call_a_model(self, workspace):
        agent = VideoDirectorAgent()
        called = {"video": False}

        class Client:
            def generate_video(self, **kwargs):
                called["video"] = True

        segment_id, path, rewrite = agent._generate_one(
            "sid",
            "seg_1",
            "prompt",
            img_path=str(workspace / "missing.png"),
            video_model="wan2.7-i2v",
        )
        assert (segment_id, path, rewrite) == ("seg_1", None, None)
        assert called["video"] is False
        assert not os.path.exists(os.path.join("code/result/video/sid", "seg_1.mp4"))

        agent.cancellation_check = lambda: True
        image = workspace / "frame.png"
        image.write_bytes(b"png")
        assert agent._generate_one("sid", "seg_1", "prompt", str(image), "wan2.7-i2v") == ("seg_1", None, None)
