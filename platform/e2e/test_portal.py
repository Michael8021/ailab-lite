"""What a user does in the Portal, in a real browser, against the real services.

These pin the properties that a build check cannot: the access token never reaches page scripts,
runs complete through the SSE timeline, downloads work, quarantined files are not downloadable.
"""

from __future__ import annotations

import re
import uuid

import pytest

from .conftest import TOKEN, login

pytestmark = pytest.mark.e2e

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _submit_task(page, prompt: str, model: str) -> str:
    page.wait_for_selector("select option", state="attached")
    page.select_option("select", model)
    page.fill("textarea", prompt)
    page.click("button:has-text('提交任务')")
    page.wait_for_url(re.compile(r"/runs/run_"), timeout=15000)
    return page.url.rsplit("/", 1)[-1]


def _wait_for_run_end(page, seconds: int = 90) -> None:
    page.wait_for_function(
        "() => document.body.innerText.includes('run.completed') || document.body.innerText.includes('run.failed')", timeout=seconds * 1000
    )


# ------------------------------------------------------------------------------------------ access


def test_anonymous_visitors_get_the_login_form_at_the_url_they_asked_for(page, stack) -> None:
    page.goto(stack.portal + "/files")
    page.wait_for_selector("input[type=password]")
    assert page.url.endswith("/files"), "a rewrite, not a redirect to another origin"


def test_a_wrong_token_is_refused_and_no_session_is_created(page, context, stack) -> None:
    page.goto(stack.portal)
    page.fill("input[type=password]", "not-the-token")
    page.click("button[type=submit]")
    page.wait_for_selector(".error")
    assert "令牌无效" in page.inner_text(".error")
    assert not [c for c in context.cookies() if c["name"] == "ailab_token"]


def test_login_returns_to_the_page_that_was_asked_for(page, stack) -> None:
    login(page, stack, path="/files")
    page.wait_for_selector("input[type=file]")
    assert page.url.endswith("/files")


def test_the_token_is_invisible_to_page_scripts_and_to_the_bundle(page, context, stack) -> None:
    scripts: list[str] = []
    page.on("response", lambda r: scripts.append(r.url) if r.url.endswith(".js") else None)
    login(page, stack)
    page.goto(stack.portal + "/files")
    page.wait_for_selector("input[type=file]")

    cookie = next(c for c in context.cookies() if c["name"] == "ailab_token")
    assert cookie["httpOnly"] is True and cookie["sameSite"] == "Strict"
    assert page.evaluate("document.cookie") == ""
    assert TOKEN not in page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    assert TOKEN not in page.content()
    assert scripts, "expected to have seen the JS chunks"
    for url in set(scripts):
        assert TOKEN not in context.request.get(url).text(), f"the token is in {url}"
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
    assert not [h for h in hrefs if "token" in h], "download links must not carry a token"


def test_the_proxy_exposes_only_v1_and_refuses_cross_site_writes(page, context, stack) -> None:
    login(page, stack)
    req = context.request
    assert req.get(stack.portal + "/api/internal/runs/x").status == 404  # /internal is not proxied at all
    assert req.get(stack.portal + "/api/v1/runs").status == 200
    hostile = req.post(stack.portal + "/api/v1/runs", headers={"Origin": "http://evil.example"}, data={"prompt": "x"})
    assert hostile.status == 403
    assert req.post(stack.portal + "/api/v1/runs", headers={"Origin": stack.portal}, data={"prompt": "x"}).status == 201
    anonymous = page.context.browser.new_context()
    try:
        assert anonymous.request.get(stack.portal + "/api/v1/runs").status == 401
    finally:
        anonymous.close()


def test_logout_ends_the_session(logged_in, stack) -> None:
    page = logged_in
    page.click("text=退出")
    page.wait_for_selector("input[type=password]")
    page.goto(stack.portal + "/")
    page.wait_for_selector("input[type=password]")


# ------------------------------------------------------------------------------------------ work


