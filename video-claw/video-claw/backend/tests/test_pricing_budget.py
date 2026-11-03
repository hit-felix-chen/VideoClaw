"""R3 price book, pre-call ranges, and the per-project budget gate.

Documented prices are the verified Seedance rows (and any fixture a test
registers). Config amounts without a source URL stay unknown and are never
turned into a number.
"""

import pytest

from models.middle.budget import ProjectBudget
from models.middle.catalog import PRICE_DOC, SEEDANCE_25_TODO
from models.middle.ids import DEFAULT_REFERENCE_VIDEO_MODEL
from models.middle.pricing import PriceBook, QuoteRequest, quote
from models.middle.registry import ModelRegistry
from models.middle.spec import ModelSpec, PriceQuote, PriceUnit
from storage import Database, LedgerStore, ProjectStore
from storage.database import StorageError

SEEDANCE = DEFAULT_REFERENCE_VIDEO_MODEL
IMAGE_FIXTURE = "priced-image-fixture"
UNVERIFIED_FIXTURE = "unverified-image-fixture"


def _documented_image(amount: float = 0.5) -> None:
    ModelRegistry.add_spec(
        ModelSpec(
            id=IMAGE_FIXTURE,
            name="Fixture image",
            provider="fixture",
            types=["t2i"],
            currency="CNY",
            prices=[
                PriceQuote(
                    model_id=IMAGE_FIXTURE,
                    amount=amount,
                    currency="CNY",
                    unit=PriceUnit.PER_IMAGE,
                    verified=True,
                    resolution="720p",
                    effective_date="2026-01-01",
                    source_url="https://example.com/vendor-price",
                    source="https://example.com/vendor-price",
                )
            ],
        )
    )


def _unverified_image() -> None:
    ModelRegistry.add_spec(
        ModelSpec(
            id=UNVERIFIED_FIXTURE,
            name="Unverified image",
            provider="fixture",
            types=["t2i"],
            prices=[
                PriceQuote(
                    model_id=UNVERIFIED_FIXTURE,
                    amount=9.0,
                    currency="CNY",
                    unit=PriceUnit.PER_IMAGE,
                    verified=False,
                    source_url="https://example.com/not-verified",
                )
            ],
        )
    )


def _remove_fixtures() -> None:
    ModelRegistry.remove_spec(IMAGE_FIXTURE)
    ModelRegistry.remove_spec(UNVERIFIED_FIXTURE)


@pytest.fixture
def conn(tmp_path):
    connection = Database.connect(tmp_path / "data")
    try:
        yield connection
    finally:
        connection.close()


def test_price_table_uses_documented_rows_only():
    rows = PriceBook.table()
    assert rows
    assert all(str(row["source_url"]).startswith("https://") for row in rows)
    assert all(row["fetched"] for row in rows)
    seed_rows = [row for row in rows if row["model_id"] == SEEDANCE]
    assert seed_rows
    for row in seed_rows:
        assert row["unit"] == PriceUnit.PER_MILLION_TOKENS
        assert row["currency"] == "CNY"
        assert row["resolution"] in {"480p", "720p", "1080p"}
        assert "effective_date" in row
        assert row["effective_date"] is None
        assert row["source_url"] == PRICE_DOC
        assert row["verified"] is True
        assert row["fetched"] == "2026-11-03"
    resolutions = {row["resolution"] for row in seed_rows}
    assert resolutions == {"480p", "720p", "1080p"}
    assert any("effective_date" in item for item in SEEDANCE_25_TODO)
    assert "wan2.7-image" in PriceBook.unknown_models()
    assert "edge-tts" in PriceBook.unknown_models()
    assert "deepseek-chat" not in PriceBook.unknown_models()
    assert "deepseek-flash" not in PriceBook.unknown_models()
    assert "qwen3.7-plus" not in PriceBook.unknown_models()
    assert SEEDANCE not in PriceBook.unknown_models()
    assert any(note.startswith("TODO: wan2.7-image") for note in PriceBook.price_todos())
    assert any(note.startswith("TODO: edge-tts") for note in PriceBook.price_todos())


