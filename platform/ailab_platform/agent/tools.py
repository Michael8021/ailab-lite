"""Agent tools exposed to the model.

Every tool is executed through the Broker; the Agent never owns a sandbox
handle or a container runtime socket.
"""

from __future__ import annotations

import json
from typing import Any

from ..clients import AsyncBrokerClient
from ..llm import INVALID_ARGUMENTS_KEY
from ..paths import OUTPUTS, WORKSPACE, PathError, normalize_path


class ToolError(RuntimeError):
    pass


TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出沙箱目录中的文件（最多 500 个）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "/workspace"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取沙箱中的文本文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把文本内容写入沙箱文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "在沙箱内执行 shell 命令，返回 stdout/stderr/exit_code。",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deliver_file",
            "description": "把 /workspace 下的产出提交到资料库，生成可下载的 Artifact。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "结束任务，给出中文总结。",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


def _workspace_path(path: str, under: str = WORKSPACE) -> str:
    try:
        return normalize_path(path, under)
    except PathError as exc:
        raise ToolError(str(exc)) from exc


READ_LIMIT_CHARS = 30_000


def _looks_binary(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return True
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # a multibyte character cut by the 8 KiB window is fine; anything else is not text
        return exc.start < len(head) - 4
    return False


def _truncate(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


async def execute_tool(name: str, args: dict[str, Any], lease_id: str, broker: AsyncBrokerClient) -> dict:
    if INVALID_ARGUMENTS_KEY in args:
        raise ToolError("tool arguments were not valid JSON; resend the call with a well-formed JSON object")
    if name == "exec":
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            raise ToolError("exec requires cmd")
        timeout = args.get("timeout_seconds")
        result = await broker.exec(lease_id, cmd, int(timeout) if timeout else None)
        return {
            "ok": result.get("exit_code") == 0,
            "exit_code": result.get("exit_code"),
            "stdout": _truncate(str(result.get("stdout") or "")),
            "stderr": _truncate(str(result.get("stderr") or "")),
            "duration_ms": result.get("duration_ms"),
        }

    if name == "write_file":
        path = _workspace_path(str(args.get("path") or ""))
        content = str(args.get("content") or "")
        await broker.put_file(lease_id, path, content.encode("utf-8"))
        return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}

    if name == "read_file":
        path = _workspace_path(str(args.get("path") or ""))
        data = await broker.get_file(lease_id, path)
        if _looks_binary(data):
            # Decoding an image/zip as text would put ~100k characters of noise into the context.
            return {"ok": False, "path": path, "error": f"binary file ({len(data)} bytes); read_file only reads text files"}
        return {"ok": True, "path": path, "content": _truncate(data.decode("utf-8", "replace"), READ_LIMIT_CHARS)}

    if name == "list_files":
        path = _workspace_path(str(args.get("path") or "/workspace"))
        files = await broker.list_files(lease_id, path)
        return {"ok": True, "path": path, "files": files}

    if name == "deliver_file":
        path = _workspace_path(str(args.get("path") or ""), OUTPUTS)
        filename = args.get("filename")
        result = await broker.commit(lease_id, path, str(filename) if filename else None)
        return {"ok": True, **result}

    if name == "finish":
        return {"ok": True, "summary": str(args.get("summary") or "")}

    raise ToolError(f"unknown tool: {name}")


def tool_result_message(call_id: str, name: str, result: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(result, ensure_ascii=False),
    }
