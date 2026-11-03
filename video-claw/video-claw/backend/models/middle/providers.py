"""Adapters over the clients that already live in ``models/``.

Video submit is owned by these adapters so a reference-image list reaches
Seedance. LLM chat completions go through ``OpenAICompatibleAdapter`` and
``OpenAICompatLLM``. Image and VLM adapters still delegate to their clients.
"""

from typing import Any, Optional

from models.middle.adapter import ProviderAdapter
from models.middle.normalize import ParamNormalizer


class _VideoAdapter(ProviderAdapter):
    modalities = frozenset({"video"})
    owns_submit = True
    fill_resolution = ""

    def normalize(self, spec, params: dict) -> dict:
        filled = dict(params)
        if not filled.get("resolution") and self.fill_resolution:
            filled["resolution"] = spec.default_resolution or self.fill_resolution
        return ParamNormalizer.video(spec, filled, resolution_case=self.resolution_case)


class DashScopeVideoAdapter(_VideoAdapter):
    provider = "dashscope"
    providers = frozenset({"dashscope"})
    resolution_case = "upper"
    fill_resolution = "720p"

    def submit(self, spec, params: dict, *, transport: Any = None) -> str:
        client = transport or self._transport()
        return client._generate_wan(
            params.get("prompt") or "",
            params.get("image_path"),
            params.get("save_path") or "",
            spec.id,
            params.get("duration") if params.get("duration") is not None else 5,
            params.get("shot_type") or "single",
            params.get("video_ratio") or params.get("ratio") or "16:9",
            params.get("last_image_path"),
            params.get("first_clip_path"),
            params.get("reference_image_path"),
            params.get("reference_image_paths"),
            params.get("reference_video_paths"),
            params.get("reference_audio_path"),
            params.get("audio_path"),
            params.get("negative_prompt"),
            params.get("resolution"),
            params.get("prompt_extend"),
            False if params.get("watermark") is None else params.get("watermark"),
            params.get("seed"),
            params.get("audio"),
        )

    @staticmethod
    def _transport():
        from models.video_client import VideoClient

        return VideoClient()


class KlingVideoAdapter(_VideoAdapter):
    provider = "kling"
    providers = frozenset({"kling"})
    resolution_case = "upper"
    fill_resolution = ""

    def submit(self, spec, params: dict, *, transport: Any = None) -> str:
        client = transport or self._transport()
        return client._generate_kling(
            params.get("prompt") or "",
            params.get("image_path"),
            params.get("save_path") or "",
            spec.id,
            params.get("duration") if params.get("duration") is not None else 5,
            params.get("sound") or "",
            params.get("video_ratio") or params.get("ratio") or "16:9",
            params.get("resolution"),
            params.get("mode") or "pro",
            params.get("cfg_scale") if params.get("cfg_scale") is not None else 0.5,
            params.get("negative_prompt") or "",
        )

    @staticmethod
    def _transport():
        from models.video_client import VideoClient

        return VideoClient()


class ArkVideoAdapter(_VideoAdapter):
    """Seedance on Volcengine Ark. Reference images use role ``reference_image``."""

    provider = "ark"
    providers = frozenset({"ark"})
    resolution_case = "lower"
    fill_resolution = "720p"

    def submit(self, spec, params: dict, *, transport: Any = None) -> str:
        client = transport or self._transport()
        task_id = params.get("vendor_task_id")
        if isinstance(task_id, str) and task_id.strip():
            return self._seedance(transport).resume(task_id.strip(), params.get("save_path") or "")
        return client._generate_seedance(
            params.get("prompt") or "",
            params.get("image_path"),
            params.get("save_path") or "",
            spec.id,
            params.get("duration") if params.get("duration") is not None else 5,
            params.get("video_ratio") or params.get("ratio") or "adaptive",
            params.get("resolution"),
            params.get("seed"),
            params.get("watermark"),
            params.get("generate_audio"),
            reference_image_paths=params.get("reference_image_paths"),
            last_image_path=params.get("last_image_path"),
            on_task=params.get("on_task"),
        )

    def poll(self, spec, task_id: str, *, transport: Any = None) -> str:
        client = self._seedance(transport)
        return client.poll_task(task_id)

    def fetch(self, spec, ref: Any, *, transport: Any = None) -> str:
        client = self._seedance(transport)
        if isinstance(ref, str):
            raise TypeError("fetch expects {'url', 'save_path'}")
        return client.download(ref["url"], ref["save_path"])

    def _seedance(self, transport: Any):
        if transport is not None and hasattr(transport, "seedance_client"):
            return transport.seedance_client
        from models.video_seedance import SeedanceVideoClient

        return SeedanceVideoClient()

    @staticmethod
    def _transport():
        from models.video_client import VideoClient

        return VideoClient()


