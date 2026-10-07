"""Real Chromium -> HTTP -> durable graph -> SQLite, with isolated demo fixtures."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from after_sales.repositories.seed import seed_demo
from after_sales.repositories.sqlite import read_database

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def browser():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".tools/playwright-browsers"))
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def server(tmp_path, request):
    db = tmp_path / "business.sqlite"
    seed_demo(db)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    environment = {
        **os.environ,
        "AFTER_SALES_MODEL_MODE": "scripted",
        "AFTER_SALES_BUSINESS_DB_PATH": str(db),
        "AFTER_SALES_CHECKPOINT_DB_PATH": str(tmp_path / "checkpoints.sqlite"),
    }
    if getattr(request, "param", None) == "interrupt":
        environment["P09_TEST_INTERRUPT_ONCE"] = "1"
    log_path = tmp_path / "server.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tests/support/web_server.py"), str(port)],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=log,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=1) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        pytest.fail("server readiness failed: " + log_path.read_text())
                    time.sleep(0.05)
            yield {"url": url, "db": db, "log": log_path}
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def artifact_dir(tmp_path, request):
    destination = Path(os.environ.get("P09_ARTIFACT_DIR", str(tmp_path / "browser"))).resolve()
    directory = destination / request.node.name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def page(browser, server, artifact_dir):
    directory = artifact_dir
    context = browser.new_context(viewport={"width": 1440, "height": 1050}, locale="zh-CN")
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    # No browser-originated network outside this test's loopback server is allowed.
    context.route(
        "**/*",
        lambda route: (
            route.continue_()
            if urlparse(route.request.url).netloc == urlparse(server["url"]).netloc
            else route.abort()
        ),
    )
    page = context.new_page()
    page.set_default_timeout(15000)
    failures, network = [], []
    page.on("pageerror", lambda error: failures.append(str(error)))
    page.on(
        "response",
        lambda response: network.append(
            {
                "method": response.request.method,
                "path": urlparse(response.url).path,
                "query": urlparse(response.url).query,
                "status": response.status,
                "request_id": response.headers.get("x-request-id"),
            }
        ),
    )
    page.goto(server["url"])
    expect(page.locator("#connection")).to_contain_text("服务已连接")
    yield page
    page.screenshot(path=str(directory / "page.png"), full_page=True)
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": "p09-browser-observation-v1",
                "browser": browser.version,
                "viewport": page.viewport_size,
                "model_mode": "scripted",
                "fixture_business_clock": "2026-10-02T04:00:00Z",
                "temporary_database": True,
                "ticket_id": page.locator("#ticket-id").text_content(),
                "business_status": page.locator("#business-status").text_content(),
                "run_status": page.locator("#run-status").text_content(),
                "receipts": page.locator("#receipts").inner_text(),
                "evidence": page.locator("#evidence-count").text_content(),
                "statistics": page.locator("#stats").inner_text(),
                "events": page.locator("#event-count").text_content(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    (directory / "http.json").write_text(json.dumps(network, ensure_ascii=False, indent=2) + "\n")
    context.tracing.stop(path=str(directory / "trace.zip"))
    context.close()
    assert not failures, failures


def choose(page, ticket, actor="demo-customer-a"):
    page.locator("#identity").select_option(actor)
    page.locator(f'[data-ticket="{ticket}"]').click()
    expect(page.locator("#ticket-id")).to_have_text(ticket)


def approve(page):
    page.locator("#identity").select_option("demo-operator")
    expect(page.get_by_role("form", name="操作员确认表单")).to_be_visible()
    page.locator('input[name="read_confirmation"]').check()
    page.get_by_role("button", name="提交处理决定").click()
    expect(page.locator("#run-status")).to_have_text("运行结束")


def ledger_count(server):
    with read_database(server["db"]) as db:
        return db.execute("SELECT count(*) FROM action_ledger").fetchone()[0]


@pytest.mark.parametrize(
    "ticket,target,receipt",
    [
        ("T-DELAY-002", "处理中", "登记物流调查"),
        ("T-RETURN-001", "待客户寄回", "登记退货申请"),
    ],
)
def test_core_logistics_and_return(page, server, artifact_dir, ticket, target, receipt):
    choose(page, ticket, "demo-customer-b")
    page.get_by_role("button", name="开始处理", exact=True).click()
    expect(page.locator("#business-status")).to_have_text("待人工确认")
    expect(page.locator("#pending-card")).to_contain_text("依据政策")
    expect(page.locator("#evidence-count")).to_contain_text("快照")
    page.locator("#identity").select_option("demo-operator")
    expect(page.get_by_role("form", name="操作员确认表单")).to_be_visible()
    page.screenshot(path=str(artifact_dir / "pending-desktop.png"), full_page=True)
    approve(page)
    expect(page.locator("#business-status")).to_have_text(target)
    expect(page.locator("#receipts")).to_contain_text(receipt)
    if target == "待客户寄回":
        expect(page.locator("#status-note")).to_contain_text("尚未履约")
    assert ledger_count(server) == 1


def test_core_not_received_create_supplement_refresh_and_refund(page, server):
    page.get_by_role("button", name="新建工单").click()
    page.locator('#create-form select[name="type"]').select_option("delivered_not_received")
    page.locator('textarea[name="message"]').fill("物流显示签收，但我没有收到，请帮我核实。")
    page.get_by_role("button", name="创建工单", exact=True).click()
    page.get_by_role("button", name="开始处理", exact=True).click()
    expect(page.get_by_role("form", name="客户补充表单")).to_be_visible()
    page.reload()
    expect(page.get_by_role("form", name="客户补充表单")).to_be_visible()
    page.locator('#pending-card input[name="order_id"]').fill("ORD-004")
    page.get_by_role("button", name="提交补充资料").click()
    expect(page.locator("#business-status")).to_have_text("待人工确认")
    expect(page.locator("#pending-card")).to_contain_text("¥100.00")
    approve(page)
    expect(page.locator("#business-status")).to_have_text("已办结")
    expect(page.locator("#receipts")).to_contain_text("¥100.00")
    assert ledger_count(server) == 1


def test_double_click_start_and_approve(page, server):
    choose(page, "T-NOTRECEIVED-002")
    page.get_by_role("button", name="开始处理", exact=True).dblclick()
    expect(page.locator("#business-status")).to_have_text("待人工确认")
    page.locator("#identity").select_option("demo-operator")
    page.locator('input[name="read_confirmation"]').check()
    page.get_by_role("button", name="提交处理决定").dblclick()
    expect(page.locator("#run-status")).to_have_text("运行结束")
    with read_database(server["db"]) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM execution_jobs").fetchone()[0] == 2
    assert ledger_count(server) == 1


def test_lost_start_ack_reload_reuses_key(page, server):
    choose(page, "T-NOTRECEIVED-002")
    keys = []

    def lose_ack(route):
        keys.append(route.request.headers["idempotency-key"])
        response = route.fetch()
        assert response.status == 202
        route.abort("failed")

    page.route("**/tickets/*/runs", lose_ack)
    page.get_by_role("button", name="开始处理", exact=True).click()
    expect(page.locator("#notice-text")).to_contain_text("连接暂时中断")
    page.unroute("**/tickets/*/runs", lose_ack)
    page.reload()
    expect(page.locator("#notice-text")).to_contain_text("上次提交结果尚未确认")
    page.on(
        "request",
        lambda request: (
            keys.append(request.headers["idempotency-key"]) if request.method == "POST" else None
        ),
    )
    page.get_by_role("button", name="重试原请求").click()
    expect(page.locator("#business-status")).to_have_text("待人工确认")
    assert len(keys) == 2 and keys[0] == keys[1]
    with read_database(server["db"]) as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_lost_approval_ack_replay_keeps_single_refund(page, server):
    choose(page, "T-NOTRECEIVED-002")
    page.get_by_role("button", name="开始处理", exact=True).click()
    page.locator("#identity").select_option("demo-operator")
    page.locator('input[name="read_confirmation"]').check()
    keys = []

    def lose_ack(route):
        keys.append(route.request.headers["idempotency-key"])
        assert route.fetch().status == 202
        route.abort("failed")

    page.route("**/responses", lose_ack)
    page.get_by_role("button", name="提交处理决定").click()
    expect(page.locator("#notice-text")).to_contain_text("连接暂时中断")
    expect(page.locator("#run-status")).to_have_text("运行结束")
    page.unroute("**/responses", lose_ack)
    page.on(
        "request",
        lambda request: (
            keys.append(request.headers["idempotency-key"]) if request.method == "POST" else None
        ),
    )
    page.get_by_role("button", name="重试原请求").click()
    expect(page.locator("#notice-text")).to_contain_text("请求已受理")
    assert len(keys) == 2 and keys[0] == keys[1]
    assert ledger_count(server) == 1
    with read_database(server["db"]) as db:
        assert (
            db.execute("SELECT refunded_cents FROM orders WHERE id='ORD-004'").fetchone()[0]
            == 10000
        )


def test_ui_refund_revision_validates_decimal_and_requires_new_confirmation(page, server):
    choose(page, "T-NOTRECEIVED-002")
    page.get_by_role("button", name="开始处理", exact=True).click()
    page.locator("#identity").select_option("demo-operator")
    page.locator('#pending-card select[name="decision"]').select_option("revise")
    amount = page.get_by_label("调整后的退款金额（元）")
    checkbox = page.locator('input[name="read_confirmation"]')
    amount.fill("90.001")
    checkbox.check()
    page.get_by_role("button", name="提交处理决定").click()
    expect(page.locator("#pending-card .form-error")).to_contain_text("最多两位小数")
    assert ledger_count(server) == 0
    amount.fill("90.00")
    page.get_by_role("button", name="提交处理决定").click()
    expect(page.locator("#pending-card .action-detail")).to_contain_text("¥90.00")
    expect(checkbox).not_to_be_checked()
    checkbox.check()
    page.get_by_role("button", name="提交处理决定").click()
    expect(page.locator("#receipts")).to_contain_text("¥90.00")
    assert ledger_count(server) == 1


def test_event_reconnect_keeps_cursor_and_no_duplicates(page, server):
    choose(page, "T-NOTRECEIVED-002")
    page.get_by_role("button", name="开始处理", exact=True).click()
    expect(page.locator("#event-count")).to_contain_text("已同步到")
    cursors = []

    def disconnect(route):
        cursors.append(int(route.request.url.split("after_seq=")[1].split("&")[0]))
        route.abort("failed")

    page.route("**/events?*", disconnect)
    expect(page.locator("#connection")).to_contain_text("连接中断")
    assert cursors and cursors[0] > 0
    page.unroute("**/events?*", disconnect)
    after = []
    page.on(
        "request",
        lambda request: (
            after.append(int(request.url.split("after_seq=")[1].split("&")[0]))
            if "/events?" in request.url
            else None
        ),
    )
    page.get_by_role("button", name="重试原请求").click()
    expect(page.locator("#connection")).to_contain_text("服务已连接")
    assert after and after[0] == cursors[-1]
    page.locator("#all-events").click()
    sequences = page.locator("#timeline li").evaluate_all(
        "items => items.map(item => Number(item.dataset.sequence))"
    )
    assert sequences == sorted(set(sequences))


def test_stale_confirmation_requires_reading_new_proposal(page, server):
    choose(page, "T-NOTRECEIVED-002")
    page.get_by_role("button", name="开始处理", exact=True).click()
    page.locator("#identity").select_option("demo-operator")
    checkbox = page.locator('input[name="read_confirmation"]')
    checkbox.check()
    with httpx.Client(base_url=server["url"], trust_env=False) as client:
        headers = {"Authorization": "Bearer demo-operator"}
        ticket = client.get("/tickets/T-NOTRECEIVED-002", headers=headers).json()
        run = ticket["latest_run"]
        pending = run["pending_input"]
        # Keep the real old browser form visible while another operator revises the real run.
        page.route("**/tickets/T-NOTRECEIVED-002", lambda route: route.fulfill(json=ticket))
        response = client.post(
            f"/runs/{run['run_id']}/responses",
            headers={**headers, "Idempotency-Key": "other-operator-revise"},
            json={
                **{
                    k: pending[k]
                    for k in ("pending_id", "input_revision", "proposal_revision", "proposal_hash")
                },
                "action_hashes": {a["action_id"]: a["content_hash"] for a in pending["actions"]},
                "decision": "revise",
                "refund_amounts": {pending["actions"][0]["action_id"]: 9000},
            },
        )
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = client.get(f"/runs/{run['run_id']}", headers=headers).json()
            if (
                current["pending_input"]
                and current["pending_input"]["pending_id"] != pending["pending_id"]
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail("new proposal did not become ready")
    # Old submit must really hit the server and be rejected with 409.
    with page.expect_response(
        lambda response: response.request.method == "POST" and "/responses" in response.url
    ) as rejected:
        page.get_by_role("button", name="提交处理决定").click()
    assert rejected.value.status == 409
    expect(page.locator("#notice-text")).to_contain_text("方案或任务状态已变化")
    page.unroute("**/tickets/T-NOTRECEIVED-002")
    expect(page.locator("#pending-card")).to_contain_text("¥90.00")
    expect(checkbox).not_to_be_checked()
    assert ledger_count(server) == 0
    checkbox.check()
    page.get_by_role("button", name="提交处理决定").click()
    expect(page.locator("#receipts")).to_contain_text("¥90.00")
    assert ledger_count(server) == 1


@pytest.mark.parametrize("server", ["interrupt"], indirect=True)
def test_resume_interrupted_execution_then_cancel_pending(page, server):
    choose(page, "T-NOTRECEIVED-002")
    page.get_by_role("button", name="开始处理", exact=True).click()
    expect(page.locator("#run-status")).to_have_text("执行中断")
    page.get_by_role("button", name="恢复处理").click()
    expect(page.locator("#business-status")).to_have_text("待人工确认")
    page.get_by_role("button", name="取消运行").click()
    expect(page.locator("#run-status")).to_have_text("已取消")
    expect(page.locator("#pending-card")).to_be_hidden()
    assert ledger_count(server) == 0


def test_identity_scope_search_empty_and_safe_text(page, server):
    expect(page.locator('[data-ticket="T-RETURN-001"]')).to_have_count(0)
    page.locator("#search").fill("不存在的工单")
    expect(page.locator("#ticket-list")).to_contain_text("暂无匹配工单")
    page.locator("#search").fill("")
    page.get_by_role("button", name="新建工单").click()
    malicious = '<img src=x onerror="window.p09Injected=true"> 联系13800138000和hello@example.com'
    page.locator('textarea[name="message"]').fill(malicious)
    page.get_by_role("button", name="创建工单", exact=True).click()
    expect(page.locator("#messages")).to_contain_text("<img")
    expect(page.locator("#messages")).not_to_contain_text("13800138000")
    assert page.locator("#messages img").count() == 0
    assert page.evaluate("window.p09Injected === undefined")
    page.locator("#identity").select_option("demo-customer-b")
    expect(page.locator("#ticket-content")).to_be_hidden()
    expect(page.locator('[data-ticket="T-NOTRECEIVED-002"]')).to_have_count(0)


def test_narrow_layout_form_and_timeline(page, server, artifact_dir):
    page.set_viewport_size({"width": 390, "height": 844})
    choose(page, "T-NOTRECEIVED-002")
    page.get_by_role("button", name="开始处理", exact=True).click()
    page.locator("#identity").select_option("demo-operator")
    expect(page.get_by_role("form", name="操作员确认表单")).to_be_visible()
    page.screenshot(path=str(artifact_dir / "pending-narrow.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for selector in ("#pending-card", "#timeline", "#identity", "#controls"):
        bounds = page.locator(selector).bounding_box()
        assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
    page.locator('input[name="read_confirmation"]').check()
    page.get_by_role("button", name="提交处理决定").click()
    expect(page.locator("#receipts")).to_contain_text("登记模拟退款")
