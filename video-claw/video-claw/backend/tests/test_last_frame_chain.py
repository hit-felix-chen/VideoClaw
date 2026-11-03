# -*- coding: utf-8 -*-
"""Last-frame continuity: command, episode grouping, and regenerate stale flags."""

import os
import shutil
import subprocess
import threading

import pytest

from core.agents.base_agent import SESSION_PARAM_KEYS
from core.agents.last_frame_chain import LastFrameChain
from core.agents.video_agent import VideoDirectorAgent
from core.orchestrator import WorkflowEngine
from core.utils.video_frames import VideoFrameExtractor, extract_last_frame


def _reference(seg):
    return f"ref/{seg['segment_id']}.jpg"


def _episodes():
    return [
        {"segment_id": "seg_01_02", "episode": 1, "episode_number": 1},
        {"segment_id": "seg_01_01", "episode": 1, "episode_number": 1},
        {"segment_id": "seg_02_01", "episode": 2, "episode_number": 2},
    ]


def test_extract_last_frame_command_construction():
    cmd = VideoFrameExtractor.extract_last_frame_command("ffmpeg", "in.mp4", "out.jpg")
    assert cmd == [
        "ffmpeg",
        "-y",
        "-sseof",
        "-0.1",
        "-i",
        "in.mp4",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "out.jpg",
    ]
    assert cmd[cmd.index("-sseof") + 1] == "-0.1"
    assert cmd[cmd.index("-frames:v") + 1] == "1"


def test_extract_last_frame_uses_constructed_command(monkeypatch, tmp_path):
    seen = {}

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    def fake_run(cmd, check, stdout, stderr):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = tmp_path / "Frames" / "seg_last.jpg"
    assert extract_last_frame("clip.mp4", str(output)) == str(output)
    assert seen["cmd"] == VideoFrameExtractor.extract_last_frame_command(
        "/usr/bin/ffmpeg", "clip.mp4", str(output),
    )
    assert os.path.isdir(output.parent)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_extract_last_frame_writes_jpeg(tmp_path):
    video = tmp_path / "clip.mp4"
    frame = tmp_path / "Frames" / "seg_last.jpg"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=0.4",
            "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    extract_last_frame(str(video), str(frame))
    assert frame.is_file()
    assert frame.stat().st_size > 0


def test_episode_grouping_order_and_boundary():
    segments = _episodes()
    groups = LastFrameChain.group_segments(segments)
    assert [[seg["segment_id"] for seg in group] for group in groups] == [
        ["seg_01_02", "seg_01_01"],
        ["seg_02_01"],
    ]

    # An id that looks like episode 2 stays in the episode field it was given.
    mislabeled = [
        {"segment_id": "seg_02_01", "episode": 1},
        {"segment_id": "seg_01_01", "episode": 1},
    ]
    assert LastFrameChain.group_segments(mislabeled)[0][0]["segment_id"] == "seg_02_01"

    by_number = [
        {"segment_id": "a", "episode_number": 4},
        {"segment_id": "b", "episode_number": 4},
        {"segment_id": "c", "episode_number": 5},
    ]
    assert [[seg["segment_id"] for seg in group] for group in LastFrameChain.group_segments(by_number)] == [
        ["a", "b"],
        ["c"],
    ]

    # No episode field: do not invent a chain by parsing ids.
    unscoped = [{"segment_id": "seg_01_01"}, {"segment_id": "seg_01_02"}]
    unscoped_groups = LastFrameChain.group_segments(unscoped)
    assert len(unscoped_groups) == 2


