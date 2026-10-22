from __future__ import annotations

import asyncio
import json

from ailab_platform.llm import MockLLM


def _call(messages: list[dict], prompt: str):
    return asyncio.run(MockLLM().chat(messages, tools=[], model="mock"))


def test_mock_ppt_sequence() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "帮我做一个 PPT"},
    ]
    first = _call(messages, "帮我做一个 PPT")
    assert first.tool_calls and first.tool_calls[0].name == "load_skill"
    assert first.tool_calls[0].arguments == {"name": "pptx"}

    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {"name": "load_skill", "arguments": "{}"},
                }
            ],
        }
    )
    messages.append({"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "{}"})
    second = _call(messages, "帮我做一个 PPT")
    assert second.tool_calls[0].name == "exec"
    assert "build.py --example" in second.tool_calls[0].arguments["cmd"]


def test_mock_chat_answers_without_tools() -> None:
    reply = _call([{"role": "system", "content": "sys"}, {"role": "user", "content": "你好"}], "你好")
    assert not reply.tool_calls and "离线演示模型" in reply.content


def test_mock_counts_steps_from_the_latest_user_message() -> None:
    history = [
        {"role": "user", "content": "在沙箱里执行命令"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "x", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "x", "content": "{}"},
        {"role": "assistant", "content": "done"},
    ]
    first = _call([*history, {"role": "user", "content": "再在沙箱里执行一次命令"}], "")
    assert first.tool_calls and first.tool_calls[0].name == "exec"


def test_mock_sandbox_command_finishes() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "在沙箱里执行一条命令"},
    ]
    first = _call(messages, "在沙箱里执行一条命令")
    assert first.tool_calls[0].name == "exec"
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {"name": "exec", "arguments": json.dumps({"cmd": "echo hi"})},
                }
            ],
        }
    )
    messages.append({"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "{}"})
    final = _call(messages, "在沙箱里执行一条命令")
    assert not final.tool_calls
    assert final.content
