from __future__ import annotations

import pytest

from ailab_platform.agent import prompts
from ailab_platform.config import settings


@pytest.fixture(autouse=True)
def _fresh_cache():
    prompts.system_prompt.cache_clear()
    prompts.skills.cache_clear()
    yield
    prompts.system_prompt.cache_clear()
    prompts.skills.cache_clear()


def test_the_system_prompt_lists_the_skills_but_does_not_inline_them() -> None:
    text = prompts.system_prompt()
    assert "- pptx：" in text and "- xlsx：" in text
    assert "build.py" not in text  # the full instructions come from load_skill


def test_load_skill_returns_the_whole_skill() -> None:
    assert "/opt/skills/pptx/scripts/build.py" in prompts.load_skill("pptx")
    with pytest.raises(KeyError, match="available: pptx, xlsx"):
        prompts.load_skill("nope")


def test_missing_skills_fail_loudly(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "skills_dir", tmp_path / "absent")
    with pytest.raises(FileNotFoundError, match="AILAB_SKILLS_DIR"):
        prompts.system_prompt()
    prompts.skills.cache_clear()
    monkeypatch.setattr(settings, "skills_dir", tmp_path)  # exists, but empty
    with pytest.raises(FileNotFoundError, match="AILAB_SKILLS_DIR"):
        prompts.system_prompt()


def test_skills_dir_override(monkeypatch, tmp_path) -> None:
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "SKILL.md").write_text("---\nname: custom\ndescription: CUSTOM SKILL\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(settings, "skills_dir", tmp_path)
    assert "- custom：CUSTOM SKILL" in prompts.system_prompt()
    assert prompts.load_skill("custom").endswith("body")


def test_history_becomes_user_and_assistant_messages() -> None:
    msgs = prompts.history_messages([{"prompt": "q", "reply": "", "files": ["a.xlsx"]}])
    assert msgs[0] == {"role": "user", "content": "q"}
    assert msgs[1]["role"] == "assistant" and "a.xlsx" in msgs[1]["content"]
