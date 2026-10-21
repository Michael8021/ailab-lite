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
    assert first.tool_calls and first.tool_calls[0].name == "write_file"
    assert "outline.json" in first.tool_calls[0].arguments["path"]

    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        }
    )
    messages.append({"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "{}"})
    second = _call(messages, "帮我做一个 PPT")
    assert second.tool_calls[0].name == "exec"
    assert "build.py" in second.tool_calls[0].arguments["cmd"]


def test_mock_generic_finishes() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "你好"},
    ]
    first = _call(messages, "你好")
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
    final = _call(messages, "你好")
    assert not final.tool_calls
    assert final.content
