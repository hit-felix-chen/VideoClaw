"""OpenAI-compatible LLM client. No network and no DashScope SDK."""

import importlib.util
from pathlib import Path

import pytest

from config import Config
from core.agents.doctor_agent import REASON_TYPE_PROMPT_TOO_LONG, DoctorAgent
from core.agents.script_agent import ScriptWriterAgent
from core.agents.storyboard_agent import StoryboardAgent
from models.llm_openai import OpenAICompatLLM
from models.middle.errors import UnsupportedParam
from models.middle.providers import OpenAICompatibleAdapter
from models.middle.registry import ModelRegistry

SMOKE_MODELS = (
    ("qwen3.6-max-preview", "dashscope"),
    ("deepseek-chat", "deepseek"),
    ("gemini-2.5-flash", "gemini"),
    ("gpt-4o", "openai"),
)


class _Message:
    def __init__(self, content, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning


class _Choice:
    def __init__(self, content, reasoning=None):
        self.message = _Message(content, reasoning)


class _Response:
    def __init__(self, content, reasoning=None):
        self.choices = [_Choice(content, reasoning)]


def _patch_complete(monkeypatch, content):
    calls = []

    def fake_complete(provider, model, messages, **extra):
        calls.append({"provider": provider, "model": model, "messages": messages, "extra": extra})
        return _Response(content)

    monkeypatch.setattr(OpenAICompatibleAdapter, "complete", staticmethod(fake_complete))
    return calls


class TestRegistryWebSearch:
    def test_dashscope_enable_search_is_model_specific(self):
        preview = ModelRegistry.get("qwen3.6-max-preview")
        listed = ModelRegistry.get("qwen3.7-max")
        assert preview.web_search["supported"] is False
        assert preview.web_search["verified"] is True
        assert preview.web_search["transport"] is None
        assert preview.web_search["source"].endswith("/web-search")
        assert "enable_search" in preview.web_search["note"]
        assert preview.input_modalities == ["text"]
        assert listed.web_search == {
            "supported": True,
            "verified": True,
            "transport": "extra_body.enable_search",
            "source": preview.web_search["source"],
        }
        assert listed.capability_dict()["web_search"]["supported"] is True

    def test_other_providers_do_not_invent_web_search(self):
        gemini = ModelRegistry.get("gemini-2.5-flash")
        deepseek = ModelRegistry.get("deepseek-chat")
        gpt = ModelRegistry.get("gpt-4o")
        assert gemini.web_search["supported"] is False
        assert "web_search" in gemini.web_search["source"]
        assert deepseek.web_search["supported"] is False
        assert gpt.web_search["supported"] is False
        assert gemini.input_modalities == ["text", "image"]
        assert gpt.input_modalities == ["text", "image"]
        assert deepseek.input_modalities == ["text"]
        assert ModelRegistry.get("kimi-k2.6").input_modalities == ["text", "image"]
        assert ModelRegistry.get("kimi-k2.6").web_search["supported"] is False

    def test_dashscope_chat_url_is_compatible_mode_and_native_config_stays(self):
        assert Config.DASHSCOPE_BASE_URL.rstrip("/").endswith("/api/v1")
        base_url, _key = OpenAICompatibleAdapter.endpoint("dashscope")
        assert base_url == Config.DASHSCOPE_BASE_URL.rstrip("/")[: -len("/api/v1")] + "/compatible-mode/v1"
        assert OpenAICompatibleAdapter.endpoint("openai")[0] == Config.OPENAI_BASE_URL
        assert OpenAICompatibleAdapter.endpoint("gemini")[0] == Config.GOOGLE_GEMINI_BASE_URL
        assert OpenAICompatibleAdapter.endpoint("deepseek")[0] == Config.DEEPSEEK_BASE_URL
        native = "https://dashscope-intl.aliyuncs.com/api/v1"
        assert OpenAICompatibleAdapter.chat_base_url("dashscope", native) == (
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        already = "https://example.invalid/compatible-mode/v1"
        assert OpenAICompatibleAdapter.chat_base_url("dashscope", already) == already
        assert OpenAICompatibleAdapter.chat_base_url("openai", "https://api.openai.com/v1") == (
            "https://api.openai.com/v1"
        )


class TestOpenAICompatLLM:
    def test_deleted_clients_and_no_dashscope_import_on_the_llm_path(self):
        for name in ("llm_client", "llm_gpt", "llm_gemini", "llm_deepseek", "llm_dashscope"):
            assert importlib.util.find_spec(f"models.{name}") is None
        source = Path("models/llm_openai.py").read_text(encoding="utf-8")
        assert "import dashscope" not in source
        assert "QwenVLClient" not in source

    def test_chat_completions_sends_enable_search_only_when_the_registry_allows_it(self, monkeypatch):
        created = {}

        class Completions:
            def create(self, **kwargs):
                created["kwargs"] = kwargs
                return _Response("天气不错")

        class FakeOpenAI:
            def __init__(self, **kwargs):
                created["init"] = kwargs
                self.chat = type("Chat", (), {"completions": Completions()})()

        monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
        text = OpenAICompatLLM().query("明天天气", model="qwen3.7-max", web_search=True)
        assert text == "天气不错"
        assert created["kwargs"]["model"] == "qwen3.7-max"
        assert created["kwargs"]["extra_body"] == {"enable_search": True}
        assert created["init"]["base_url"].endswith("/compatible-mode/v1")
        assert created["kwargs"]["messages"][1]["content"][0]["text"] == "明天天气"

        created.clear()
        plain = OpenAICompatLLM().query("写一场戏", model="qwen3.6-max-preview", web_search=False)
        assert plain == "天气不错"
        assert "extra_body" not in created["kwargs"]

    def test_missing_web_search_and_ignored_images_are_rejected(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise AssertionError("chat.completions should not be called")

        monkeypatch.setattr(OpenAICompatibleAdapter, "complete", staticmethod(refuse))
        for model in ("qwen3.6-max-preview", "gemini-2.5-flash", "deepseek-chat", "gpt-4o", "kimi-k2.6"):
            with pytest.raises(UnsupportedParam) as caught:
                OpenAICompatLLM().query("hi", model=model, web_search=True)
            assert caught.value.name == "web_search"

        with pytest.raises(UnsupportedParam) as images:
            OpenAICompatLLM().query(
                "hi",
                image_urls=["https://example.com/a.png"],
                model="qwen3.6-max-preview",
            )
        assert images.value.name == "image_urls"

    def test_registry_rejects_vlm_only_and_vl_names_without_the_vlm_client(self, monkeypatch):
        def refuse_vlm(*_args, **_kwargs):
            raise AssertionError("VLM client must not receive an LLM query")

        monkeypatch.setattr("models.vlm_dashscope.QwenVLClient.chat", refuse_vlm)
        calls = _patch_complete(monkeypatch, "kimi text")
        assert OpenAICompatLLM().query("看图", model="kimi-k2.6") == "kimi text"
        assert calls[0]["provider"] == "dashscope"
        assert calls[0]["model"] == "kimi-k2.6"

        for model in ("qwen3.6-plus", "qwen3.6-flash", "custom-vl-max"):
            with pytest.raises(UnsupportedParam) as caught:
                OpenAICompatLLM().query("hi", model=model)
            assert caught.value.name == "model"

    def test_gpt_and_gemini_send_image_url_parts(self, monkeypatch):
        calls = _patch_complete(monkeypatch, "seen")
        assert OpenAICompatLLM().query(
            "Ａ",
            image_urls=["https://example.com/a.png"],
            model="gpt-4o",
        ) == "seen"
        content = calls[0]["messages"][1]["content"]
        assert content[0] == {"type": "text", "text": "A"}
        assert content[1] == {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
        assert calls[0]["provider"] == "openai"

        calls.clear()
        OpenAICompatLLM().query("frame", image_urls=["https://example.com/b.png"], model="gemini-2.5-flash")
        assert calls[0]["provider"] == "gemini"
        assert calls[0]["messages"][1]["content"][1]["type"] == "image_url"

    def test_empty_lines_are_removed_and_reasoning_content_is_used(self):
        assert OpenAICompatLLM._text(_Response(None, "reasoned")) == "reasoned"
        calls_holder = {}

        def fake_complete(provider, model, messages, **extra):
            calls_holder["extra"] = extra
            return _Response("line\n\n\nnext")

        # Bound via the class so this test does not need monkeypatch for one call.
        original = OpenAICompatibleAdapter.complete
        OpenAICompatibleAdapter.complete = staticmethod(fake_complete)
        try:
            assert OpenAICompatLLM().query("p", model="deepseek-chat") == "line\nnext"
            assert calls_holder["extra"] == {}
        finally:
            OpenAICompatibleAdapter.complete = original


class TestAgentSmoke:
    @pytest.mark.parametrize(("model", "provider"), SMOKE_MODELS)
    async def test_script_storyboard_and_doctor_rewrite(self, monkeypatch, model, provider):
        calls = _patch_complete(
            monkeypatch,
            '{"rewrite_prompt": "shorter prompt", "scene": "night"}',
        )
        script = ScriptWriterAgent()._cancellable_query(
            OpenAICompatLLM(),
            "写一场戏",
            [],
            model,
            True,
            "sid",
            False,
        )
        storyboard = await StoryboardAgent()._query_json_object_with_retries(
            "分镜",
            model,
            "sid",
            label="smoke",
        )
        rewritten = DoctorAgent(llm_model=model).rewrite_prompt(
            prompt="原提示",
            reason="太长",
            reason_type=REASON_TYPE_PROMPT_TOO_LONG,
            stage="video",
        )
        assert "shorter prompt" in script
        assert storyboard["scene"] == "night"
        assert rewritten == "shorter prompt"
        assert [item["model"] for item in calls] == [model, model, model]
        assert {item["provider"] for item in calls} == {provider}
        assert all(item["extra"] == {} for item in calls)
