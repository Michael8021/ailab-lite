"""System prompt, skill index and conversation history for the Agent Service.

Skills are the same SKILL.md files baked into the sandbox image (/opt/skills). Only their names and one-line
descriptions go into every prompt; the model reads a full skill with load_skill when a task needs it, so a plain
chat turn does not pay for pages of PPT instructions.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..config import settings

BASE_PROMPT = """你是 AILab 助手：既能像聊天助手一样回答问题，也能在隔离沙箱里动手完成任务（做 PPT、Excel、文档，处理文件，写代码运行）。

# 怎么回答
- 普通问题（解释、写作、翻译、总结一段话、出主意、闲聊）**直接用文字回答，不要调用任何工具**。
- 需要产出文件、处理用户上传的文件、或者要运行代码计算时才用工具。沙箱在你第一次用到时才启动（几秒钟）。
- 用中文和 Markdown 回答：先给结论，结构清楚，简洁；不要把工具的原始输出整段贴给用户。
- 不确定的事实不要编造；需要用户提供的信息（数据、名称、时间）就直接问。

# 沙箱约定
- /workspace/inputs/：用户本轮上传的文件（只读使用）
- /workspace/work/：你的草稿（规格文件、脚本、中间数据）
- /workspace/assets/：图片等素材
- /workspace/outputs/：交付给用户的成品。这里的 .pptx / .xlsx / .docx / .pdf 一生成，平台就会自动渲染预览，用户实时能看到
- 沙箱没有网络；不要尝试读取环境变量、密钥或系统文件；单条命令不超过 10 分钟

# 多轮对话
- 同一个对话里，上一轮留下的 /workspace 文件（work、assets、outputs）会在你使用沙箱时恢复。
- 用户说"改一下""第三页换成…""再加一列…"时：读原来的规格文件或脚本，只改相关部分，重新生成**同名**文件，不要从头再做。
- 改完简要说明改了什么。

# 技能
做下面这些事之前，先调用 load_skill 读取对应技能的完整说明并照做：
{skills}

# 交付
成品生成后调用 deliver_file 提交到资料库，然后用一两句话告诉用户结果和可以怎么继续调整。
outputs 里没交付的文件在本轮结束时也会自动归档。
"""


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    head = text.split("---", 2)[1]
    out = {}
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


@lru_cache
def skills() -> dict[str, dict]:
    """name -> {"description", "path"} for every <skills_dir>/<name>/SKILL.md."""
    root: Path = settings.skills_path
    if not root.is_dir():
        raise FileNotFoundError(f"skills directory not found at {root}; set AILAB_SKILLS_DIR")
    found = {}
    for skill_md in sorted(root.glob("*/SKILL.md")):
        meta = _frontmatter(skill_md.read_text(encoding="utf-8"))
        name = meta.get("name") or skill_md.parent.name
        found[name] = {"description": meta.get("description", ""), "path": skill_md}
    if not found:
        raise FileNotFoundError(f"no skills (*/SKILL.md) under {root}; set AILAB_SKILLS_DIR")
    return found


def load_skill(name: str) -> str:
    skill = skills().get(name)
    if skill is None:
        raise KeyError(f"unknown skill {name!r}; available: {', '.join(skills())}")
    return skill["path"].read_text(encoding="utf-8")


@lru_cache
def system_prompt() -> str:
    """The base prompt with the skill index. A missing skills directory fails loudly instead of silently
    producing an agent that cannot build decks."""
    index = "\n".join(f"- {name}：{s['description']}" for name, s in skills().items())
    return BASE_PROMPT.replace("{skills}", index)


def user_prompt(prompt: str, input_files: list[dict] | None = None) -> str:
    if not input_files:
        return prompt
    lines = [prompt, "", "（本轮附件，使用沙箱时位于 /workspace/inputs/：）"]
    for f in input_files:
        lines.append(f"- /workspace/inputs/{f.get('filename') or f.get('file_id')}")
    return "\n".join(lines)


def history_messages(history: list[dict] | None) -> list[dict]:
    """Earlier turns of the conversation as plain user/assistant messages (tool traffic is not replayed)."""
    out: list[dict] = []
    for turn in history or []:
        out.append({"role": "user", "content": turn.get("prompt") or ""})
        reply = turn.get("reply") or "（没有回复）"
        if turn.get("files"):
            reply += "\n\n（这一轮产出的文件：" + "、".join(turn["files"]) + "）"
        out.append({"role": "assistant", "content": reply})
    return out