def test_plan_default_off_unchanged_and_boundary_not_crossed():
    segments = _episodes()
    off = LastFrameChain.plan_first_frames(segments, chain=False, sid="S", reference_for=_reference)
    assert [item["segment_id"] for item in off] == ["seg_01_02", "seg_01_01", "seg_02_01"]
    assert [item["img_path"] for item in off] == [
        "ref/seg_01_02.jpg",
        "ref/seg_01_01.jpg",
        "ref/seg_02_01.jpg",
    ]
    assert all(item["chained"] is False and item["prev_id"] is None for item in off)
    assert len({item["parallel_group"] for item in off}) == 3

    on = LastFrameChain.plan_first_frames(segments, chain=True, sid="S", reference_for=_reference)
    assert [item["segment_id"] for item in on] == ["seg_01_02", "seg_01_01", "seg_02_01"]
    assert on[0]["img_path"] == "ref/seg_01_02.jpg"
    assert on[0]["chained"] is False
    assert on[1]["img_path"] == LastFrameChain.frame_path("S", "seg_01_02")
    assert on[1]["prev_id"] == "seg_01_02"
    assert on[1]["chained"] is True
    assert "Frames" in on[1]["img_path"]
    assert on[1]["img_path"].endswith(".jpg")
    # Flat index would hand seg_02_01 the previous shot. The episode boundary stops that.
    assert on[2]["img_path"] == "ref/seg_02_01.jpg"
    assert on[2]["chained"] is False
    assert on[2]["prev_id"] is None
    assert on[0]["parallel_group"] == on[1]["parallel_group"]
    assert on[2]["parallel_group"] != on[0]["parallel_group"]

    assert LastFrameChain.active(False, "first_frame") is False
    assert LastFrameChain.active(None, "first_frame") is False
    assert LastFrameChain.active(True, "start_end_frame") is False
    assert LastFrameChain.active(True, "reference") is False
    assert LastFrameChain.active(True, "first_frame") is True


def test_cascade_and_stale_stay_inside_episode():
    segments = [
        {"segment_id": "a", "episode": 1},
        {"segment_id": "b", "episode": 1},
        {"segment_id": "c", "episode": 1},
        {"segment_id": "d", "episode": 2},
        {"segment_id": "e", "episode": 2},
    ]
    assert LastFrameChain.expand_cascade(["b"], segments, False) == ["b"]
    assert LastFrameChain.expand_cascade(["b"], segments, True) == ["b", "c"]
    assert LastFrameChain.expand_cascade(["b", "d"], segments, True) == ["b", "d", "c", "e"]
    assert "d" not in LastFrameChain.expand_cascade(["c"], segments, True)
    assert LastFrameChain.stale_ids(["b"], segments) == ["c"]
    assert LastFrameChain.stale_ids(["a"], segments) == ["b", "c"]
    assert LastFrameChain.stale_ids(LastFrameChain.expand_cascade(["a"], segments, True), segments) == []
    assert LastFrameChain.stale_ids(["c"], segments) == []
    assert "d" not in LastFrameChain.stale_ids(["c"], segments)
    assert "e" not in LastFrameChain.stale_ids(["a"], segments)


def test_selected_video_prefers_chosen_file(tmp_path):
    chosen = tmp_path / "chosen.mp4"
    newer = tmp_path / "newer.mp4"
    chosen.write_bytes(b"a")
    newer.write_bytes(b"b")
    assert LastFrameChain.selected_video_path(
        {"selected": str(chosen)},
        [str(chosen), str(newer)],
    ) == str(chosen)
    missing = tmp_path / "gone.mp4"
    assert LastFrameChain.selected_video_path(
        {"selected": str(missing)},
        [str(chosen), str(newer)],
    ) == str(newer)


def test_chain_last_frame_defaults_off():
    assert "chain_last_frame" in SESSION_PARAM_KEYS
    agent = VideoDirectorAgent()
    merged = agent._merge_session_params({"session_id": "s", "_session_meta": {}})
    assert not merged.get("chain_last_frame")
    merged_off = agent._merge_session_params({
        "session_id": "s",
        "_session_meta": {"chain_last_frame": False},
    })
    assert merged_off["chain_last_frame"] is False
    merged_on = agent._merge_session_params({
        "session_id": "s",
        "_session_meta": {"chain_last_frame": True},
    })
    assert merged_on["chain_last_frame"] is True


def test_list_versions_ignores_frame_files(tmp_path):
    video_dir = tmp_path / "video"
    frames = tmp_path / "Frames"
    video_dir.mkdir()
    frames.mkdir()
    first = video_dir / "seg_01_01.mp4"
    second = video_dir / "seg_01_01_v2.mp4"
    final = video_dir / "seg_01_01_final.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    final.write_bytes(b"c")
    (video_dir / "seg_01_01_last.jpg").write_bytes(b"d")
    (frames / "seg_01_01_last.jpg").write_bytes(b"e")
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))
    agent = VideoDirectorAgent()
    agent._video_base = lambda sid: str(video_dir)  # type: ignore[method-assign]
    listed = [os.path.basename(path) for path in agent._list_versions("sid", "seg_01_01")]
    assert listed == ["seg_01_01.mp4", "seg_01_01_v2.mp4"]


