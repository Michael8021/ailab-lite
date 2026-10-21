"""System prompt for the Agent Service.

The skill text is the same file baked into the sandbox image, so the agent and
the sandbox agree on the pptx-builder workflow.
"""

from __future__ import annotations

from functools import lru_cache

from ..config import settings

BASE_PROMPT = """你是 AILab 平台的 Agent Service，负责在隔离沙箱里完成用户任务。

工作目录约定：
- 输入文件在 /workspace/inputs/（只读使用）。
- 产出写到 /workspace/outputs/。
- /workspace 之外不可写；不要尝试读取环境变量、密钥或系统文件。

可用工具：
- list_files(path)：列出目录文件。
- read_file(path)：读取文本文件。
- write_file(path, content)：写入文本文件。
- exec(cmd, timeout_seconds)：在沙箱里执行 shell 命令。
- deliver_file(path, filename)：把产出提交到资料库（必须调用，否则用户拿不到文件）。
- finish(summary)：结束任务并给出中文总结。

规则：
1. 先判断任务是否需要产出文件；需要则生成到 /workspace/outputs/。
2. 做 PPT 时按下面的 pptx-builder 技能执行；生成后必须预览自检，有 OVERFLOW 就精简文字重做。
3. 不要伪造工具结果；命令失败就修正后重试，最多 12 步。
4. 任务完成后调用 deliver_file，再调用 finish。
5. 全程用中文回复，简洁说明做了什么、产出在哪里。
"""


@lru_cache
def system_prompt() -> str:
    """BASE_PROMPT plus the pptx-builder skill.

    A missing skill file used to degrade silently (the mock LLM never reads the
    prompt), so a real model would just fail to build decks. Fail loudly instead.
    """
    path = settings.pptx_skill_path
    if not path.is_file():
        raise FileNotFoundError(f"pptx-builder skill not found at {path}; set AILAB_SKILLS_DIR")
    return BASE_PROMPT + "\n\n--- pptx-builder 技能 ---\n" + path.read_text(encoding="utf-8")


def user_prompt(prompt: str, input_files: list[dict] | None = None) -> str:
    if not input_files:
        return prompt
    lines = ["用户请求：", prompt, "", "本次输入文件："]
    for f in input_files:
        lines.append(f"- {f.get('path') or '/workspace/inputs/' + str(f.get('filename') or f.get('file_id'))}")
    return "\n".join(lines)
