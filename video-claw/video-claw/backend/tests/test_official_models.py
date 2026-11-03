"""Official ids, aliases, and time-based quotes. No network."""

from datetime import datetime, timezone

from config import DEFAULT_CONFIG
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.official import FETCHED, absent_ids
from models.middle.pricing import PriceBook, PriceUnit, QuoteRequest, quote
from models.middle.registry import ModelRegistry

_UTC = timezone.utc


def _at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=_UTC)


class TestDefaultsAndAliases:
    def test_defaults_resolve_with_official_capabilities(self):
        llm = ModelRegistry.get(DEFAULT_CONFIG["models"]["llm"])
        vlm = ModelRegistry.get(DEFAULT_CONFIG["models"]["vlm"])
        assert llm.id == "deepseek-flash"
        assert "llm" in llm.types
        assert llm.context_window == 1_000_000
        assert llm.max_output_tokens == 384_000
        assert llm.input_modalities == ["text", "image"]
        assert llm.base_url == "https://api.deepseek.com"
        assert llm.web_search["supported"] is False
        assert vlm.id == "qwen3.7-plus"
        assert "vlm" in vlm.types
        assert vlm.context_window == 1_000_000
        assert vlm.max_output_tokens == 131072
        assert vlm.input_modalities == ["text", "image", "video"]
        assert vlm.web_search["supported"] is True
        assert vlm.web_search["transport"] == "extra_body.enable_search"

    def test_display_alias_resolves_and_retired_ids_do_not(self):
        alias = ModelRegistry.get("deepseek-4.1-flash")
        assert alias is not None
        assert alias.id == "deepseek-flash"
        assert alias.name == "deepseek-4.1-flash"
        assert "deepseek-4.1-flash" not in {spec.id for spec in ModelRegistry.all()}
        for model_id in absent_ids():
            assert ModelRegistry.get(model_id) is None
        assert ModelRegistry.get("qwen3.8-plus") is None

    def test_every_default_is_priced_or_explicitly_unknown(self):
        unknown = set(PriceBook.unknown_models())
        for model_id in DEFAULT_CONFIG["models"].values():
            spec = ModelRegistry.get(model_id)
            assert spec is not None, model_id
            if model_id in unknown:
                assert not any(price.verified and price.source_url for price in spec.prices)
                continue
            priced = [price for price in spec.prices if price.verified]
            assert priced, model_id
            assert all(price.source_url.startswith("https://") for price in priced)
            assert all(price.fetched == FETCHED for price in priced)

    def test_price_table_rows_cite_a_source(self):
        rows = PriceBook.table()
        assert rows
        assert DEFAULT_REFERENCE_VIDEO_MODEL in {row["model_id"] for row in rows}
        assert "deepseek-flash" in {row["model_id"] for row in rows}
        for row in rows:
            assert row["source_url"].startswith("https://")
            assert row["fetched"] == FETCHED

    def test_retired_models_are_flagged_and_are_not_defaults(self):
        defaults = set(DEFAULT_CONFIG["models"].values())
        flagged = (
            "kimi-k2.6",
            "kimi-k2.7-code",
            "doubao-seed-2-0-pro-260215",
            "doubao-seedance-1-0-pro-250528",
            "doubao-seedream-4-5-251128",
            "doubao-seedream-4-0-250828",
            "seedance-1-0-pro",
            "gemini-3-flash-preview",
        )
        for model_id in flagged:
            spec = ModelRegistry.get(model_id)
            assert spec is not None, model_id
            assert spec.deprecated
            assert model_id not in defaults
        assert ModelRegistry.get("kimi-k2.6").deprecated_on == "2026-12-31"
        assert "2026-12-01" in " ".join(ModelRegistry.get("gemini-4-argon").todos)
        assert "2027-01-01" in " ".join(ModelRegistry.get("gemini-3.8-flash").todos)

    def test_kling_and_gemini_video_are_not_openai_compatible(self):
        kling = ModelRegistry.get("kling-v3")
        omni = ModelRegistry.get("gemini-omni-1.1-flash")
        assert kling.openai_compatible is False
        assert omni.openai_compatible is False
        assert ModelRegistry.adapter_for(kling, "llm") is None
        assert ModelRegistry.adapter_for(omni, "llm") is None
        assert ModelRegistry.get("kling 3.0 turbo (api id unverified)") is None
        assert ModelRegistry.get("kling 4.0") is None

    def test_unconfirmed_gemini_prices_stay_unknown(self):
        unknown = set(PriceBook.unknown_models())
        for model_id in (
            "gemini-3.6-flash",
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.1-flash-image",
            "gemini-3.1-flash-lite-image",
            "gemini-3-pro-image",
            "gemini-nano-banana-2.1",
            "gemini-3.8-flash-lite-tts",
        ):
            spec = ModelRegistry.get(model_id)
            assert spec is not None
            assert spec.prices == []
            assert model_id in unknown


