"""Registry reads. Prices come from MODEL_CONFIG, not from a provider."""

from models.config_model import (
    MODEL_CONFIG,
    get_max_concurrency,
    get_model_config,
    get_models_by_type,
    parse_api_model,
)

PRICE_KEYS = (
    "price_per_1k_input_token",
    "price_per_1k_output_token",
    "price_per_image",
    "price_per_second",
)


class TestModelConfig:
    def test_registry_has_prices_for_every_model(self):
        models = MODEL_CONFIG["models"]
        assert "wan2.7-i2v" in models
        assert "deepseek-chat" not in models
        assert "qwen3.6-max-preview" not in models
        for model_id, metadata in models.items():
            assert metadata["name"]
            assert metadata["provider"]
            assert isinstance(metadata["type"], list) and metadata["type"]
            assert isinstance(metadata["concurrency"], int) and metadata["concurrency"] > 0
            present = [key for key in PRICE_KEYS if key in metadata]
            assert present, model_id
            for key in present:
                assert isinstance(metadata[key], (int, float))
                assert metadata[key] >= 0

    def test_get_model_config_returns_the_registered_price(self):
        registered = MODEL_CONFIG["models"]["wan2.7-image"]
        loaded = get_model_config("wan2.7-image")
        assert loaded["price_per_image"] == registered["price_per_image"] == 0.2
        assert loaded["provider"] == "dashscope"

        video_id = next(
            model_id
            for model_id, metadata in MODEL_CONFIG["models"].items()
            if "video" in (metadata.get("type") or []) and "price_per_second" in metadata
        )
        assert get_model_config(video_id)["price_per_second"] == MODEL_CONFIG["models"][video_id]["price_per_second"]

        image_id = next(
            model_id
            for model_id, metadata in MODEL_CONFIG["models"].items()
            if "price_per_image" in metadata
        )
        assert get_model_config(image_id)["price_per_image"] == MODEL_CONFIG["models"][image_id]["price_per_image"]

    def test_exact_id_is_not_replaced_by_a_shorter_neighbor(self):
        for model_id, metadata in MODEL_CONFIG["models"].items():
            loaded = get_model_config(model_id)
            assert loaded["name"] == metadata["name"]
            assert loaded["provider"] == metadata["provider"]

    def test_loose_alias_still_exposes_a_real_price(self):
        loaded = get_model_config("models/deepseek-flash")
        assert loaded["id"] == "deepseek-flash"
        assert loaded["name"] == "deepseek-4.1-flash"
        alias = get_model_config("deepseek-4.1-flash")
        assert alias["id"] == "deepseek-flash"

    def test_unknown_model_is_a_stub_without_a_price(self):
        loaded = get_model_config("not-a-real-model-xyz")
        assert loaded == {
            "name": "not-a-real-model-xyz",
            "provider": "unknown",
            "type": [],
            "concurrency": 3,
        }
        assert not any(key in loaded for key in PRICE_KEYS)

    def test_empty_model_id_is_unknown(self):
        loaded = get_model_config("")
        assert loaded["provider"] == "unknown"
        assert loaded["name"] == ""
        assert loaded["concurrency"] == 3

    def test_concurrency_switch(self):
        assert get_max_concurrency("deepseek-flash", enable_concurrency=False) == 1
        assert get_max_concurrency("deepseek-flash", enable_concurrency=True) == 10
        assert get_max_concurrency("not-a-real-model-xyz", enable_concurrency=True) == 3

    def test_models_by_type_and_parse_api_model(self):
        llm_ids = {item["id"] for item in get_models_by_type("llm")}
        assert "deepseek-flash" in llm_ids
        assert "deepseek-chat" not in llm_ids
        assert all("llm" in item["type"] for item in get_models_by_type("llm"))
        assert parse_api_model("api/dashscope/wan2.7-i2v", "video") == ("dashscope", "wan2.7-i2v")
        provider, model_id = parse_api_model("deepseek-flash", "image")
        assert provider == "" and model_id == "deepseek-flash"