class DashScopeImageAdapter(ProviderAdapter):
    provider = "dashscope"
    providers = frozenset({"dashscope"})
    modalities = frozenset({"image"})

    def submit(self, spec, params: dict, *, transport: Any = None) -> list:
        client = getattr(transport, "dashscope_client", None) if transport is not None else None
        if client is None:
            from models.image_dashscope import DashScopeClient

            client = DashScopeClient()
        paths = params.get("image_paths") or []
        if paths:
            return client.edit_image(
                prompt=params.get("prompt") or "",
                image_urls=paths,
                model=spec.id,
                size=params.get("size"),
                session_id=params.get("session_id"),
                save_dir=params.get("save_dir"),
            )
        return client.generate_image(
            prompt=params.get("prompt") or "",
            model=spec.id,
            size=params.get("size") or "1024*1024",
            session_id=params.get("session_id"),
            save_dir=params.get("save_dir"),
        )


class ArkImageAdapter(ProviderAdapter):
    provider = "ark"
    providers = frozenset({"ark"})
    modalities = frozenset({"image"})

    def submit(self, spec, params: dict, *, transport: Any = None) -> list:
        client = getattr(transport, "seedream_client", None) if transport is not None else None
        if client is None:
            from models.image_seedream import SeedreamClient

            client = SeedreamClient()
        return client.generate_image(
            prompt=params.get("prompt") or "",
            model=spec.id,
            session_id=params.get("session_id") or "default",
            size=params.get("size") or "2048*2048",
            image_paths=params.get("image_paths"),
        )


class OpenAIImageAdapter(ProviderAdapter):
    provider = "openai"
    providers = frozenset({"openai"})
    modalities = frozenset({"image"})

    def submit(self, spec, params: dict, *, transport: Any = None) -> list:
        client = getattr(transport, "gpt_client", None) if transport is not None else None
        if client is None:
            from models.image_gpt import ImageGPT

            client = ImageGPT()
        path = client.generate_image(
            prompt=params.get("prompt") or "",
            size=params.get("size") or "1024x1024",
            model=spec.id,
            save_dir=params.get("save_dir"),
        )
        return [path] if path else []


class OpenAICompatibleAdapter(ProviderAdapter):
    """OpenAI ``chat.completions`` surface for every LLM provider.

    ``OpenAICompatLLM`` calls ``complete``. Keys and base URLs come from
    ``Config`` via ``endpoint``. DashScope's configured root is the native
    SDK URL (``/api/v1``); chat completions are on ``/compatible-mode/v1``
    of the same host. Image, video, and VLM clients keep the native URL.
    """

    provider = "openai-compatible"
    providers = frozenset({"openai", "deepseek", "gemini", "dashscope", "ark", "moonshot"})
    modalities = frozenset({"llm"})

    def matches(self, spec, modality: Optional[str] = None) -> bool:
        if spec is not None and spec.openai_compatible is False:
            return False
        return super().matches(spec, modality)

    @staticmethod
    def chat_base_url(provider: str, base_url: str) -> str:
        url = (base_url or "").rstrip("/")
        if provider == "gemini":
            if url.endswith("/openai"):
                return url
            if url.endswith("/v1beta"):
                return url + "/openai"
            return url
        if provider != "dashscope" or not url:
            return url
        if url.endswith("/compatible-mode/v1"):
            return url
        if url.endswith("/api/v1"):
            return url[: -len("/api/v1")] + "/compatible-mode/v1"
        return url

    @staticmethod
    def endpoint(provider: str) -> tuple:
        from config import Config

        endpoints = {
            "openai": (Config.OPENAI_BASE_URL, Config.OPENAI_API_KEY),
            "deepseek": (Config.DEEPSEEK_BASE_URL, Config.DEEPSEEK_API_KEY),
            "gemini": (Config.GOOGLE_GEMINI_BASE_URL, Config.GEMINI_API_KEY),
            "dashscope": (Config.DASHSCOPE_BASE_URL, Config.DASHSCOPE_API_KEY),
            "ark": (Config.ARK_BASE_URL, Config.ARK_API_KEY),
            "moonshot": (Config.MOONSHOT_BASE_URL, Config.MOONSHOT_API_KEY),
        }
        base_url, api_key = endpoints.get(provider, ("", ""))
        return OpenAICompatibleAdapter.chat_base_url(provider, base_url or ""), api_key or ""

    @staticmethod
    def messages(
        prompt: str,
        image_urls: Optional[list] = None,
        system: str = "You are a helpful assistant.",
    ) -> list:
        content = [{"type": "text", "text": prompt}]
        for url in image_urls or []:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

    @staticmethod
    def client(provider: str):
        from openai import OpenAI

        from config import Config

        base_url, api_key = OpenAICompatibleAdapter.endpoint(provider)
        kwargs = {"api_key": api_key or "missing", "timeout": 300}
        if base_url:
            kwargs["base_url"] = base_url
        proxy = Config.provider_proxy(provider)
        if proxy:
            import httpx

            kwargs["http_client"] = httpx.Client(proxy=proxy, timeout=300)
        return OpenAI(**kwargs)

    @staticmethod
    def complete(provider: str, model: str, messages: list, **extra):
        client = OpenAICompatibleAdapter.client(provider)
        return client.chat.completions.create(model=model, messages=messages, **extra)

    def submit(self, spec, params: dict, *, transport: Any = None) -> Any:
        messages = params.get("messages") or OpenAICompatibleAdapter.messages(
            params.get("prompt") or "",
            params.get("image_urls"),
        )
        extra = dict(params.get("extra") or {})
        return OpenAICompatibleAdapter.complete(spec.provider, spec.id, messages, **extra)


