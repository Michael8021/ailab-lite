"""AgentLoop against fakes: user attribution, bad tool arguments, reasoning echo."""

from __future__ import annotations

import asyncio

from ailab_platform.agent.loop import AgentLoop
from ailab_platform.config import settings
from ailab_platform.llm import INVALID_ARGUMENTS_KEY, LLMClient, LLMResponse, ToolCall


class FakePlatform:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, dict]] = []
        self.events: list[tuple[str, dict]] = []

    async def get_run(self, run_id):
        return {"id": run_id, "user_id": "usr_42", "status": "queued", "model": "m", "prompt": "hi", "input_files": []}

    async def set_run_status(self, run_id, status, **kw):
        self.statuses.append((status, kw))
        return {}

    async def get_run_state(self, run_id):
        # through get_run, so the fakes below that change a run's status (cancel, API errors) apply here too
        return {"status": (await self.get_run(run_id))["status"]}

    async def append_event(self, run_id, type_, payload=None):
        self.events.append((type_, payload or {}))

    async def record_usage(self, *a, **k):
        pass


class FakeBroker:
    def __init__(self) -> None:
        self.put_calls: list[tuple] = []
        self.leases = 0
        self.preview_calls = 0
        self.snapshots = 0

    async def create_lease(self, run_id, inputs=None, **_):
        self.leases += 1
        return {"lease_id": "lease_1", "provider": "fake", "inputs": []}

    async def exec(self, lease_id, cmd, timeout=None):
        return {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}

    async def refresh_previews(self, lease_id):
        self.preview_calls += 1
        return []

    async def snapshot(self, lease_id):
        self.snapshots += 1
        return {"key": "workspaces/run_1.tgz", "size": 10}

    async def list_files(self, lease_id, path):
        return []

    async def commit(self, lease_id, path, filename=None, role="output"):
        return {"filename": filename or path.rsplit("/", 1)[-1], "size": 1}

    async def put_file(self, lease_id, path, data):
        self.put_calls.append((path, data))

    async def release(self, lease_id):
        pass


