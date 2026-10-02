from pathlib import Path

import pytest
from pydantic import ValidationError

from after_sales.config import Settings

pytestmark = pytest.mark.unit


def test_scripted_defaults_need_no_credentials():
    settings = Settings()
    assert settings.model_mode == "scripted"
    assert settings.model_api_key is None
    assert settings.max_model_calls == 20
    assert settings.max_tool_calls == 30
    assert settings.review_repair_limit == 2
    assert settings.max_concurrency == 2
    assert settings.token_budget == 50_000


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("AFTER_SALES_MAX_MODEL_CALLS", "7")
    monkeypatch.setenv("AFTER_SALES_BUSINESS_DB_PATH", "data/orders.sqlite")
    settings = Settings()
    assert settings.max_model_calls == 7
    assert settings.business_db_path == Path("data/orders.sqlite")


@pytest.mark.parametrize(
    "field", ["max_model_calls", "max_tool_calls", "max_concurrency", "token_budget"]
)
def test_positive_budgets(field):
    with pytest.raises(ValidationError):
        Settings(**{field: 0})


def test_repair_limit_allows_zero_but_rejects_negative():
    assert Settings(review_repair_limit=0).review_repair_limit == 0
    with pytest.raises(ValidationError):
        Settings(review_repair_limit=-1)
    with pytest.raises(ValidationError):
        Settings(review_repair_limit=3)


@pytest.mark.parametrize(
    ("values", "required"),
    [
        ({}, "MODEL_PROVIDER"),
        ({"model_provider": "example"}, "MODEL_NAME"),
        ({"model_provider": "example", "model_name": "example-model"}, "MODEL_API_KEY"),
    ],
)
def test_live_mode_requires_explicit_configuration(values, required):
    with pytest.raises(ValidationError, match=required):
        Settings(model_mode="live", **values)


def test_key_excluded_from_serialization():
    settings = Settings(model_api_key="test-placeholder-sensitive")
    assert "model_api_key" not in settings.model_dump()
    assert "test-placeholder-sensitive" not in settings.model_dump_json()
    assert "test-placeholder-sensitive" not in repr(settings)


def test_databases_cannot_share_a_file(tmp_path):
    with pytest.raises(ValidationError, match="different files"):
        Settings(business_db_path=tmp_path / "data.sqlite", checkpoint_db_path="./data.sqlite")


def test_database_path_cannot_be_a_directory(tmp_path):
    with pytest.raises(ValidationError, match="refer to files"):
        Settings(business_db_path=tmp_path)