def test_quote_returns_a_range_for_a_documented_price():
    video = quote(
        QuoteRequest(
            model_id=SEEDANCE,
            modality="video",
            resolution="720p",
            includes_video_input=False,
            completion_tokens=1_000_000,
        )
    )
    assert video["available"] is True
    assert video["unknown_models"] == []
    assert video["min"] == 70.0
    assert video["max"] == 70.0
    assert video["currency"] == "CNY"
    assert video["unit"] == PriceUnit.PER_MILLION_TOKENS
    assert video["source_url"] == PRICE_DOC
    assert video["resolution"] == "720p"

    half = quote(
        QuoteRequest(
            model_id=SEEDANCE,
            modality="video",
            resolution="1080p",
            includes_video_input=True,
            completion_tokens=500_000,
        )
    )
    assert half["min"] == half["max"] == 23.0

    missing_tokens = quote(
        QuoteRequest(
            model_id=SEEDANCE,
            modality="video",
            resolution="720p",
            includes_video_input=False,
        )
    )
    assert missing_tokens["available"] is False
    assert missing_tokens["unknown_models"] == []
    assert missing_tokens["min"] is None
    assert missing_tokens["max"] is None

    try:
        _documented_image(0.5)
        image = quote(
            QuoteRequest(model_id=IMAGE_FIXTURE, modality="image", asset_count=2)
        )
        assert image["available"] is True
        assert image["min"] == 1.0
        assert image["max"] == 6.0
        assert image["min"] == 2 * PriceBook.quote(IMAGE_FIXTURE, documented_only=True).amount
        assert image["max"] == 2 * 6 * 0.5
        one = quote(QuoteRequest(model_id=IMAGE_FIXTURE, modality="image"))
        assert one["min"] == 0.5
        assert one["max"] == 3.0
    finally:
        _remove_fixtures()


def test_unknown_price_is_not_a_number():
    try:
        _unverified_image()
        for model_id in ("wan2.7-image", "edge-tts", "deepseek-chat", UNVERIFIED_FIXTURE, "no-such-model"):
            priced = quote(QuoteRequest(model_id=model_id, modality="image", asset_count=1))
            assert priced["available"] is False
            assert priced["min"] is None
            assert priced["max"] is None
            assert priced["min"] != 0
            assert model_id in priced["unknown_models"]
        assert PriceBook.quote("wan2.7-image") is None
        refused = quote(QuoteRequest(model_id="wan2.7-image", modality="image", asset_count=1))
        assert refused["min"] is None
        assert UNVERIFIED_FIXTURE in PriceBook.unknown_models()
    finally:
        _remove_fixtures()


def test_over_budget_pauses_and_raise_continues(conn):
    project = ProjectStore.create(conn, "Budget", budget_cap=10)
    project_id = project["id"]
    before = ProjectBudget.budget_status(conn, project_id)
    assert before == {
        "used": 0.0,
        "committed": 0.0,
        "remaining": 10.0,
        "cap": 10.0,
        "currency": None,
        "mixed_currency": False,
    }

    paused = ProjectBudget.authorize(
        conn,
        project_id,
        QuoteRequest(
            model_id=SEEDANCE,
            modality="video",
            resolution="720p",
            includes_video_input=False,
            completion_tokens=1_000_000,
        ),
        node_id="clip1",
    )
    assert paused["submitted"] is False
    assert paused["allowed"] is False
    assert paused["confirmed"] is False
    assert paused["status"] == "awaiting_user"
    assert paused["this_quote"] == 70.0
    assert paused["committed"] == 0.0
    assert paused["used"] == 0.0
    assert "已用" in paused["message"]
    assert "本次报价" in paused["message"]
    assert "当前上限" in paused["message"]
    assert "70" in paused["message"]
    assert "10" in paused["message"]
    stored = LedgerStore.get(conn, paused["entry_id"])
    assert stored["quote_amount"] == 70.0
    assert stored["actual_amount"] is None
    assert stored["confirmed"] is False
    assert stored["currency"] == "CNY"
    node = conn.execute(
        "SELECT status FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, "clip1"),
    ).fetchone()
    assert node["status"] == "awaiting_user"
    assert ProjectStore.open(conn, project_id)["budget_cap"] == 10
    assert ProjectStore.open(conn, project_id)["spent"] is None

    still = ProjectBudget.raise_cap(conn, project_id, paused["entry_id"], 69)
    assert still["allowed"] is False
    assert still["submitted"] is False
    assert still["status"] == "awaiting_user"
    assert ProjectStore.open(conn, project_id)["budget_cap"] == 10

    continued = ProjectBudget.raise_cap(conn, project_id, paused["entry_id"], 70)
    assert continued["allowed"] is True
    assert continued["submitted"] is False
    assert continued["confirmed"] is True
    assert continued["status"] == "quoted"
    assert continued["committed"] == 70.0
    assert continued["used"] == 0.0
    assert continued["remaining"] == 0.0
    assert continued["cap"] == 70.0
    assert LedgerStore.get(conn, paused["entry_id"])["confirmed"] is True
    assert conn.execute(
        "SELECT status FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, "clip1"),
    ).fetchone()["status"] == "ready"

    settled = ProjectBudget.record_actual(conn, project_id, paused["entry_id"], 70)
    assert settled["actual"] == 70.0
    assert settled["submitted"] is False
    status = ProjectBudget.budget_status(conn, project_id)
    assert status["used"] == 70.0
    assert status["committed"] == 0.0
    assert status["remaining"] == 0.0
    assert status["currency"] == "CNY"
    assert ProjectStore.open(conn, project_id)["spent"] == 70.0
    assert LedgerStore.get(conn, paused["entry_id"])["status"] == "spent"


