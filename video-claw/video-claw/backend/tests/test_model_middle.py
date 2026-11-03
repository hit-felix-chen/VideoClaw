"""Middle layer: normalized capabilities, exact ids, Seedance 2.5, TTS."""

from dataclasses import dataclass

from config import Config
from models.config_model import MODEL_CONFIG, get_model_config
from models.middle.adapter import ProviderAdapter
from models.middle.catalog import PRICE_DOC, SEEDANCE_25_TODO, TASK_DOC
from models.middle.errors import UnsupportedParam
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.pricing import PriceBook
from models.middle.providers import OpenAICompatibleAdapter
from models.middle.registry import ModelRegistry
from models.middle.spec import ModelSpec, PriceUnit
from models.video_seedance import SeedanceVideoClient

SEEDANCE = DEFAULT_REFERENCE_VIDEO_MODEL


class TestIdsAndPrices:
    def test_empty_id_matches_nothing_and_costs_nothing(self):
        for empty in ("", "   ", None):
            assert ModelRegistry.get(empty) is None
            assert PriceBook.prices(empty) == []
            assert PriceBook.quote(empty) is None
            assert PriceBook.cost(empty, quantity=1) is None
        loaded = get_model_config("")
        assert loaded["provider"] == "unknown"
        assert loaded["name"] == ""
        assert "price_per_1k_input_token" not in loaded

    def test_substring_does_not_select_a_neighbor(self):
        assert get_model_config("deepseek")["provider"] == "unknown"
        assert get_model_config("chat")["provider"] == "unknown"
        assert ModelRegistry.get("doubao-seedance-2-5") is None
        assert get_model_config("models/deepseek-flash")["name"] == "deepseek-4.1-flash"

    def test_explicit_seedance_alias_is_exact(self):
        spec = ModelRegistry.get("doubao-seedance-2.5")
        assert spec is not None
        assert spec.id == SEEDANCE
        assert ModelRegistry.get("doubao-seedance-2.5-extra") is None


class TestSeedance25:
    def test_official_capabilities_and_unset_price_per_second(self):
        spec = ModelRegistry.get(SEEDANCE)
        assert spec.name == "Seedance 2.5"
        assert spec.provider == "ark"
        assert spec.resolutions == ["480p", "720p", "1080p"]
        assert spec.default_resolution == "720p"
        assert spec.aspect_ratios == ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"]
        assert spec.default_ratio == "adaptive"
        assert spec.duration.min_seconds == 4
        assert spec.duration.max_seconds == 30
        assert spec.duration.default == -1
        assert spec.duration.allow_auto is True
        assert spec.duration.verified is True
        assert spec.max_reference_images == 30
        assert spec.max_reference_videos == 10
        assert spec.max_reference_audios == 10
        assert spec.currency == "CNY"
        assert spec.resolutions_verified is True
        assert spec.max_reference_images_verified is True
        assert "reference_to_video" in spec.adapter_abilities
        assert "reference_to_video" in spec.defaults
        assert TASK_DOC in spec.sources
        assert PRICE_DOC in spec.sources
        assert all(price.unit != PriceUnit.PER_SECOND for price in spec.prices)
        assert any("price_per_second" in item for item in SEEDANCE_25_TODO)
        metadata = get_model_config(SEEDANCE)
        assert "price_per_second" not in metadata
        assert metadata["currency"] == "CNY"

    def test_token_prices_need_a_resolution_and_video_flag(self):
        assert PriceBook.quote(SEEDANCE) is None
        assert PriceBook.quote(SEEDANCE, resolution="720p") is None
        no_video = PriceBook.quote(SEEDANCE, resolution="720P", includes_video_input=False)
        with_video = PriceBook.quote(SEEDANCE, resolution="1080p", includes_video_input=True)
        assert no_video.amount == 70.0
        assert no_video.currency == "CNY"
        assert no_video.unit == PriceUnit.PER_MILLION_TOKENS
        assert no_video.verified is True
        assert no_video.list_price is True
        assert with_video.amount == 46.0
        assert PriceBook.quote(SEEDANCE, resolution="480p", includes_video_input=False).amount == 70.0
        assert PriceBook.quote(SEEDANCE, resolution="1080p", includes_video_input=False).amount == 77.0
        assert PriceBook.quote(SEEDANCE, resolution="4k", includes_video_input=False) is None
        assert PriceBook.cost(SEEDANCE, resolution="720p", includes_video_input=False) is None
        priced = PriceBook.cost(
            SEEDANCE,
            resolution="720p",
            includes_video_input=False,
            completion_tokens=1_000_000,
        )
        assert priced.total == 70.0
        assert priced.currency == "CNY"

    def test_normalize_rejects_unsupported_values(self):
        spec = ModelRegistry.get(SEEDANCE)
        adapter = ModelRegistry.adapter_for(spec, "video")
        unified = adapter.normalize(spec, {"resolution": "720P", "video_ratio": "16:9", "duration": 5})
        assert unified["resolution"] == "720p"
        assert unified["ratio"] == "16:9"

        low = adapter.normalize(
            spec,
            {
                "resolution": "480p",
                "ratio": "adaptive",
                "duration": -1,
                "reference_image_paths": ["a.png", "b.png"],
            },
        )
        assert low["resolution"] == "480p"
        assert low["duration"] == -1
        assert low["generation_mode"] == "reference"

        with_raises = (
            {"resolution": "4k", "ratio": "16:9", "duration": 5},
            {"resolution": "720p", "ratio": "16:9", "duration": 3},
            {"resolution": "720p", "ratio": "16:9", "duration": 31},
            {"resolution": "720p", "ratio": "2:1", "duration": 5},
            {"resolution": "720p", "ratio": "16:9", "duration": 5, "reference_image_paths": ["x"] * 31},
            {
                "resolution": "720p",
                "ratio": "16:9",
                "duration": 5,
                "image_path": "frame.png",
                "reference_image_paths": ["ref.png"],
            },
            {"resolution": "720p", "ratio": "16:9", "duration": 5, "image_path": "frame.png"},
        )
        for params in with_raises:
            try:
                adapter.normalize(spec, params)
            except UnsupportedParam:
                continue
            raise AssertionError(params)

    def test_content_roles_for_reference_images(self):
        content = SeedanceVideoClient.build_content(
            "a clip",
            reference_image_paths=["https://example.com/a.png", "https://example.com/b.png"],
        )
        roles = [item.get("role") for item in content if item["type"] == "image_url"]
        assert roles == ["reference_image", "reference_image"]
        framed = SeedanceVideoClient.build_content(
            "a clip",
            image_path="https://example.com/first.png",
            last_image_path="https://example.com/last.png",
        )
        assert [item["role"] for item in framed if item["type"] == "image_url"] == [
            "first_frame",
            "last_frame",
        ]


