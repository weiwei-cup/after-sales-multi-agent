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
    max_model_calls: int = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=30, gt=0)
    review_repair_limit: int = Field(default=2, ge=0)
    max_concurrency: int = Field(default=2, gt=0)
    token_budget: int = Field(default=50_000, gt=0)
    tool_timeout_seconds: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    tool_max_result_bytes: int = Field(default=12_000, ge=512)

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