class ScriptedLLM(LLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.user_ids: list[str | None] = []
        self.seen_messages: list[list[dict]] = []

    async def chat(self, messages, tools, model, user_id=None):
        self.user_ids.append(user_id)
        self.seen_messages.append([dict(m) for m in messages])
        return self.responses.pop(0)


def _run(llm, platform=None, broker=None):
    platform, broker = platform or FakePlatform(), broker or FakeBroker()
    asyncio.run(AgentLoop(platform=platform, broker=broker, llm=llm).process("run_1"))
    return platform, broker


def test_user_id_reaches_the_model_gateway() -> None:
    llm = ScriptedLLM([LLMResponse(content="done")])
    platform, _ = _run(llm)
    assert llm.user_ids == ["usr_42"]
    assert platform.statuses[-1][0] == "succeeded"


def test_tool_call_with_invalid_json_is_not_executed() -> None:
    bad = ToolCall(id="c1", name="write_file", arguments={INVALID_ARGUMENTS_KEY: '{"path": "/workspace/a.txt", "con'})
    llm = ScriptedLLM([LLMResponse(tool_calls=[bad]), LLMResponse(content="giving up")])
    platform, broker = _run(llm)

    assert broker.put_calls == []  # no empty file written
    result = next(p for t, p in platform.events if t == "agent.tool_result")
    assert result["ok"] is False and "valid JSON" in result["error"]
    # the model sees its own malformed text and the error
    assert llm.seen_messages[1][-2]["tool_calls"][0]["function"]["arguments"].startswith('{"path"')


def test_the_loop_keeps_reasoning_and_streams_thinking_events() -> None:
    step = ToolCall(id="c1", name="list_files", arguments={"path": "/nope"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[step], reasoning_content="why " * 10), LLMResponse(content="x", reasoning_content="ok")])
    platform, _ = _run(llm)
    assistant = next(m for m in llm.seen_messages[1] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "why " * 10  # whether the upstream gets it back is the client's call
    thinking = [p for t, p in platform.events if t == "agent.thinking"]
    assert "".join(p["text"] for p in thinking if p["step"] == 0) == "why " * 10
    assert all("seconds" in p for p in thinking)


def test_tool_call_events_do_not_store_huge_arguments() -> None:
    big = ToolCall(id="c1", name="write_file", arguments={"path": "/workspace/a", "content": "x" * 50_000})
    llm = ScriptedLLM([LLMResponse(tool_calls=[big]), LLMResponse(content="done")])
    platform, _ = _run(llm)
    event = next(p for t, p in platform.events if t == "agent.tool_call")
    assert len(event["arguments"]["content"]) < 600


# ---- leftovers, cancellation, tokens, waiting for a sandbox ---------------------------------

import time  # noqa: E402

from ailab_platform.clients import ClientError  # noqa: E402


class RichBroker(FakeBroker):
    """`outputs` appear once the run has started working: the listing taken right after leasing is empty."""

    def __init__(self, outputs=None, lease_failures=0, slow_exec=False) -> None:
        super().__init__()
        self.outputs = outputs or []
        self.lease_failures = lease_failures
        self.slow_exec = slow_exec
        self.commits: list[str] = []
        self.released = 0
        self.listings = 0

    async def create_lease(self, run_id, inputs=None, **_):
        if self.lease_failures:
            self.lease_failures -= 1
            raise ClientError("POST", "u", 429, "sandbox capacity reached (1)")
        return {"lease_id": "lease_1", "provider": "fake", "inputs": [{"path": "/workspace/inputs/a.txt"}]}

    async def list_files(self, lease_id, path):
        self.listings += 1
        return [] if self.listings == 1 else self.outputs

    async def commit(self, lease_id, path, filename=None, role="output"):
        self.commits.append(path)
        return {"filename": filename or path.rsplit("/", 1)[-1], "size": 1}

    async def exec(self, lease_id, cmd, timeout=None):
        if self.slow_exec:
            await asyncio.sleep(30)  # a long-running command
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def release(self, lease_id):
        self.released += 1


def test_files_the_model_forgot_to_deliver_are_archived() -> None:
    broker = RichBroker(outputs=[{"path": "/workspace/outputs/report.md", "size": 5}])
    write = ToolCall(id="c1", name="exec", arguments={"cmd": "echo hi > /workspace/outputs/report.md"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[write]), LLMResponse(content="done, report is ready")])
    platform, _ = _run(llm, broker=broker)
    assert broker.commits == ["/workspace/outputs/report.md"]
    assert platform.statuses[-1][0] == "succeeded"
    assert platform.statuses[-1][1]["result"]["artifacts"][0]["auto"] is True


def test_already_delivered_file_is_not_delivered_twice() -> None:
    broker = RichBroker(outputs=[{"path": "/workspace/outputs/report.md", "size": 1}])
    deliver = ToolCall(id="c1", name="deliver_file", arguments={"path": "/workspace/outputs/report.md"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[deliver]), LLMResponse(content="done")])
    _run(llm, broker=broker)
    assert broker.commits == ["/workspace/outputs/report.md"]  # once, by the model


def test_deliver_file_outside_outputs_is_refused_before_reaching_the_broker() -> None:
    broker = RichBroker()
    bad = ToolCall(id="c1", name="deliver_file", arguments={"path": "/workspace/notes.txt"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[bad]), LLMResponse(content="done")])
    platform, _ = _run(llm, broker=broker)
    assert broker.commits == []
    result = next(p for t, p in platform.events if t == "agent.tool_result")
    assert result["ok"] is False and "outputs" in result["error"]


def test_token_limit_stops_the_run_but_keeps_the_work(monkeypatch) -> None:
    monkeypatch.setattr(settings, "run_max_tokens", 100)
    broker = RichBroker(outputs=[{"path": "/workspace/outputs/partial.md", "size": 3}])
    step = ToolCall(id="c", name="list_files", arguments={"path": "/workspace"})
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[step], usage={"prompt_tokens": 50, "completion_tokens": 20}),
            LLMResponse(content="over budget", usage={"prompt_tokens": 30, "completion_tokens": 20}),
            LLMResponse(content="never reached"),
        ]
    )
    platform, _ = _run(llm, broker=broker)
    status, kw = platform.statuses[-1][0], platform.statuses[-1][1]
    assert status == "failed" and "token limit" in kw["error"]
    assert broker.commits == ["/workspace/outputs/partial.md"]
    assert len(llm.responses) == 1  # no further model call once the budget was consumed


