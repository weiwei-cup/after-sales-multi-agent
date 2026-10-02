"""Keep tests independent from developer environment variables and dotenv files."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("AFTER_SALES_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
