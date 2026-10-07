"""Validated runtime configuration. No model clients are created here."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AFTER_SALES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    model_mode: Literal["scripted", "live"] = "scripted"
    model_provider: str | None = None
    model_name: str | None = None
    model_api_key: SecretStr | None = Field(default=None, exclude=True)
    business_db_path: Path = Path("var/business.sqlite")
    checkpoint_db_path: Path = Path("var/checkpoints.sqlite")
    api_workers: int = Field(default=1, ge=1, le=4)
    api_queue_capacity: int = Field(default=32, ge=1, le=1000)
    max_model_calls: int = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=30, gt=0)
    review_repair_limit: int = Field(default=2, ge=0, le=2)
    max_concurrency: int = Field(default=2, gt=0)
    token_budget: int = Field(default=50_000, gt=0)
    model_token_reservation: int = Field(default=2_048, gt=0)
    active_time_budget_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    transient_retry_limit: int = Field(default=1, ge=0, le=3)
    retry_backoff_seconds: float = Field(default=0.05, ge=0, le=5, allow_inf_nan=False)
    tool_timeout_seconds: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    tool_max_result_bytes: int = Field(default=12_000, ge=512)
    proposal_repair_limit: int = Field(default=1, ge=0, le=3)
    model_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_database_paths(self) -> Self:
        paths = (self.business_db_path, self.checkpoint_db_path)
        if any(path == Path(".") or path.is_dir() for path in paths):
            raise ValueError("database paths must refer to files")
        if self.business_db_path.resolve() == self.checkpoint_db_path.resolve():
            raise ValueError("business and checkpoint databases must use different files")
        return self

    @model_validator(mode="after")
    def validate_live_configuration(self) -> Self:
        if self.model_mode == "live":
            if not self.model_provider or not self.model_provider.strip():
                raise ValueError("live mode requires AFTER_SALES_MODEL_PROVIDER")
            if not self.model_name or not self.model_name.strip():
                raise ValueError("live mode requires AFTER_SALES_MODEL_NAME")
            if not self.model_api_key or not self.model_api_key.get_secret_value().strip():
                raise ValueError("live mode requires AFTER_SALES_MODEL_API_KEY")
        return self
