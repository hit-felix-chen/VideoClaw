"""Shared test utilities.

Keep helpers on classes so the suite does not grow a pile of module-level functions.
"""

import base64
import os
from typing import Optional


class SampleMedia:
    """Smallest real files the API accepts. No ffmpeg and no image library required."""

    PNG_1X1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    @staticmethod
    def png_bytes() -> bytes:
        return SampleMedia.PNG_1X1


class VersionFiles:
    """Create empty version files with a chosen mtime."""

    @staticmethod
    def touch(path: str, mtime: Optional[float] = None) -> str:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    @staticmethod
    def names(paths: list[str]) -> list[str]:
        return [os.path.basename(path) for path in paths]


class ProjectPayloads:
    """Bodies that satisfy the route schemas without selecting a live provider call."""

    @staticmethod
    def start_body(**overrides) -> dict:
        body = {
            "idea": "一个用于测试的想法",
            "llm_model": "deepseek-flash",
            "vlm_model": "qwen3.7-plus",
            "image_t2i_model": "doubao-seedream-5-0-260128",
            "image_it2i_model": "doubao-seedream-5-0-260128",
            "video_model": "wan2.7-i2v",
            "video_generation_mode": "first_frame",
            "episodes": 1,
        }
        body.update(overrides)
        return body


class FakeModelClients:
    """Replace provider clients. Sandbox routes import these classes inside the handler."""

    @staticmethod
    def llm(monkeypatch, text: str = "mocked-llm") -> None:
        class LLM:
            def query(self, prompt, model=None, web_search=False, **kwargs):
                return text

        monkeypatch.setattr("models.llm_openai.OpenAICompatLLM", LLM)

    @staticmethod
    def vlm(monkeypatch, text: str = "mocked-vlm") -> None:
        class VLM:
            def query(self, prompt, image_paths=None, model=None, **kwargs):
                return text

        monkeypatch.setattr("models.vlm_client.VLM", VLM)

    @staticmethod
    def image(monkeypatch, paths: Optional[list[str]] = None) -> None:
        produced = list(paths or [])

        class ImageClient:
            def generate_image(self, prompt, model=None, image_paths=None, video_ratio=None, **kwargs):
                return list(produced)

        monkeypatch.setattr("models.image_client.ImageClient", ImageClient)

    @staticmethod
    def video(monkeypatch) -> None:
        class VideoClient:
            def generate_video(
                self,
                prompt="",
                image_path="",
                save_path="",
                model="",
                duration=5,
                shot_type="multi",
                video_ratio="16:9",
                resolution="720P",
                **kwargs,
            ):
                if save_path:
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, "wb") as handle:
                        handle.write(b"fake-mp4")
                return save_path or "ok"

        monkeypatch.setattr("models.video_client.VideoClient", VideoClient)


class WorkflowStubs:
    """Stop stage execution from reaching an agent."""

    @staticmethod
    def silence_execution(monkeypatch) -> None:
        async def fake_stream(**kwargs):
            yield '{"type":"done","ok":true}\n'

        monkeypatch.setattr("api.routers.workflow.stream_workflow_task", fake_stream)


class ConfigState:
    """Snapshot Config class attributes around PUT /api/config."""

    @staticmethod
    def snapshot() -> dict:
        from config import CONFIG_PATH, Config

        tracked = {}
        for name in dir(Config):
            if name.startswith("_"):
                continue
            value = getattr(Config, name)
            if isinstance(value, (str, int, float, bool, dict, list)) or value is None:
                tracked[name] = value
        return {"attrs": tracked, "path": CONFIG_PATH, "existed": CONFIG_PATH.exists()}

    @staticmethod
    def restore(snapshot: dict) -> None:
        from config import Config

        for name, value in snapshot["attrs"].items():
            setattr(Config, name, value)
