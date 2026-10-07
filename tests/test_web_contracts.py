"""Public summaries and packaged UI retain the HTTP scope/privacy boundaries."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from after_sales.api.app import create_app
from after_sales.api.contracts import ResultView
from after_sales.config import Settings
from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import BusinessRepository
from after_sales.services.application import ApplicationService
from after_sales.services.public import evidence_view

pytestmark = pytest.mark.api


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        business_db_path=tmp_path / "business.sqlite",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
    )
    seed_demo(settings.business_db_path)
    service = ApplicationService(
        BusinessRepository(settings.business_db_path),
        settings,
        clock=lambda: datetime(2026, 10, 2, 4, tzinfo=UTC),
    )
    with TestClient(create_app(settings, service=service)) as client:
        yield client


def test_ui_assets_and_security_headers_do_not_shadow_api(client):
    response = client.get("/")
    assert response.status_code == 200 and '<html lang="zh-CN">' in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-cache"
    for name in ("workbench.css", "workbench.js"):
        assert client.get("/assets/" + name).status_code == 200
    assert client.get("/tickets").status_code == 401
    assert client.get("/assets/../config.py").status_code == 404
    tickets = client.get("/tickets", headers={"Authorization": "Bearer demo-customer-a"}).json()
    assert all(t["last_activity_at"].endswith("Z") for t in tickets)


def test_evidence_projection_allowlists_facts_and_redacts_metadata():
    item = {
        "id": "E-test",
        "source_type": "order",
        "source_id": "ORD-004",
        "source_version": "1",
        "observed_at": "2026-10-02T04:00:00Z",
        "facts": {"status": "lost", "customer_id": "CUST-A", "private": "secret"},
        "session_id": "private-session",
        "ticket_id": "private-ticket",
    }
    public = evidence_view(item)
    assert public["summary"] == "订单状态：已确认丢失"
    assert set(public) == {
        "evidence_id",
        "source_type",
        "source_id",
        "source_version",
        "observed_at",
        "summary",
    }
    assert "private" not in str(public) and "secret" not in str(public)
    item.update(source_type="policy", facts={"title": "联系13800138000或hello@example.com"})
    public = evidence_view(item)
    assert "13800138000" not in public["summary"] and "hello@example.com" not in public["summary"]


def test_p08_persisted_result_still_validates_with_optional_ui_summaries():
    result = ResultView.model_validate(
        {
            "outcome": "completed",
            "decision": "inform_progress",
            "customer_reply": "已发货",
            "receipts": [],
            "model_calls": 10,
            "tool_calls": 8,
            "error_code": None,
        }
    )
    assert result.evidence == [] and result.proposal_revision is None
