"""What a user does in the Portal, in a real browser, against the real services.

These pin the properties that a build check cannot: the session token never reaches page scripts,
runs complete through the SSE timeline, downloads work, quarantined files are not downloadable.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from .conftest import PASSWORD, PLATFORM, TOKEN, USERNAME, login

pytestmark = pytest.mark.e2e

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _send(page, prompt: str, model: str | None = None) -> None:
    page.wait_for_selector("select option", state="attached")
    if model:
        page.select_option("select", model)
    page.fill("textarea", prompt)
    page.keyboard.press("Enter")


def _start_conversation(page, prompt: str, model: str = "test-fast") -> str:
    _send(page, prompt, model)
    page.wait_for_url(re.compile(r"/c/cnv_"), timeout=15000)
    return page.url.rsplit("/", 1)[-1]


def _open_all_steps(page) -> None:
    """Expand every group of steps (a group still open from streaming must not be toggled shut)."""
    closed = page.locator(".steps-head[aria-expanded='false']")
    while closed.count():  # re-query each time: an opened head no longer matches
        closed.first.click()


def _wait_for_turns(page, count: int, seconds: int = 90) -> None:
    """Until `count` turns have finished (each finished turn shows its meta line)."""
    page.wait_for_function("(n) => document.querySelectorAll('.turn-meta').length >= n", arg=count, timeout=seconds * 1000)


# ------------------------------------------------------------------------------------------ access


def test_anonymous_visitors_get_the_login_form_at_the_url_they_asked_for(page, stack) -> None:
    page.goto(stack.portal + "/files")
    page.wait_for_selector("input[type=password]")
    assert page.url.endswith("/files"), "a rewrite, not a redirect to another origin"


def test_a_wrong_password_is_refused_and_no_session_is_created(page, context, stack) -> None:
    page.goto(stack.portal)
    page.fill("input[name=username]", USERNAME)
    page.fill("input[type=password]", "not-the-password")
    page.click("button[type=submit]")
    page.wait_for_selector(".error")
    assert "用户名或密码错误" in page.inner_text(".error")
    assert not [c for c in context.cookies() if c["name"] == "ailab_token"]


def test_the_static_api_token_is_not_a_password(page, context, stack) -> None:
    page.goto(stack.portal)
    page.fill("input[name=username]", USERNAME)
    page.fill("input[type=password]", TOKEN)
    page.click("button[type=submit]")
    page.wait_for_selector(".error")
    assert not [c for c in context.cookies() if c["name"] == "ailab_token"]


def test_login_returns_to_the_page_that_was_asked_for(page, stack) -> None:
    login(page, stack, path="/files")
    page.wait_for_selector("input[type=file]")
    assert page.url.endswith("/files")


def test_the_session_token_and_password_are_invisible_to_page_scripts_and_to_the_bundle(page, context, stack) -> None:
    scripts: list[str] = []
    page.on("response", lambda r: scripts.append(r.url) if r.url.endswith(".js") else None)
    login(page, stack)
    page.goto(stack.portal + "/files")
    page.wait_for_selector("input[type=file]", state="attached")

    cookie = next(c for c in context.cookies() if c["name"] == "ailab_token")
    token = cookie["value"]
    assert cookie["httpOnly"] is True and cookie["sameSite"] == "Strict"
    assert page.evaluate("document.cookie") == ""
    storage = page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    assert token not in storage and PASSWORD not in storage
    assert token not in page.content() and PASSWORD not in page.content()
    assert scripts, "expected to have seen the JS chunks"
    for url in set(scripts):
        body = context.request.get(url).text()
        assert token not in body and PASSWORD not in body, f"a secret is in {url}"
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


def test_logout_ends_the_session_on_the_server_too(logged_in, context, stack) -> None:
    page = logged_in
    token = next(c for c in context.cookies() if c["name"] == "ailab_token")["value"]
    page.click(".user-chip")  # the account menu at the bottom of the sidebar
    page.click("text=退出登录")
    page.wait_for_selector("input[type=password]")
    page.goto(stack.portal + "/")
    page.wait_for_selector("input[type=password]")
    # a copy of the cookie made before logging out is worthless now
    assert httpx.get(f"{stack.api}/v1/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_the_signed_in_user_is_shown_and_can_open_the_account_page(logged_in, stack) -> None:
    page = logged_in
    page.wait_for_selector(".user-chip:has-text('Administrator')")  # the bootstrap account's display name
    page.click(".user-chip")
    page.click("text=账号设置")
    page.wait_for_selector("text=修改密码")
    page.wait_for_function("(u) => document.querySelector('main').innerText.includes(u)", arg=USERNAME)


def test_changing_the_password_signs_everyone_out_and_the_new_one_works(browser, stack) -> None:
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        name, old, new = f"pw{uuid.uuid4().hex[:6]}", "old-password-1", "new-password-2"
        # a second account, so the shared one used by other tests keeps its password
        created = subprocess.run(
            [sys.executable, "-m", "ailab_platform.users", "add", name, "--password-stdin"],
            input=old + "\n",
            text=True,
            capture_output=True,
            cwd=PLATFORM,
            env=stack.env,
        )
        assert created.returncode == 0, created.stderr
        login(page, stack, name, old)
        page.goto(stack.portal + "/account")
        page.fill("input[autocomplete=current-password]", old)
        page.locator("input[autocomplete=new-password]").nth(0).fill(new)
        page.locator("input[autocomplete=new-password]").nth(1).fill(new)
        page.click("button:has-text('修改密码')")
        page.wait_for_selector("input[name=username]")  # back at the login page
        page.fill("input[name=username]", name)  # the old password no longer works
        page.fill("input[type=password]", old)
        page.click("button[type=submit]")
        page.wait_for_selector(".error")
        login(page, stack, name, new)
        page.wait_for_selector("select option", state="attached", timeout=15000)
    finally:
        ctx.close()


# ------------------------------------------------------------------------------------------ work


def test_the_workbench_offers_the_platforms_models_and_the_quota(logged_in) -> None:
    page = logged_in
    options = page.eval_on_selector_all("select option", "os => os.map(o => [o.value, o.textContent])")
    assert options == [["test-pro", "Test Pro（消耗 ×3）"], ["test-fast", "Test Fast"]]  # tests/models.test.toml, hidden ones excluded
    assert page.input_value("select") == "test-pro"
    page.click(".user-chip")  # usage lives in the account menu
    page.wait_for_function("() => document.body.innerText.includes('今日额度已用')", timeout=15000)
    assert "deepseek" not in page.content().lower()


def test_a_plain_question_is_answered_without_a_sandbox(logged_in, stack) -> None:
    page = logged_in
    conv = _start_conversation(page, "你好，介绍一下你自己")
    _wait_for_turns(page, 1)
    answer = page.inner_text(".msg.assistant .md")
    assert "离线演示模型" in answer and "本对话第 1 轮" in answer
    assert page.locator(".msg.assistant strong").count() >= 1  # markdown rendered, not shown as **raw**
    assert page.locator(".steps").count() == 0  # no tools, no sandbox
    page.wait_for_selector(f".conv-item.on a[href='/c/{conv}']")


def test_a_conversation_continues_with_its_history(logged_in) -> None:
    page = logged_in
    _start_conversation(page, "第一个问题")
    _wait_for_turns(page, 1)
    _send(page, "第二个问题")
    _wait_for_turns(page, 2)
    page.wait_for_function("() => document.querySelectorAll('.msg.assistant .md').length >= 2", timeout=15000)
    answers = page.locator(".msg.assistant .md").all_inner_texts()
    assert "本对话第 2 轮" in answers[-1]
    assert page.locator(".msg.user .bubble").all_inner_texts() == ["第一个问题", "第二个问题"]
    page.reload()  # history comes back from the server
    page.wait_for_selector(".turn-meta")
    assert page.locator(".msg.user .bubble").count() == 2


def test_a_task_runs_to_completion_and_its_timeline_streams_in(logged_in) -> None:
    page = logged_in
    _start_conversation(page, "你好，在沙箱里执行一条命令")
    _wait_for_turns(page, 1)
    _open_all_steps(page)
    assert "运行命令" in page.inner_text(".steps")
    with page.expect_navigation():
        page.click(".turn-meta a:has-text('详情')")
    page.wait_for_function("() => document.body.innerText.includes('run.completed')", timeout=30000)
    page.wait_for_selector(".chip:has-text('model: test-fast')")  # the run itself loads separately from its events
    body = page.inner_text("body")
    assert "model: test-fast" in body  # the chip next to the title
    for event in ("run.created", "agent.started", "agent.tool_call", "run.completed"):
        assert event in body, f"{event} missing from the timeline"


def test_a_spreadsheet_is_previewed_live_and_can_be_revised(logged_in) -> None:
    page = logged_in
    _start_conversation(page, "做一个 Excel 表格")
    page.wait_for_selector(".preview-pane .sheet-grid td", timeout=60000)  # appears while the turn is still running
    _wait_for_turns(page, 1)
    grid = page.inner_text(".preview-pane .sheet-grid")
    assert "华东" in grid and "合计" in grid
    assert page.inner_text(".preview-tab.on") == "sales.xlsx"
    page.click(".preview-pane button[aria-label='关闭预览']")
    assert page.locator(".preview-pane").count() == 0
    page.click(".file-card button:has-text('预览')")
    page.wait_for_selector(".preview-pane .sheet-grid")

    _send(page, "表格再改一下")  # the second turn rebuilds the same file from the restored workspace
    _wait_for_turns(page, 2)
    assert page.locator(".file-card").count() == 2  # one delivery per turn, same file name


def test_a_deck_that_cannot_be_previewed_says_so(logged_in) -> None:
    page = logged_in
    _start_conversation(page, "帮我做一个 PPT")
    _wait_for_turns(page, 1, seconds=120)
    # the render runs in the background, so its note may land in a later group of steps: open them all
    _open_all_steps(page)
    steps = "\n".join(page.locator(".steps").all_inner_texts())
    assert "读取技能 pptx" in steps and "交付文件" in steps
    # the browser tests run the mock sandbox on the host, which has no LibreOffice to render slides
    assert "预览生成失败" in steps or page.locator(".preview-pane img").count() > 0
    assert "ailab-demo.pptx" in page.inner_text(".file-card")


def test_the_chosen_model_is_remembered(logged_in, stack) -> None:
    page = logged_in
    page.wait_for_selector("select option", state="attached")
    page.select_option("select", "test-fast")
    page.goto(stack.portal + "/")
    page.wait_for_selector("select option", state="attached")
    assert page.input_value("select") == "test-fast"


def test_a_deliverable_can_be_downloaded(logged_in) -> None:
    page = logged_in
    _start_conversation(page, "总结这份资料，生成一个文件")
    _wait_for_turns(page, 1)
    with page.expect_download() as download:
        page.click(".file-card a:has-text('下载')")
    content = open(download.value.path(), encoding="utf-8").read()
    assert download.value.suggested_filename == "answer.md"
    assert "处理结果" in content


def test_a_conversation_can_be_deleted(logged_in, stack) -> None:
    page = logged_in
    conv = _start_conversation(page, "要删掉的对话")
    _wait_for_turns(page, 1)
    page.on("dialog", lambda d: d.accept())
    page.hover(f".conv-item:has(a[href='/c/{conv}'])")
    page.click(f".conv-item:has(a[href='/c/{conv}']) .conv-del")
    page.wait_for_url(stack.portal + "/")
    page.wait_for_selector(".chat-home")
    assert page.locator(f".conv-item a[href='/c/{conv}']").count() == 0


# ------------------------------------------------------------------------------------------ files


def _row(page, filename: str):
    return page.locator(".file-row", has_text=filename)


def test_an_upload_is_scanned_then_downloadable_then_deletable(logged_in, stack, tmp_path) -> None:
    page = logged_in
    page.goto(stack.portal + "/files")
    name = f"note-{uuid.uuid4().hex[:6]}.txt"
    path = tmp_path / name
    path.write_text("hello from the browser test\n", encoding="utf-8")
    page.set_input_files("input[type=file]", str(path))
    row = _row(page, name)
    row.wait_for(timeout=15000)
    row.locator(".status:has-text('可用')").wait_for(timeout=20000)  # quarantined (scanning) until the Worker is done
    with page.expect_download() as download:
        row.locator("a:has-text('下载')").click()
    assert open(download.value.path(), encoding="utf-8").read() == "hello from the browser test\n"

    page.on("dialog", lambda d: d.accept())
    row.locator("button:has-text('删除')").click()
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
    row.locator("button:has-text('删除')").click()
    page.wait_for_function("(n) => !document.body.innerText.includes(n)", arg=name, timeout=15000)


# ------------------------------------------------------------------------------------------ sign-up, thinking


def test_a_visitor_can_sign_up_and_lands_logged_in(page, context, stack) -> None:
    page.goto(stack.portal + "/login")
    page.click("a:has-text('注册一个')")
    page.wait_for_url(re.compile(r"/register$"))
    name = f"u{uuid.uuid4().hex[:8]}"
    page.fill("input[name=username]", name)
    page.fill("input[name=display_name]", "新同事")
    page.fill("input[name=password]", "short")
    page.fill("input[name=again]", "short")
    page.click("button[type=submit]")
    page.wait_for_selector(".alert.error")
    assert "8 个字符" in page.inner_text(".alert.error")
    page.fill("input[name=password]", "a-good-password")
    page.fill("input[name=again]", "a-good-password")
    page.click("button[type=submit]")
    page.wait_for_selector(".user-chip:has-text('新同事')", timeout=15000)
    cookie = next(c for c in context.cookies() if c["name"] == "ailab_token")
    assert cookie["httpOnly"] is True
    # the same name cannot be taken twice
    other = page.context.browser.new_context()
    try:
        p2 = other.new_page()
        p2.goto(stack.portal + "/register")
        p2.fill("input[name=username]", name)
        p2.fill("input[name=password]", "another-password")
        p2.fill("input[name=again]", "another-password")
        p2.click("button[type=submit]")
        p2.wait_for_selector(".alert.error")
        assert "已经被注册" in p2.inner_text(".alert.error")
    finally:
        other.close()


def test_the_models_thinking_is_shown_and_folds_away(logged_in) -> None:
    page = logged_in
    _start_conversation(page, "你好呀")
    _wait_for_turns(page, 1)
    head = page.locator(".thinking-head")
    head.wait_for(timeout=15000)
    assert "已深度思考" in head.inner_text()
    assert page.locator(".thinking-body").count() == 0  # folded once the answer is there
    head.click()
    assert "离线演示模型" in page.inner_text(".thinking-body")
    page.reload()  # kept with the run, not only in the live stream
    page.wait_for_selector(".thinking-head")


def test_the_sidebar_collapses_and_remembers_it(logged_in, stack) -> None:
    page = logged_in
    page.wait_for_selector(".sidebar .side-link:has-text('资料库')")
    assert page.locator(".topbar").count() == 0  # no top bar: the sidebar carries the navigation
    assert page.inner_text(".chat-home h1") == "有什么我能帮你的吗？"
    assert page.locator(".suggestion").count() == 0
    page.click("button[aria-label='收起侧栏']")
    page.wait_for_selector(".sidebar", state="hidden")
    page.reload()
    page.wait_for_selector("button[aria-label='展开侧栏']")
    assert not page.locator(".sidebar").is_visible()
    page.click("button[aria-label='展开侧栏']")
    page.wait_for_selector(".sidebar", state="visible")
    page.click(".side-link:has-text('资料库')")
    page.wait_for_url(stack.portal + "/files")
    page.wait_for_selector(".side-link.on:has-text('资料库')")


# ------------------------------------------------------------------------------------------ modes, skills, attachments


def test_chat_mode_answers_in_words_without_a_sandbox(logged_in, stack) -> None:
    page = logged_in
    page.click(".mode-toggle button:has-text('对话')")
    _start_conversation(page, "在沙箱里执行一条命令")  # would use the sandbox in work mode
    _wait_for_turns(page, 1)
    assert "离线演示模型" in page.inner_text(".msg.assistant .md")
    assert page.locator(".steps").count() == 0
    assert page.inner_text(".msg.user .attach-chip") == "对话模式"
    page.goto(stack.portal + "/")
    page.wait_for_selector(".mode-toggle button.on:has-text('对话')")  # remembered


def test_a_skill_button_makes_the_file_with_that_skill(logged_in) -> None:
    page = logged_in
    page.click(".skill-chip:has-text('PPT 生成')")
    page.wait_for_selector(".skill-tag:has-text('PPT 生成')")
    assert "页数" in page.get_attribute("textarea", "placeholder")
    _start_conversation(page, "春游活动介绍")  # no "PPT" in the words: the button says it
    _wait_for_turns(page, 1, seconds=120)
    _open_all_steps(page)
    steps = "\n".join(page.locator(".steps").all_inner_texts())
    assert "已加载技能" in steps and "交付文件" in steps
    assert "ailab-demo.pptx" in page.inner_text(".file-card")
    assert page.inner_text(".msg.user .attach-chip") == "PPT 生成"
    assert page.locator(".skill-tag").count() == 0  # one message only


def test_an_attachment_is_read_without_a_sandbox(logged_in, tmp_path) -> None:
    page = logged_in
    page.click(".mode-toggle button:has-text('对话')")
    note = tmp_path / "会议纪要.txt"
    note.write_text("本周完成了登录改版。下周开始做对话界面。", encoding="utf-8")
    page.set_input_files(".composer-box input[type=file]", str(note))
    page.wait_for_selector(".composer-box .attach-chip:has-text('会议纪要.txt')")
    page.wait_for_function("() => !document.querySelector('.composer-box').innerText.includes('扫描中')", timeout=20000)
    _start_conversation(page, "总结一下这份纪要")
    _wait_for_turns(page, 1)
    assert "我读到了 1 个附件的内容" in page.inner_text(".msg.assistant .md")
    _open_all_steps(page)
    assert "已读取附件" in page.inner_text(".steps") and "会议纪要.txt" in page.inner_text(".steps")


def test_attachment_upload_and_scan_responses_cannot_race_with_send_or_remove(logged_in, tmp_path) -> None:
    page = logged_in
    pending = {}
    page.route("**/api/v1/files", lambda route: pending.update(upload=route))
    page.route("**/api/v1/files/deferred-file", lambda route: pending.update(scan=route))
    note = tmp_path / "deferred.txt"
    note.write_text("still uploading", encoding="utf-8")
    page.fill("textarea", "请读取附件")
    page.set_input_files(".composer-box input[type=file]", str(note))
    page.wait_for_timeout(100)
    assert page.get_by_role("button", name="发送", exact=True).is_disabled()
    assert "upload" in pending
    pending["upload"].fulfill(json={"id": "deferred-file", "filename": note.name, "status": "pending"})
    page.wait_for_selector(".composer-box .attach-chip")
    deadline = time.monotonic() + 10
    while "scan" not in pending and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert "scan" in pending
    page.get_by_role("button", name="移除 deferred.txt", exact=True).click()
    pending["scan"].fulfill(json={"id": "deferred-file", "filename": note.name, "status": "ready"})
    page.wait_for_timeout(200)
    assert page.locator(".composer-box .attach-chip").count() == 0
    assert page.get_by_role("button", name="发送", exact=True).is_enabled()


def test_skills_can_be_switched_off_and_their_buttons_go_away(logged_in, stack) -> None:
    page = logged_in
    page.click(".side-link:has-text('技能')")
    page.wait_for_url(stack.portal + "/skills")
    ppt = page.locator(".skill-row", has_text="演示文稿（PPT）")
    switch = ppt.locator("button[role=switch]")
    assert switch.get_attribute("aria-checked") == "true"
    try:
        switch.click()
        page.wait_for_function("() => document.querySelector('.skill-row .switch').getAttribute('aria-checked') === 'false'")
        page.reload()  # saved on the server, not just in the page
        page.wait_for_selector(".skill-row.off:has-text('演示文稿（PPT）')")
        page.goto(stack.portal + "/")
        page.wait_for_selector(".skill-chip:has-text('Excel 表格')")
        assert page.locator(".skill-chip:has-text('PPT 生成')").count() == 0
    finally:
        page.goto(stack.portal + "/skills")
        page.wait_for_selector(".skill-row")
        if ppt.locator("button[role=switch]").get_attribute("aria-checked") == "false":
            ppt.locator("button[role=switch]").click()
            page.wait_for_selector(".skill-row:not(.off):has-text('演示文稿（PPT）')")


def test_a_personal_skill_uploaded_to_the_library_can_be_picked(logged_in, stack, tmp_path) -> None:
    page = logged_in
    name = f"weekly-{uuid.uuid4().hex[:6]}"
    skill = tmp_path / "SKILL.md"
    skill.write_text(f"---\nname: {name}\ntitle: 周报助手\ndescription: 按团队模板写周报\n---\n# 步骤\n先列本周完成。\n", encoding="utf-8")
    page.goto(stack.portal + "/skills")
    page.wait_for_selector(".skill-row")
    page.set_input_files("input[accept='.md,.zip']", str(skill))
    row = page.locator(".skill-row", has_text="周报助手")
    row.wait_for(timeout=40000)
    assert row.locator("button[role=switch]").get_attribute("aria-checked") == "true"
    assert name in row.inner_text()

    page.goto(stack.portal + "/files")  # it is a file in the library
    page.wait_for_selector(".skill-file-chip:has-text('周报助手')")

    page.goto(stack.portal + "/")
    page.click(".skill-chip:has-text('周报助手')")
    _start_conversation(page, "帮我写本周周报")
    _wait_for_turns(page, 1)
    assert page.inner_text(".msg.user .attach-chip") == "周报助手"
    _open_all_steps(page)
    assert "已加载技能" in "\n".join(page.locator(".steps").all_inner_texts())

    page.goto(stack.portal + "/skills")  # deleting the skill deletes the file
    page.on("dialog", lambda d: d.accept())
    page.locator(".skill-row", has_text="周报助手").locator("button:has-text('删除')").click()
    page.wait_for_function("() => !document.body.innerText.includes('周报助手')", timeout=15000)