def test_run_waits_for_a_free_sandbox(monkeypatch) -> None:
    import ailab_platform.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "SANDBOX_RETRY_SECONDS", 0.01)
    broker = RichBroker(lease_failures=3)
    work = ToolCall(id="c1", name="exec", arguments={"cmd": "true"})
    platform, _ = _run(ScriptedLLM([LLMResponse(tool_calls=[work]), LLMResponse(content="ok")]), broker=broker)
    assert [t for t, _ in platform.events].count("agent.waiting_sandbox") == 1
    assert platform.statuses[-1][0] == "succeeded"


def test_gives_up_waiting_for_a_sandbox(monkeypatch) -> None:
    import ailab_platform.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "SANDBOX_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(settings, "sandbox_wait_seconds", 0.05)
    work = ToolCall(id="c1", name="exec", arguments={"cmd": "true"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[work]), LLMResponse(content="could not")])
    platform, _ = _run(llm, broker=RichBroker(lease_failures=10_000))
    result = next(p for t, p in platform.events if t == "agent.tool_result")
    assert result["ok"] is False and "no sandbox" in result["error"]  # the model is told and can answer without it


def test_cancel_interrupts_a_running_tool_and_frees_the_sandbox(monkeypatch) -> None:
    import ailab_platform.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "CANCEL_POLL_SECONDS", 0.05)

    class CancelingPlatform(FakePlatform):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_run(self, run_id):
            self.calls += 1
            run = await super().get_run(run_id)
            if self.calls > 3:  # the user clicks cancel while the command runs
                run["status"] = "canceled"
            return run

    broker = RichBroker(slow_exec=True)
    slow = ToolCall(id="c1", name="exec", arguments={"cmd": "sleep 30"})
    started = time.monotonic()
    platform, _ = _run(ScriptedLLM([LLMResponse(tool_calls=[slow])]), platform=CancelingPlatform(), broker=broker)
    assert time.monotonic() - started < 5
    assert "agent.canceled" in [t for t, _ in platform.events]
    assert broker.released == 1
    assert platform.statuses[-1][0] == "running"  # the API already holds `canceled`; the Agent adds nothing


def test_a_flaky_status_check_does_not_fail_the_run() -> None:
    class FlakyPlatform(FakePlatform):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_run(self, run_id):
            self.calls += 1
            if self.calls in (2, 3):  # transient API errors on the cancel checks
                raise ConnectionError("api restarting")
            return await super().get_run(run_id)

    platform, _ = _run(ScriptedLLM([LLMResponse(content="done")]), platform=FlakyPlatform())
    assert platform.statuses[-1][0] == "succeeded"


# ---- read_file ----------------------------------------------------------------------------------

from ailab_platform.agent.tools import READ_LIMIT_CHARS, execute_tool  # noqa: E402


class _ReadBroker:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def get_file(self, lease_id, path):
        return self.data


def _read(data: bytes) -> dict:
    return asyncio.run(execute_tool("read_file", {"path": "/workspace/x"}, "lease", _ReadBroker(data)))


def test_read_file_refuses_binary_instead_of_flooding_the_context() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(256)) * 50
    out = _read(png)
    assert out["ok"] is False and "binary" in out["error"] and "content" not in out


def test_read_file_reads_text_including_chinese_and_truncates_long_files() -> None:
    assert _read("你好，世界\n".encode())["content"] == "你好，世界\n"
    big = _read(("字" * (READ_LIMIT_CHARS + 500)).encode())
    assert big["ok"] and len(big["content"]) < READ_LIMIT_CHARS + 100 and "truncated" in big["content"]


def test_a_multibyte_char_split_by_the_sniff_window_is_still_text() -> None:
    text = ("a" * 8191 + "字" * 10).encode()  # the 8 KiB window cuts the first 字 in half
    assert _read(text)["ok"] is True


# ---- the run's own model decides which client is used ------------------------------------------------


def test_run_resolves_its_model_from_the_catalog() -> None:
    class P(FakePlatform):
        async def get_run(self, run_id):
            return {**await super().get_run(run_id), "model": "test-fast", "prompt": "你好"}

    platform = P()
    asyncio.run(AgentLoop(platform=platform, broker=RichBroker()).process("run_1"))  # no llm injected: catalog -> MockLLM
    assert platform.statuses[-1][0] == "succeeded"