class TestRegistryBehavior:
    def test_config_prices_stay_unlabeled_and_resolutions_fold(self):
        assert "720P" in MODEL_CONFIG["models"]["wan2.7-i2v"]["capabilities"]["resolutions"]
        spec = ModelRegistry.get("wan2.7-i2v")
        assert spec.resolutions == ["720p", "1080p"]
        assert spec.currency is None
        assert spec.prices == []
        assert PriceBook.quote("wan2.7-image") is None
        assert PriceBook.cost("wan2.7-i2v", quantity=10) is None

    def test_edge_tts_is_a_modality_without_a_price(self):
        spec = ModelRegistry.get("edge-tts")
        assert spec.provider == "edge"
        assert "tts" in spec.types
        assert spec.output == "audio"
        assert spec.prices == []
        assert spec.max_reference_images is None
        assert PriceBook.cost("edge-tts", quantity=1) is None
        adapter = ModelRegistry.adapter_for(spec, "tts")
        assert adapter is not None
        assert adapter.owns_submit is True
        assert adapter.classify_error(UnsupportedParam("voice", "nope", [])) == "unsupported_param"

    def test_openai_compatible_surface_does_not_call_the_network(self):
        messages = OpenAICompatibleAdapter.messages("hello", ["https://example.com/a.png"])
        assert messages[0]["role"] == "system"
        assert messages[1]["content"][0] == {"type": "text", "text": "hello"}
        assert messages[1]["content"][1]["type"] == "image_url"
        base_url, _api_key = OpenAICompatibleAdapter.endpoint("openai")
        assert base_url == Config.OPENAI_BASE_URL
        deepseek = ModelRegistry.get("deepseek-flash")
        assert isinstance(ModelRegistry.adapter_for(deepseek, "llm"), OpenAICompatibleAdapter)
        assert ModelRegistry.adapter_for(ModelRegistry.get("gemini-omni-1.1-flash"), "llm") is None
        assert ModelRegistry.adapter_for(ModelRegistry.get("kling-v3"), "llm") is None

    def test_default_reference_model(self):
        assert Config.VIDEO_REFERENCE_MODEL == SEEDANCE

    def test_new_video_provider_is_selected_without_a_name_branch(self):
        @dataclass
        class _Box:
            called: dict

        box = _Box(called={})

        class ExampleAdapter(ProviderAdapter):
            provider = "example"
            providers = frozenset({"example"})
            modalities = frozenset({"video"})
            owns_submit = True

            def normalize(self, spec, params):
                from models.middle.normalize import ParamNormalizer

                return ParamNormalizer.video(spec, params, resolution_case="lower")

            def submit(self, spec, params, *, transport=None):
                box.called["model"] = spec.id
                box.called["resolution"] = params.get("resolution")
                return "example-url"

        spec = ModelSpec(
            id="example-video",
            name="Example Video",
            provider="example",
            types=["video"],
            abilities=["reference_to_video"],
            adapter_abilities=["reference_to_video"],
            resolutions=["720p"],
            api_contract_verified=True,
        )
        adapter = ExampleAdapter()
        ModelRegistry.add_spec(spec)
        ModelRegistry.register(adapter)
        try:
            from models.video_client import VideoClient

            url = VideoClient().generate_video(
                prompt="x",
                image_path=None,
                save_path="/tmp/example.mp4",
                model="example-video",
                resolution="720P",
                duration=4,
            )
            assert url == "example-url"
            assert box.called == {"model": "example-video", "resolution": "720p"}
        finally:
            ModelRegistry.remove_spec("example-video")
            ModelRegistry.unregister(adapter)

    def test_dashscope_keeps_vendor_resolution_and_multi_shot(self, monkeypatch):
        captured = {}

        class FakeDashscope:
            def generate_video(self, **kwargs):
                captured.update(kwargs)
                return "dash-url"

        from models.video_client import VideoClient

        client = VideoClient()
        client._dashscope_client = FakeDashscope()
        url = client.generate_video(
            prompt="scene",
            image_path="frame.png",
            save_path="/tmp/wan.mp4",
            model="wan2.7-i2v",
            resolution="1080P",
            duration=5,
            shot_type="multi",
            video_ratio="16:9",
        )
        assert url == "dash-url"
        assert captured["resolution"] == "1080P"
        assert captured["shot_type"] == "multi"

        captured.clear()
        client.generate_video(
            prompt="scene",
            image_path="frame.png",
            save_path="/tmp/horse.mp4",
            model="happyhorse-1.0-i2v",
            resolution="720p",
            duration=5,
            shot_type="multi",
        )
        assert captured["resolution"] == "720P"
        assert captured["shot_type"] == "single"


