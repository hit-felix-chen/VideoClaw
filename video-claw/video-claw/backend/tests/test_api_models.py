"""GET /api/models stays on the main flow after it leaves the pipeline router."""


class TestApiModels:
    def test_model_type_media_type_and_ability_filters(self, client):
        models = client.get("/api/models", params={"model_type": "llm"})
        assert models.status_code == 200
        body = models.json()["models"]
        ids = {item["id"] for item in body}
        assert "deepseek-chat" in ids
        sample = next(item for item in body if item["id"] == "deepseek-chat")
        assert sample["provider"] == "deepseek"
        assert sample["model_type"] == "llm"
        assert sample["label"]
        assert "text_generation" in sample["ability_types"]
        assert "capabilities" in sample

        media = client.get("/api/models", params={"media_type": "image"})
        assert media.status_code == 200
        assert media.json()["models"]
        assert all(item["media_type"] == "image" for item in media.json()["models"])

        video = client.get(
            "/api/models",
            params={"media_type": "video", "ability": "first_frame_i2v", "verified_only": True},
        )
        assert video.status_code == 200
        selected = video.json()["models"]
        assert selected
        for item in selected:
            tags = set(item.get("adapter_ability_types") or [])
            tags.update(item.get("ability_types") or [])
            if item.get("ability_type"):
                tags.add(item["ability_type"])
            assert "first_frame_i2v" in tags
            assert item["media_type"] == "video"

        assert client.get("/api/models", params={"model_type": "nope"}).status_code == 422
        assert client.get("/api/models", params={"media_type": "audio"}).status_code == 422