def test_build_payload_stale_flag_and_default_omission():
    agent = VideoDirectorAgent()
    agent._list_versions = lambda sid, seg: []  # type: ignore[method-assign]
    segments = [
        {"segment_id": "a", "episode": 1, "episode_number": 1, "segment_number": 1, "total_duration": 5},
        {"segment_id": "b", "episode": 1, "episode_number": 1, "segment_number": 2, "total_duration": 5},
    ]
    clips = [
        {"id": "a", "description": "分镜列表：\n分镜1：[5秒] a"},
        {"id": "b", "description": "分镜列表：\n分镜1：[5秒] b", "stale": True},
    ]
    plain = agent._build_payload("s", segments, [clips[0]])
    assert "stale" not in plain["payload"]["clips"][0]
    marked = agent._build_payload("s", segments, clips, fresh_ids=["a"], stale_ids=["b"])
    by_id = {item["id"]: item for item in marked["payload"]["clips"]}
    assert by_id["a"]["stale"] is False
    assert by_id["b"]["stale"] is True


def _storyboard(episodes):
    return {
        "session_id": "s",
        "video_generation_mode": "first_frame",
        "video_first_frame_model": "wan2.7-i2v",
        "enable_concurrency": False,
        "style": "realistic",
        "_session_artifacts": {
            "storyboard": {"episodes": episodes},
            "reference_generation": {"scenes": []},
            "character_design": {},
            "video_generation": {"clips": []},
        },
    }


def _shot(segment_id, episode, content="动作"):
    return {
        "segment_id": segment_id,
        "episode": episode,
        "episode_number": episode,
        "segment_number": 1,
        "total_duration": 5,
        "shots": [{"duration": 5, "content": content}],
    }


def _bind_generator(monkeypatch, agent, tmp_path):
    recorded = []
    lock = threading.Lock()

    def fake_generate(sid, segment_id, prompt, img_path, *args, **kwargs):
        path = tmp_path / f"{segment_id}.mp4"
        path.write_bytes(b"v")
        with lock:
            recorded.append({
                "id": segment_id,
                "img": img_path,
                "last": args[7] if len(args) > 7 else kwargs.get("last_image_path"),
                "mode": args[6] if len(args) > 6 else kwargs.get("video_generation_mode"),
            })
        return segment_id, str(path), None

    monkeypatch.setattr(agent, "_generate_one", fake_generate)
    monkeypatch.setattr(agent, "_get_reference_image", lambda sid, seg_id, scene_map: f"ref/{seg_id}.jpg")
    monkeypatch.setattr(agent, "_get_next_reference_image", lambda *args, **kwargs: "next-ref.jpg")
    monkeypatch.setattr(agent, "_report_progress", lambda *args, **kwargs: None)
    return recorded


@pytest.mark.parametrize("flag", [False, None])
async def test_process_default_off_keeps_reference_stills(monkeypatch, tmp_path, flag):
    agent = VideoDirectorAgent()
    recorded = _bind_generator(monkeypatch, agent, tmp_path)
    monkeypatch.setattr(agent, "_list_versions", lambda sid, seg: [])

    def forbid_chain(*args, **kwargs):
        raise AssertionError("chain runner entered while chain_last_frame is off")

    monkeypatch.setattr(agent, "_generate_chain", forbid_chain)
    episodes = [
        {"episode_number": 1, "segments": [_shot("a", 1), _shot("b", 1)]},
        {"episode_number": 2, "segments": [_shot("c", 2)]},
    ]
    data = _storyboard(episodes)
    if flag is not None:
        data["chain_last_frame"] = flag
    await agent.process(data, None)
    assert [item["id"] for item in recorded] == ["a", "b", "c"]
    assert [item["img"] for item in recorded] == ["ref/a.jpg", "ref/b.jpg", "ref/c.jpg"]
    assert [item["last"] for item in recorded] == ["next-ref.jpg", "next-ref.jpg", "next-ref.jpg"]