class TestApiModelsMiddle:
    def test_seedance_and_tts_are_listed(self, client):
        listed = client.get("/api/models")
        assert listed.status_code == 200
        body = listed.json()["models"]
        seedance = next(item for item in body if item["id"] == SEEDANCE)
        assert seedance["provider"] == "ark"
        assert seedance["media_type"] == "video"
        assert seedance["currency"] == "CNY"
        assert seedance["capabilities"]["resolutions"] == ["480p", "720p", "1080p"]
        assert seedance["capabilities"]["max_reference_images"] == 30
        assert seedance["capabilities"]["duration"]["min"] == 4
        assert seedance["capabilities"]["duration"]["max"] == 30
        assert seedance["capabilities"]["verified"]["price"] is True
        assert any(row["amount"] == 70 and row["resolution"] == "720p" for row in seedance["pricing"])
        assert "price_per_second" not in seedance

        reference = client.get(
            "/api/models",
            params={"media_type": "video", "ability": "reference_to_video", "verified_only": True},
        )
        assert reference.status_code == 200
        assert SEEDANCE in {item["id"] for item in reference.json()["models"]}

        wan = next(item for item in body if item["id"] == "wan2.7-i2v")
        assert wan["capabilities"]["resolutions"] == ["720p", "1080p"]
        assert wan["currency"] is None
        assert wan["pricing"] == []

        speech = client.get("/api/models", params={"model_type": "tts"})
        assert speech.status_code == 200
        edge = speech.json()["models"]
        edge_row = next(item for item in edge if item["id"] == "edge-tts")
        assert edge_row["pricing"] == []
        assert edge_row["currency"] is None
        assert edge_row["model_type"] == "tts"

        llm = client.get("/api/models", params={"model_type": "llm"})
        sample = next(item for item in llm.json()["models"] if item["id"] == "deepseek-flash")
        assert "text_generation" in sample["ability_types"]
        assert "currency" in sample