class TestTierQuotes:
    def test_deepseek_flash_peak_and_off_peak_use_cache_miss(self):
        off_peak = _at(2026, 11, 2, 5)  # Monday 05:00 UTC, between the peak windows
        peak = _at(2026, 11, 2, 2)  # Monday 02:00 UTC
        weekend = _at(2026, 11, 7, 2)  # Saturday 02:00 UTC
        request = dict(model_id="deepseek-flash", input_tokens=1_000_000, output_tokens=1_000_000)
        off = quote(QuoteRequest(at=off_peak, **request))
        on = quote(QuoteRequest(at=peak, **request))
        sat = quote(QuoteRequest(at=weekend, **request))
        assert off["available"] is True
        assert off["currency"] == "USD"
        assert off["min"] == off["max"] == 0.75
        assert off["source_url"] == "https://api-docs.deepseek.com/quick_start/pricing"
        assert on["min"] == on["max"] == 1.5
        assert sat["min"] == 0.75
        miss = PriceBook.quote(
            "deepseek-flash",
            unit=PriceUnit.PER_MILLION_INPUT_TOKENS,
            documented_only=True,
            at=off_peak,
        )
        assert miss.amount == 0.15
        assert miss.cache == "miss"

    def test_qwen37_plus_input_length_boundary(self):
        moment = _at(2026, 11, 2, 3)
        low = quote(
            QuoteRequest(
                model_id="qwen3.7-plus",
                input_tokens=256_000,
                output_tokens=1_000_000,
                at=moment,
            )
        )
        high = quote(
            QuoteRequest(
                model_id="qwen3.7-plus",
                input_tokens=256_001,
                output_tokens=1_000_000,
                at=moment,
            )
        )
        assert low["currency"] == "CNY"
        assert low["min"] == low["max"] == 8.512
        assert high["min"] == high["max"] == 25.536006
        assert low["source_url"] == "https://help.aliyun.com/zh/model-studio/model-pricing"
        input_row = PriceBook.quote(
            "qwen3.7-plus",
            unit=PriceUnit.PER_MILLION_INPUT_TOKENS,
            documented_only=True,
            input_tokens=256_000,
        )
        assert input_row.amount == 2
        assert input_row.cache == "miss"

    def test_bailian_and_ark_windows_and_gemini_scheduled_rise(self):
        bailian = dict(model_id="deepseek-v4.1-flash", input_tokens=1_000_000, output_tokens=1_000_000)
        # 23:00 Shanghai is 15:00 UTC, inside 22:00-08:00 off-peak, including Saturday.
        night = quote(QuoteRequest(at=_at(2026, 11, 7, 15), **bailian))
        # 10:00 Shanghai is 02:00 UTC, daytime peak even on Saturday.
        day = quote(QuoteRequest(at=_at(2026, 11, 7, 2), **bailian))
        assert night["min"] == 5
        assert day["min"] == 10

        ark = dict(model_id="deepseek-v4-1-flash-261017", input_tokens=1_000_000, output_tokens=1_000_000)
        ark_peak = quote(QuoteRequest(at=_at(2026, 11, 2, 2), **ark))  # Mon 10:00 Shanghai
        ark_gap = quote(QuoteRequest(at=_at(2026, 11, 2, 5), **ark))  # Mon 13:00 Shanghai
        ark_weekend = quote(QuoteRequest(at=_at(2026, 11, 7, 2), **ark))
        assert ark_peak["min"] == 10
        assert ark_gap["min"] == 5
        assert ark_weekend["min"] == 5

        before = quote(
            QuoteRequest(
                model_id="gemini-4-argon",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                at=_at(2026, 11, 3, 0),
            )
        )
        after = quote(
            QuoteRequest(
                model_id="gemini-4-argon",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                at=_at(2026, 12, 1, 0),
            )
        )
        assert before["min"] == 12
        assert after["min"] == 24
        assert before["currency"] == "USD"