def test_run_for_a_model_that_left_the_catalog_fails_cleanly() -> None:
    class P(FakePlatform):
        async def get_run(self, run_id):
            return {**await super().get_run(run_id), "model": "retired-model"}

    platform, broker = P(), RichBroker()
    asyncio.run(AgentLoop(platform=platform, broker=broker).process("run_1"))
    status, kw = platform.statuses[-1][0], platform.statuses[-1][1]
    assert status == "failed" and "not available" in kw["error"]
    assert broker.released == 0  # failed before a sandbox was leased


# ---- conversations: no sandbox for chat, streaming, history, previews, workspace ------------------


class ConversationPlatform(FakePlatform):
    def __init__(self, history=None, conversation_id="cnv_1") -> None:
        super().__init__()
        self.history = history or []
        self.conversation_id = conversation_id

    async def get_run(self, run_id):
        run = await super().get_run(run_id)
        return {**run, "conversation_id": self.conversation_id, "history": self.history, "prompt": "and now?"}


def test_a_chat_answer_needs_no_sandbox_and_streams() -> None:
    broker = FakeBroker()
    platform, _ = _run(ScriptedLLM([LLMResponse(content="一段" * 100)]), broker=broker)
    assert broker.leases == 0
    deltas = [p["text"] for t, p in platform.events if t == "agent.delta"]
    assert len(deltas) >= 2 and "".join(deltas) == "一段" * 100
    assert platform.statuses[-1][0] == "succeeded" and platform.statuses[-1][1]["summary"] == "一段" * 100


def test_load_skill_needs_no_sandbox() -> None:
    broker = FakeBroker()
    read = ToolCall(id="c1", name="load_skill", arguments={"name": "pptx"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[read]), LLMResponse(content="ok")])
    _run(llm, broker=broker)
    assert broker.leases == 0
    tool_msg = llm.seen_messages[1][-1]
    assert tool_msg["role"] == "tool" and "build.py" in tool_msg["content"]


def test_earlier_turns_are_given_to_the_model() -> None:
    history = [{"prompt": "做个 PPT", "reply": "做好了", "files": ["a.pptx"], "status": "succeeded"}]
    llm = ScriptedLLM([LLMResponse(content="ok")])
    _run(llm, platform=ConversationPlatform(history))
    roles = [(m["role"], m["content"][:6]) for m in llm.seen_messages[0][1:]]
    assert roles == [("user", "做个 PPT"), ("assistant", "做好了\n\n（"), ("user", "and no")]
    assert "a.pptx" in llm.seen_messages[0][2]["content"]


def test_previews_render_after_file_changes_and_land_in_the_result() -> None:
    class PreviewBroker(FakeBroker):
        async def refresh_previews(self, lease_id):
            self.preview_calls += 1
            if self.preview_calls == 1:
                return [{"filename": "deck.pptx", "kind": "pages", "pages": ["1/p-01.jpg"], "version": 1}]
            return []

    broker = PreviewBroker()
    build = ToolCall(id="c1", name="exec", arguments={"cmd": "python build.py"})
    platform, _ = _run(ScriptedLLM([LLMResponse(tool_calls=[build]), LLMResponse(content="done")]), broker=broker)
    updated = [p for t, p in platform.events if t == "preview.updated"]
    assert updated and updated[0]["filename"] == "deck.pptx"
    assert platform.statuses[-1][1]["result"]["previews"][0]["pages"] == ["1/p-01.jpg"]
    assert broker.preview_calls >= 2  # once after the build, once more at the end


def test_a_conversation_turn_saves_its_workspace_only_if_it_used_a_sandbox() -> None:
    broker = FakeBroker()
    platform, _ = _run(ScriptedLLM([LLMResponse(content="hi")]), platform=ConversationPlatform(), broker=broker)
    assert broker.snapshots == 0 and "workspace_saved" not in platform.statuses[-1][1]["result"]

    broker = FakeBroker()
    work = ToolCall(id="c1", name="exec", arguments={"cmd": "true"})
    platform, _ = _run(
        ScriptedLLM([LLMResponse(tool_calls=[work]), LLMResponse(content="ok")]), platform=ConversationPlatform(), broker=broker
    )
    assert broker.snapshots == 1 and platform.statuses[-1][1]["result"]["workspace_saved"] is True


