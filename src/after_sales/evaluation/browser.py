"""Optional actual browser probe for the UI case; all traffic stays on loopback."""

import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import parse_qs, urlparse


def check_refresh(root, settings, ticket_id, run_id, now, architecture):
    from playwright.sync_api import sync_playwright

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    requests = []
    with (root / "browser-server.log").open("w") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "after_sales.evaluation.worker",
                "serve",
                str(root),
                "--as-of",
                now.isoformat(),
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    with opener.open(url + "/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.05)
            else:
                raise TimeoutError("temporary browser server did not start")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.on(
                        "response",
                        lambda r: (
                            requests.append(
                                {
                                    "method": r.request.method,
                                    "path": r.url.removeprefix(url),
                                    "status": r.status,
                                }
                            )
                            if r.url.startswith(url)
                            else None
                        ),
                    )
                    page.goto(url)
                    page.locator("#identity").select_option("demo-operator")
                    page.locator(f'[data-ticket="{ticket_id}"]').click()
                    page.get_by_text("已有", exact=False).first.wait_for(timeout=15000)
                    page.reload()
                    page.get_by_text(ticket_id, exact=False).first.wait_for(timeout=15000)
                    # Abort one nonzero cursor request, then require a successful retry with it.
                    seen = []

                    def disconnect(route):
                        cursor = int(parse_qs(urlparse(route.request.url).query)["after_seq"][0])
                        if cursor > 0 and not seen:
                            seen.append((route.request.url, len(requests)))
                            requests.append(
                                {
                                    "method": "GET",
                                    "path": route.request.url.removeprefix(url),
                                    "status": 0,
                                    "operation": "injected_disconnect",
                                    "after_seq": cursor,
                                }
                            )
                            route.abort()
                        else:
                            route.continue_()

                    page.route(f"**/runs/{run_id}/events?after_seq=**", disconnect)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not seen:
                        page.wait_for_timeout(100)
                    if not seen:
                        raise RuntimeError("page did not poll the selected run after reload")
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if any(
                            r["status"] == 200 and r["path"] == seen[0][0].removeprefix(url)
                            for r in requests[seen[0][1] :]
                        ):
                            break
                        page.wait_for_timeout(100)
                    else:
                        raise RuntimeError("event reconnect did not retry the same cursor")
                    if page.get_by_text("退货已完成", exact=True).count():
                        raise RuntimeError("existing return was incorrectly displayed as fulfilled")
                    labels = page.locator("#roles").inner_text()
                    if architecture == "single" and (
                        "调查 Agent" not in labels or "订单专员" in labels or "审核员" in labels
                    ):
                        raise RuntimeError("single run displayed incorrect Agent roles")
                    if architecture == "multi" and (
                        "订单专员" not in labels or "调查 Agent" in labels
                    ):
                        raise RuntimeError("multi run displayed incorrect Agent roles")
                    page.screenshot(path=str(root / "browser-refresh.png"), full_page=True)
                finally:
                    browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    return requests