def test_decline_skips_and_does_not_reserve(conn):
    project = ProjectStore.create(conn, "Decline", budget_cap=10)
    project_id = project["id"]
    paused = ProjectBudget.authorize(
        conn,
        project_id,
        QuoteRequest(
            model_id=SEEDANCE,
            modality="video",
            resolution="480p",
            includes_video_input=True,
            completion_tokens=1_000_000,
        ),
        node_id="clip2",
    )
    assert paused["this_quote"] == 42.0
    assert paused["status"] == "awaiting_user"
    declined = ProjectBudget.decline(conn, project_id, paused["entry_id"])
    assert declined["allowed"] is False
    assert declined["submitted"] is False
    assert declined["confirmed"] is False
    assert declined["status"] == "skipped"
    assert declined["committed"] == 0.0
    assert ProjectBudget.budget_status(conn, project_id)["committed"] == 0.0
    assert conn.execute(
        "SELECT status FROM node_state WHERE project_id = ? AND node_id = ?",
        (project_id, "clip2"),
    ).fetchone()["status"] == "skipped"
    with pytest.raises(StorageError):
        ProjectBudget.record_actual(conn, project_id, paused["entry_id"], 42)


def test_under_budget_reserves_without_submitting(conn):
    project = ProjectStore.create(conn, "Room", budget_cap=100)
    project_id = project["id"]
    first = ProjectBudget.authorize(
        conn,
        project_id,
        QuoteRequest(
            model_id=SEEDANCE,
            modality="video",
            resolution="720p",
            includes_video_input=False,
            completion_tokens=1_000_000,
        ),
    )
    assert first["allowed"] is True
    assert first["submitted"] is False
    assert first["confirmed"] is False
    assert first["status"] == "quoted"
    assert first["committed"] == 70.0
    assert first["remaining"] == 30.0
    assert first["message"] == ""

    second = ProjectBudget.authorize(
        conn,
        project_id,
        QuoteRequest(
            model_id=SEEDANCE,
            modality="video",
            resolution="720p",
            includes_video_input=False,
            completion_tokens=1_000_000,
        ),
    )
    assert second["allowed"] is False
    assert second["submitted"] is False
    assert second["status"] == "awaiting_user"
    assert second["committed"] == 70.0
    assert "70" in second["message"]
    assert ProjectBudget.budget_status(conn, project_id)["committed"] == 70.0


def test_unknown_model_is_not_reserved(conn):
    project = ProjectStore.create(conn, "Unknown", budget_cap=100)
    paused = ProjectBudget.authorize(
        conn,
        project["id"],
        QuoteRequest(model_id="wan2.7-image", modality="image", asset_count=1),
    )
    assert paused["allowed"] is False
    assert paused["submitted"] is False
    assert paused["status"] == "unavailable"
    assert paused["this_quote"] is None
    assert paused["unknown_models"] == ["wan2.7-image"]
    assert paused["committed"] == 0.0
    stored = LedgerStore.get(conn, paused["entry_id"])
    assert stored["quote_amount"] is None
    assert stored["actual_amount"] is None
    assert "0.2" not in paused["message"]


def test_image_range_pauses_on_the_high_end(conn):
    project = ProjectStore.create(conn, "Images", budget_cap=1)
    try:
        _documented_image(0.5)
        paused = ProjectBudget.authorize(
            conn,
            project["id"],
            QuoteRequest(model_id=IMAGE_FIXTURE, modality="image", asset_count=1),
        )
        assert paused["quote"]["min"] == 0.5
        assert paused["quote"]["max"] == 3.0
        assert paused["this_quote"] == 3.0
        assert paused["status"] == "awaiting_user"
        assert paused["submitted"] is False
        assert "0.5~3" in paused["message"]
        continued = ProjectBudget.raise_cap(conn, project["id"], paused["entry_id"], 3)
        assert continued["allowed"] is True
        assert continued["submitted"] is False
        assert continued["committed"] == 3.0
        assert continued["confirmed"] is True
    finally:
        _remove_fixtures()