def test_search_images_puts_pictures_and_credits_into_the_sandbox() -> None:
    broker = FakeBroker()

    async def get_file(lease_id, path):
        raise FileNotFoundError(path)

    broker.get_file = get_file
    find = ToolCall(id="c1", name="search_images", arguments={"query": "city skyline", "count": 2})
    platform, _ = _run(ScriptedLLM([LLMResponse(tool_calls=[find]), LLMResponse(content="ok")]), broker=broker)
    paths = [p for p, _ in broker.put_calls]
    assert paths == [
        "/workspace/assets/images/city_skyline_1.jpg",
        "/workspace/assets/images/city_skyline_2.jpg",
        "/workspace/assets/images/credits.json",
    ]
    assert broker.put_calls[0][1][:2] == b"\xff\xd8"  # JPEG
    result = next(p for t, p in platform.events if t == "agent.tool_result")
    assert result["ok"] and result["images"] == paths[:2]


def test_near_the_step_limit_the_model_is_told_to_wrap_up(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_agent_steps", 8)
    work = [LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="exec", arguments={"cmd": "true"})]) for i in range(5)]
    llm = ScriptedLLM([*work, LLMResponse(content="done")])
    _run(llm)
    notes = [m for m in llm.seen_messages[-1] if m["role"] == "user" and "平台提示" in m["content"]]
    assert len(notes) == 1 and "3 次" in notes[0]["content"]


def test_running_out_of_steps_ends_with_a_summary_not_a_bare_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_agent_steps", 2)
    deliver = ToolCall(id="c1", name="deliver_file", arguments={"path": "/workspace/outputs/a.pptx"})
    again = ToolCall(id="c2", name="exec", arguments={"cmd": "true"})
    llm = ScriptedLLM([LLMResponse(tool_calls=[deliver]), LLMResponse(tool_calls=[again]), LLMResponse(content="做完了大半，还差最后一页")])
    platform, _ = _run(llm)
    status, kw = platform.statuses[-1]
    assert status == "succeeded" and kw["summary"] == "做完了大半，还差最后一页"  # something was delivered
    assert llm.seen_messages[-1][-1]["content"].startswith("（平台提示：本轮的步数已用完")

    llm = ScriptedLLM([LLMResponse(tool_calls=[again]), LLMResponse(tool_calls=[again]), LLMResponse(content="没做成")])
    platform, _ = _run(llm)
    status, kw = platform.statuses[-1]
    assert status == "failed" and "exceeded 2 steps" in kw["error"] and kw["summary"] == "没做成"


def test_exec_accepts_command_as_well_as_cmd() -> None:
    broker = FakeBroker()
    seen = []

    async def exec_(lease_id, cmd, timeout=None):
        seen.append(cmd)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    broker.exec = exec_
    call = ToolCall(id="c1", name="exec", arguments={"command": "echo hi"})
    _run(ScriptedLLM([LLMResponse(tool_calls=[call]), LLMResponse(content="ok")]), broker=broker)
    assert seen == ["echo hi"]


def test_the_turns_thinking_is_kept_with_the_run() -> None:
    llm = ScriptedLLM([LLMResponse(content="x", reasoning_content="ponder")])
    platform, _ = _run(llm)
    assert platform.statuses[-1][1]["result"]["thinking"]["text"] == "ponder"


def test_a_model_without_tool_calling_is_offered_no_tools(monkeypatch) -> None:
    from ailab_platform.catalog import Catalog, ModelSpec

    catalog = Catalog([ModelSpec(id="m", name="m", provider="mock", tools=False)])
    monkeypatch.setattr("ailab_platform.agent.loop.get_catalog", lambda: catalog)
    offered = []

    class Recording(ScriptedLLM):
        async def chat(self, messages, tools, model, user_id=None):
            offered.append(tools)
            return await super().chat(messages, tools, model, user_id)

    _run(Recording([LLMResponse(content="hi")]))
    assert offered == [[]]


def test_cancel_checks_use_the_light_state_endpoint() -> None:
    class Counting(FakePlatform):
        def __init__(self) -> None:
            super().__init__()
            self.full, self.light = 0, 0

        async def get_run(self, run_id):
            self.full += 1
            return await super().get_run(run_id)

        async def get_run_state(self, run_id):
            self.light += 1
            return {"status": "running"}

    step = ToolCall(id="c1", name="list_files", arguments={"path": "/workspace"})
    platform = Counting()
    _run(ScriptedLLM([LLMResponse(tool_calls=[step]), LLMResponse(content="ok")]), platform=platform)
    assert platform.light >= 2 and platform.full == 1  # the run is read in full once, at the start