async def test_process_start_end_frame_ignores_chain_flag(monkeypatch, tmp_path):
    agent = VideoDirectorAgent()
    recorded = _bind_generator(monkeypatch, agent, tmp_path)
    monkeypatch.setattr(agent, "_list_versions", lambda sid, seg: [])

    def forbid_chain(*args, **kwargs):
        raise AssertionError("start_end_frame must not enter the last-frame chain")

    monkeypatch.setattr(agent, "_generate_chain", forbid_chain)
    data = _storyboard([
        {"episode_number": 1, "segments": [_shot("a", 1), _shot("b", 1)]},
    ])
    data["video_generation_mode"] = "start_end_frame"
    data["video_start_end_model"] = "wan2.7-i2v"
    data["chain_last_frame"] = True
    await agent.process(data, None)
    assert [item["img"] for item in recorded] == ["ref/a.jpg", "ref/b.jpg"]
    assert [item["mode"] for item in recorded] == ["start_end_frame", "start_end_frame"]
    assert [item["last"] for item in recorded] == ["next-ref.jpg", "next-ref.jpg"]


async def test_process_chain_orders_inside_episode_and_stops_at_boundary(monkeypatch, tmp_path):
    agent = VideoDirectorAgent()
    recorded = _bind_generator(monkeypatch, agent, tmp_path)
    monkeypatch.setattr(agent, "_list_versions", lambda sid, seg: [])
    extracted = []

    def frame_path(sid, segment_id):
        return str(tmp_path / "Frames" / f"{segment_id}_last.jpg")

    def fake_extract(video, output):
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "wb") as handle:
            handle.write(b"jpg")
        extracted.append((video, output))
        return output

    monkeypatch.setattr(LastFrameChain, "frame_path", staticmethod(frame_path))
    monkeypatch.setattr("core.utils.video_frames.extract_last_frame", fake_extract)
    data = _storyboard([
        {"episode_number": 1, "segments": [_shot("a", 1), _shot("b", 1)]},
        {"episode_number": 2, "segments": [_shot("c", 2), _shot("d", 2)]},
    ])
    data["chain_last_frame"] = True
    await agent.process(data, None)
    by_id = {item["id"]: item["img"] for item in recorded}
    assert [item["id"] for item in recorded] == ["a", "b", "c", "d"]
    assert by_id["a"] == "ref/a.jpg"
    assert by_id["c"] == "ref/c.jpg"
    assert by_id["b"] == frame_path("s", "a")
    assert by_id["d"] == frame_path("s", "c")
    assert by_id["d"] != frame_path("s", "b")
    sources = {os.path.basename(video): os.path.basename(output) for video, output in extracted}
    assert sources["a.mp4"] == "a_last.jpg"
    assert sources["c.mp4"] == "c_last.jpg"
    assert "b.mp4" not in sources