class DashScopeVlmAdapter(ProviderAdapter):
    provider = "dashscope"
    providers = frozenset({"dashscope"})
    modalities = frozenset({"vlm"})

    def submit(self, spec, params: dict, *, transport: Any = None) -> str:
        client = getattr(transport, "dashscope_client", None) if transport is not None else None
        if client is None:
            from models.vlm_dashscope import QwenVLClient

            client = QwenVLClient()
        return client.chat(
            text=params.get("prompt") or "",
            images=params.get("image_paths") or [],
            model=spec.id,
            stream=False,
        )


class GeminiVlmAdapter(ProviderAdapter):
    provider = "gemini"
    providers = frozenset({"gemini"})
    modalities = frozenset({"vlm"})

    def submit(self, spec, params: dict, *, transport: Any = None) -> str:
        client = getattr(transport, "gemini_client", None) if transport is not None else None
        if client is None:
            from models.vlm_gemini import GeminiVLClient

            client = GeminiVLClient()
        return client.chat(
            text=params.get("prompt") or "",
            images=params.get("image_paths") or [],
            model=spec.id,
        )


class OpenAIVlmAdapter(ProviderAdapter):
    provider = "openai"
    providers = frozenset({"openai"})
    modalities = frozenset({"vlm"})

    def submit(self, spec, params: dict, *, transport: Any = None) -> str:
        client = getattr(transport, "gpt_client", None) if transport is not None else None
        if client is None:
            from models.vlm_gpt import GPTVLClient

            client = GPTVLClient()
        return client.chat(
            text=params.get("prompt") or "",
            images=params.get("image_paths") or [],
            model=spec.id,
        )


class EdgeTtsAdapter(ProviderAdapter):
    """First TTS provider. Other vendors register another ``ProviderAdapter``."""

    provider = "edge"
    providers = frozenset({"edge"})
    modalities = frozenset({"tts"})
    owns_submit = True

    def submit(self, spec, params: dict, *, transport: Any = None) -> str:
        import asyncio

        from models.tts_edge import EdgeTts

        coroutine = EdgeTts.generate(
            text=params.get("text") or "",
            output_path=params.get("output_path") or "",
            voice=params.get("voice") or "zh-CN-YunjianNeural",
            speed=params.get("speed", 1.0),
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        raise RuntimeError(
            "EdgeTtsAdapter.submit cannot run inside a running event loop; await EdgeTts.generate"
        )

    @staticmethod
    async def generate(text: str, output_path: str, voice: str = "zh-CN-YunjianNeural", speed: float = 1.0) -> str:
        from models.tts_edge import EdgeTts

        return await EdgeTts.generate(text, output_path, voice=voice, speed=speed)


def register_adapters() -> None:
    from models.middle.registry import ModelRegistry

    for adapter in (
        DashScopeVideoAdapter(),
        KlingVideoAdapter(),
        ArkVideoAdapter(),
        DashScopeImageAdapter(),
        ArkImageAdapter(),
        OpenAIImageAdapter(),
        OpenAICompatibleAdapter(),
        DashScopeVlmAdapter(),
        GeminiVlmAdapter(),
        OpenAIVlmAdapter(),
        EdgeTtsAdapter(),
    ):
        ModelRegistry.register(adapter)
