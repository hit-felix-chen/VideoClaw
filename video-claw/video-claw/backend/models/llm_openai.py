"""One LLM client for every provider.

Calls go through ``OpenAICompatibleAdapter.complete`` (``chat.completions``).
The model id is resolved on ``ModelRegistry``. Name fragments such as
``kimi``, ``vl``, or ``qwen3.6-plus`` do not select a different client.

VLM modules (``vlm_dashscope.py``, ``vlm_gemini.py``, ``vlm_gpt.py``) are
unchanged. This client never imports them. Pointing those VLM calls at the
same chat.completions path is a follow-up.
"""

import logging

from config import Config
from models.middle.errors import UnsupportedParam
from models.middle.providers import OpenAICompatibleAdapter
from models.middle.registry import ModelRegistry

logger = logging.getLogger(__name__)

_ENABLE_SEARCH = "extra_body.enable_search"


class OpenAICompatLLM:
    """Chat completion with the historical ``query`` argument order."""

    DEFAULT_MODEL = "deepseek-flash"

    def query(
        self,
        prompt,
        image_urls=None,
        model=DEFAULT_MODEL,
        safe_content=True,
        task_id=None,
        web_search=False,
    ):
        if not model:
            model = self.DEFAULT_MODEL
        spec = ModelRegistry.get(model)
        if spec is None or "llm" not in spec.modalities:
            allowed = list(spec.types) if spec is not None else []
            raise UnsupportedParam("model", model, allowed)

        urls = [url for url in (image_urls or []) if url]
        if urls and "image" not in (spec.input_modalities or []):
            raise UnsupportedParam("image_urls", urls, spec.input_modalities or ["text"])

        extra = {}
        if web_search:
            capability = spec.web_search or {}
            if not capability.get("supported") or capability.get("transport") != _ENABLE_SEARCH:
                raise UnsupportedParam("web_search", True, [False])
            extra["extra_body"] = {"enable_search": True}

        if safe_content:
            prompt = self.full_to_half(prompt)
        if Config.PRINT_MODEL_INPUT:
            logger.info(
                "LLM query model=%s provider=%s images=%s web_search=%s task_id=%s prompt=%s",
                spec.id,
                spec.provider,
                len(urls),
                bool(web_search),
                task_id,
                (prompt or "")[:200],
            )

        messages = OpenAICompatibleAdapter.messages(prompt or "", urls or None)
        response = OpenAICompatibleAdapter.complete(spec.provider, spec.id, messages, **extra)
        result = self._text(response)
        if safe_content:
            result = self.full_to_half(result)
        return "\n".join(line for line in result.split("\n") if line.strip())

    @staticmethod
    def full_to_half(text):
        if not isinstance(text, str):
            return text
        translation_table = {0x3000: 0x0020}
        for code in range(65281, 65375):
            translation_table[code] = code - 65248
        return text.translate(translation_table)

    @staticmethod
    def _text(response) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("LLM response had no choices")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if isinstance(content, str) and content.strip():
            return content
        reasoning = getattr(message, "reasoning_content", None) if message is not None else None
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
        raise RuntimeError("LLM response had empty content")
