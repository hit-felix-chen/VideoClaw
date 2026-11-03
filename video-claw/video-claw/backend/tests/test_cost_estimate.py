"""Cost estimate: known prices, unknown prices, episodes, image bounds, empty sessions."""

from fastapi.testclient import TestClient

from api.app import app
from api.dependencies import workflow_engine
from models.config_model import MODEL_CONFIG, CostEstimate

IMAGE = "wan2.7-image"
IMAGE_PRO = "wan2.7-image-pro"
VIDEO_FLASH = "wan2.6-i2v-flash"
VIDEO_I2V = "wan2.7-i2v"


def _price(model_id: str, field: str) -> float:
    return float(MODEL_CONFIG["models"][model_id][field])


def test_known_prices():
    image_price = _price(IMAGE, "price_per_image")
    video_price = _price(VIDEO_FLASH, "price_per_second")
    assert image_price == 0.2
    assert video_price == 0.6

    result = CostEstimate.estimate(
        {"image": IMAGE, "video": VIDEO_FLASH},
        [{"episode_number": 1, "asset_count": 2, "duration_seconds": 10}],
    )

    assert result["unknown_models"] == []
    assert "currency" not in result
    episode = result["by_episode"][0]
    assert episode["image"] == {"min": 0.4, "max": 2.4}
    assert episode["video"] == 6.0
    assert episode["total"] == {"min": 6.4, "max": 8.4}
    assert result["total"] == {"min": 6.4, "max": 8.4}


def test_missing_price_is_unknown_not_zero():
    # deepseek-flash is registered for tokens, but CostEstimate bills price_per_image.
    assert "deepseek-flash" not in MODEL_CONFIG["models"]
    # wan2.7-image is registered, but it has no price_per_second.
    assert "price_per_second" not in MODEL_CONFIG["models"][IMAGE]

    missing_image = CostEstimate.estimate(
        {"image": "deepseek-flash", "video": VIDEO_FLASH},
        [{"episode_number": 1, "asset_count": 2, "duration_seconds": 10}],
    )
    assert missing_image["unknown_models"] == ["deepseek-flash"]
    assert missing_image["by_episode"][0]["image"] is None
    assert missing_image["by_episode"][0]["video"] == 6.0
    assert missing_image["by_episode"][0]["total"] is None
    assert missing_image["total"] is None
    # Treating the missing image price as zero would publish the video cost as the total.
    assert missing_image["total"] != {"min": 6.0, "max": 6.0}

    missing_video = CostEstimate.estimate(
        {"image": IMAGE, "video": IMAGE},
        [{"episode_number": 1, "asset_count": 1, "duration_seconds": 10}],
    )
    assert missing_video["unknown_models"] == [IMAGE]
    assert missing_video["by_episode"][0]["image"] == {"min": 0.2, "max": 1.2}
    assert missing_video["by_episode"][0]["video"] is None
    assert missing_video["total"] is None
    assert missing_video["total"] != {"min": 0.2, "max": 1.2}

    unknown_id = CostEstimate.estimate(
        {"image": IMAGE, "video": "no-such-video-model"},
        [{"episode_number": 1, "asset_count": 0, "duration_seconds": 4}],
    )
    assert unknown_id["unknown_models"] == ["no-such-video-model"]
    assert unknown_id["by_episode"][0]["video"] is None
    assert unknown_id["total"] is None


def test_multiple_episodes_sum_segment_durations():
    video_price = _price(VIDEO_I2V, "price_per_second")
    assert video_price == 0.8

    result = CostEstimate.from_session(
        {
            "image_t2i_model": IMAGE,
            "image_it2i_model": IMAGE_PRO,
        },
        {
            "storyboard": {
                "episodes": [
                    {
                        "episode_number": 1,
                        "segments": [
                            {"segment_id": "a", "total_duration": 10},
                            {"segment_id": "b", "total_duration": 5},
                        ],
                    },
                    {
                        "episode_number": 2,
                        "segments": [
                            {"segment_id": "c", "total_duration": 8},
                        ],
                    },
                ]
            }
        },
        video_model=VIDEO_I2V,
    )

    assert [row["episode_number"] for row in result["by_episode"]] == [1, 2]
    assert result["by_episode"][0]["asset_count"] == 2
    assert result["by_episode"][0]["duration_seconds"] == 15.0
    assert result["by_episode"][0]["video"] == 12.0
    assert result["by_episode"][0]["image"] == {"min": 0.4, "max": 2.4}
    assert result["by_episode"][1]["asset_count"] == 1
    assert result["by_episode"][1]["duration_seconds"] == 8.0
    assert result["by_episode"][1]["video"] == 6.4
    assert result["by_episode"][1]["total"] == {"min": 6.6, "max": 7.6}
    assert result["total"] == {"min": 19.0, "max": 22.0}
    assert result["unknown_models"] == []
    assert result["total"]["min"] == (
        result["by_episode"][0]["total"]["min"] + result["by_episode"][1]["total"]["min"]
    )