async def test_process_regenerate_cascade_and_stale(monkeypatch, tmp_path):
    agent = VideoDirectorAgent()
    recorded = _bind_generator(monkeypatch, agent, tmp_path)
    selected_a = tmp_path / "selected_a.mp4"
    selected_a.write_bytes(b"old")

    def versions(sid, seg):
        if seg == "a":
            return [str(selected_a)]
        return []

    monkeypatch.setattr(agent, "_list_versions", versions)
    extracted = []

    def frame_path(sid, segment_id):
        return str(tmp_path / "Frames" / f"{segment_id}_last.jpg")

    def fake_extract(video, output):
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "wb") as handle:
            handle.write(b"jpg")
        extracted.append(os.path.basename(video))
        return output

    monkeypatch.setattr(LastFrameChain, "frame_path", staticmethod(frame_path))
    monkeypatch.setattr("core.utils.video_frames.extract_last_frame", fake_extract)

    episodes = [{
        "episode_number": 1,
        "segments": [_shot("a", 1), _shot("b", 1), _shot("c", 1)],
    }, {
        "episode_number": 2,
        "segments": [_shot("d", 2)],
    }]
    data = _storyboard(episodes)
    data["chain_last_frame"] = True
    data["_session_artifacts"]["video_generation"]["clips"] = [
        {"id": "a", "selected": str(selected_a), "description": "分镜列表：\n分镜1：[5秒] a", "episode": 1},
        {"id": "b", "selected": "", "description": "分镜列表：\n分镜1：[5秒] b", "episode": 1},
        {"id": "c", "selected": "", "description": "分镜列表：\n分镜1：[5秒] c", "episode": 1},
        {"id": "d", "selected": "", "description": "分镜列表：\n分镜1：[5秒] d", "episode": 2},
    ]
    single = {"regenerate_clips": ["b"]}
    result = await agent.process(data, single)
    assert single["regenerate_clips"] == ["b"]
    assert [item["id"] for item in recorded] == ["b"]
    assert recorded[0]["img"] == frame_path("s", "a")
    assert extracted == ["selected_a.mp4"]
    by_id = {item["id"]: item for item in result["payload"]["clips"]}
    assert by_id["b"]["stale"] is False
    assert by_id["c"]["stale"] is True
    assert "stale" not in by_id["a"] or by_id["a"]["stale"] is False
    assert "stale" not in by_id["d"] or by_id["d"]["stale"] is False

    recorded.clear()
    extracted.clear()
    cascaded = {"regenerate_clips": ["b"], "cascade": True}
    result = await agent.process(data, cascaded)
    assert cascaded["regenerate_clips"] == ["b", "c"]
    assert [item["id"] for item in recorded] == ["b", "c"]
    assert recorded[1]["img"] == frame_path("s", "b")
    by_id = {item["id"]: item for item in result["payload"]["clips"]}
    assert by_id["c"]["stale"] is False
    assert "d" not in cascaded["regenerate_clips"]
    assert not by_id["d"].get("stale")


async def test_cascade_without_chain_keeps_reference_and_skips_stale(monkeypatch, tmp_path):
    agent = VideoDirectorAgent()
    recorded = _bind_generator(monkeypatch, agent, tmp_path)
    monkeypatch.setattr(agent, "_list_versions", lambda sid, seg: [])

    def forbid_chain(*args, **kwargs):
        raise AssertionError("cascade without chain_last_frame must stay on the old path")

    monkeypatch.setattr(agent, "_generate_chain", forbid_chain)
    data = _storyboard([{
        "episode_number": 1,
        "segments": [_shot("a", 1), _shot("b", 1), _shot("c", 1)],
    }, {
        "episode_number": 2,
        "segments": [_shot("d", 2)],
    }])
    intervention = {"regenerate_clips": ["b"], "cascade": True}
    result = await agent.process(data, intervention)
    assert intervention["regenerate_clips"] == ["b", "c"]
    assert [item["id"] for item in recorded] == ["b", "c"]
    assert [item["img"] for item in recorded] == ["ref/b.jpg", "ref/c.jpg"]
    assert all("stale" not in clip for clip in result["payload"]["clips"])


def test_regeneration_merge_keeps_stale_on_untouched_clips():
    existing = {"clips": [
        {
            "id": "a",
            "selected": "old-a.mp4",
            "versions": ["old-a.mp4"],
            "status": "done",
            "description": "keep-a",
            "stale": True,
        },
        {
            "id": "b",
            "selected": "old-b.mp4",
            "versions": ["old-b.mp4"],
            "status": "done",
            "description": "keep-b",
        },
    ]}
    payload = {"clips": [
        {
            "id": "a",
            "selected": "new-a.mp4",
            "versions": ["old-a.mp4", "new-a.mp4"],
            "status": "done",
            "description": "keep-a",
            "stale": False,
        },
        {
            "id": "b",
            "selected": "old-b.mp4",
            "versions": ["old-b.mp4"],
            "status": "done",
            "description": "keep-b",
            "stale": True,
        },
    ]}
    merged = WorkflowEngine._merge_item_regeneration_payload(
        existing, payload, ["clips"], {"clips": {"a"}},
    )
    by_id = {item["id"]: item for item in merged["clips"]}
    assert by_id["a"]["stale"] is False
    assert by_id["a"]["selected"] == "old-a.mp4"
    assert by_id["a"]["versions"] == ["old-a.mp4", "new-a.mp4"]
    assert by_id["b"]["stale"] is True
    assert by_id["b"]["selected"] == "old-b.mp4"
    assert by_id["b"]["description"] == "keep-b"