def test_the_workbench_offers_the_platforms_models_and_the_quota(logged_in) -> None:
    page = logged_in
    options = page.eval_on_selector_all("select option", "os => os.map(o => [o.value, o.textContent])")
    assert options == [["test-pro", "Test Pro（消耗 ×3）"], ["test-fast", "Test Fast"]]  # tests/models.test.toml, hidden ones excluded
    assert page.input_value("select") == "test-pro"
    page.wait_for_function("() => document.body.innerText.includes('今日额度已用')", timeout=15000)
    assert "deepseek" not in page.content().lower()


def test_a_task_runs_to_completion_and_its_timeline_streams_in(logged_in) -> None:
    page = logged_in
    _submit_task(page, "你好，在沙箱里执行一条命令", "test-fast")
    _wait_for_run_end(page)
    body = page.inner_text("body")
    assert "model: test-fast" in body
    for event in ("run.created", "agent.started", "agent.tool_call", "run.completed"):
        assert event in body, f"{event} missing from the timeline"
    assert page.locator(".status.succeeded").count() >= 1


def test_the_chosen_model_is_remembered(logged_in, stack) -> None:
    page = logged_in
    page.select_option("select", "test-fast")
    page.goto(stack.portal + "/")
    page.wait_for_selector("select option", state="attached")
    assert page.input_value("select") == "test-fast"


def test_a_deliverable_can_be_downloaded(logged_in) -> None:
    page = logged_in
    _submit_task(page, "总结这份资料，生成一个文件", "test-fast")
    _wait_for_run_end(page)
    page.wait_for_selector("text=产出")
    with page.expect_download() as download:
        page.click("a:has-text('下载')")
    content = open(download.value.path(), encoding="utf-8").read()
    assert download.value.suggested_filename == "answer.md"
    assert "处理结果" in content


# ------------------------------------------------------------------------------------------ files


def _row(page, filename: str):
    return page.locator("div", has_text=filename).filter(has=page.locator("a:has-text('删除')")).last


def test_an_upload_is_scanned_then_downloadable_then_deletable(logged_in, stack, tmp_path) -> None:
    page = logged_in
    page.goto(stack.portal + "/files")
    name = f"note-{uuid.uuid4().hex[:6]}.txt"
    path = tmp_path / name
    path.write_text("hello from the browser test\n", encoding="utf-8")
    page.set_input_files("input[type=file]", str(path))
    row = _row(page, name)
    row.wait_for(timeout=15000)
    row.locator(".status:has-text('ready')").wait_for(timeout=20000)  # quarantined (scanning) until the Worker is done
    with page.expect_download() as download:
        row.locator("a:has-text('下载')").click()
    assert open(download.value.path(), encoding="utf-8").read() == "hello from the browser test\n"

    page.on("dialog", lambda d: d.accept())
    row.locator("a:has-text('删除')").click()
    page.wait_for_function("(n) => !document.body.innerText.includes(n)", arg=name, timeout=15000)


def test_an_infected_upload_is_rejected_and_cannot_be_downloaded(logged_in, stack, tmp_path) -> None:
    page = logged_in
    page.goto(stack.portal + "/files")
    name = f"eicar-{uuid.uuid4().hex[:6]}.txt"
    path = tmp_path / name
    path.write_bytes(EICAR)
    page.set_input_files("input[type=file]", str(path))
    row = _row(page, name)
    row.wait_for(timeout=15000)
    row.locator(".status:has-text('已拒绝')").wait_for(timeout=20000)
    assert row.locator("a:has-text('下载')").count() == 0
    file_id = page.evaluate(
        """async (n) => { const r = await fetch('/api/v1/files'); const l = await r.json(); return l.find(f => f.filename === n).id }""",
        name,
    )
    assert page.request.get(f"{stack.portal}/api/v1/files/{file_id}/content").status == 409
    page.on("dialog", lambda d: d.accept())
    row.locator("a:has-text('删除')").click()
    page.wait_for_function("(n) => !document.body.innerText.includes(n)", arg=name, timeout=15000)