def test_image_range_bounds():
    price = _price(IMAGE, "price_per_image")
    result = CostEstimate.estimate(
        {"image": IMAGE, "video": VIDEO_FLASH},
        [{"episode_number": 1, "asset_count": 1, "duration_seconds": 0}],
    )
    image = result["by_episode"][0]["image"]
    assert CostEstimate.MIN_IMAGE_CALLS_PER_ASSET == 1
    assert CostEstimate.MAX_IMAGE_CALLS_PER_ASSET == 6
    assert image["min"] == round(price * CostEstimate.MIN_IMAGE_CALLS_PER_ASSET, 6)
    assert image["max"] == round(price * CostEstimate.MAX_IMAGE_CALLS_PER_ASSET, 6)
    assert image["max"] != round(price * 3, 6)
    assert result["by_episode"][0]["video"] == 0.0
    assert result["total"] == image


def test_reference_scenes_and_it2i_when_character_assets_exist():
    scenes = CostEstimate.from_session(
        {"image_t2i_model": IMAGE, "image_it2i_model": IMAGE_PRO},
        {
            "reference_generation": {
                "scenes": [
                    {"id": "s1", "episode": 1},
                    {"id": "s2", "episode": 1},
                    {"id": "s3", "episode": 2},
                ]
            }
        },
        video_model=VIDEO_FLASH,
    )
    assert [row["asset_count"] for row in scenes["by_episode"]] == [2, 1]
    assert scenes["by_episode"][0]["duration_seconds"] == 0.0
    assert scenes["by_episode"][0]["image"] == {"min": 0.4, "max": 2.4}
    assert scenes["total"] == {"min": 0.6, "max": 3.6}

    with_refs = CostEstimate.from_session(
        {"image_t2i_model": IMAGE, "image_it2i_model": IMAGE_PRO},
        {
            "character_design": {"characters": [{"id": "c1"}], "settings": []},
            "storyboard": {
                "episodes": [
                    {"episode_number": 1, "segments": [{"total_duration": 0}]},
                ]
            },
        },
        video_model=VIDEO_FLASH,
    )
    assert with_refs["by_episode"][0]["image"] == {"min": 0.4, "max": 2.4}


def test_missing_total_duration_uses_video_generation_default():
    missing = CostEstimate.from_session(
        {"image_t2i_model": IMAGE},
        {"storyboard": {"episodes": [{"episode_number": 1, "segments": [{}]}]}},
        video_model=VIDEO_FLASH,
    )
    assert missing["by_episode"][0]["duration_seconds"] == 10.0
    assert missing["by_episode"][0]["video"] == 6.0

    explicit_zero = CostEstimate.from_session(
        {"image_t2i_model": IMAGE},
        {
            "storyboard": {
                "episodes": [
                    {"episode_number": 1, "segments": [{"total_duration": 0}]},
                ]
            }
        },
        video_model=VIDEO_FLASH,
    )
    assert explicit_zero["by_episode"][0]["duration_seconds"] == 0.0
    assert explicit_zero["by_episode"][0]["video"] == 0.0


def test_empty_session():
    result = CostEstimate.from_session(
        {
            "image_t2i_model": IMAGE,
            "image_it2i_model": IMAGE,
            "video_first_frame_model": VIDEO_FLASH,
            "video_generation_mode": "first_frame",
        },
        {},
        video_model=VIDEO_FLASH,
    )
    assert result == {
        "by_episode": [],
        "total": {"min": 0.0, "max": 0.0},
        "unknown_models": [],
    }
    assert "currency" not in result


def test_cost_estimate_route_empty_session_and_known_prices():
    client = TestClient(app)
    missing = client.get("/api/project/does-not-exist/cost_estimate")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Session not found"

    session_id = "cost-estimate-empty"
    workflow_engine.create_session(
        session_id,
        {
            "image_t2i_model": IMAGE,
            "image_it2i_model": IMAGE,
            "video_first_frame_model": VIDEO_FLASH,
            "video_generation_mode": "first_frame",
            "video_model": VIDEO_FLASH,
        },
    )
    try:
        empty = client.get(f"/api/project/{session_id}/cost_estimate")
        assert empty.status_code == 200
        assert empty.json() == {
            "by_episode": [],
            "total": {"min": 0.0, "max": 0.0},
            "unknown_models": [],
        }
        assert "currency" not in empty.json()

        state = workflow_engine.get_state(session_id)
        assert state is not None
        state.artifacts = {
            "storyboard": {
                "episodes": [
                    {
                        "episode_number": 1,
                        "segments": [{"segment_id": "a", "total_duration": 10}],
                    }
                ]
            }
        }
        priced = client.get(f"/api/project/{session_id}/cost_estimate")
        assert priced.status_code == 200
        body = priced.json()
        assert body["unknown_models"] == []
        assert body["by_episode"][0]["asset_count"] == 1
        assert body["by_episode"][0]["duration_seconds"] == 10.0
        assert body["by_episode"][0]["image"] == {"min": 0.2, "max": 1.2}
        assert body["by_episode"][0]["video"] == 6.0
        assert body["total"] == {"min": 6.2, "max": 7.2}
        assert "currency" not in body
    finally:
        workflow_engine.delete_session(session_id)
