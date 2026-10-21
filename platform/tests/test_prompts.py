from __future__ import annotations

import pytest

from ailab_platform.agent import prompts
from ailab_platform.config import settings


@pytest.fixture(autouse=True)
def _fresh_cache():
    prompts.system_prompt.cache_clear()
    yield
    prompts.system_prompt.cache_clear()


def test_system_prompt_includes_the_pptx_skill() -> None:
    assert "pptx-builder 技能" in prompts.system_prompt()


def test_missing_skill_fails_loudly(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "skills_dir", tmp_path)
    with pytest.raises(FileNotFoundError, match="AILAB_SKILLS_DIR"):
        prompts.system_prompt()


def test_skills_dir_override(monkeypatch, tmp_path) -> None:
    (tmp_path / "pptx-builder").mkdir()
    (tmp_path / "pptx-builder" / "SKILL.md").write_text("CUSTOM SKILL", encoding="utf-8")
    monkeypatch.setattr(settings, "skills_dir", tmp_path)
    assert "CUSTOM SKILL" in prompts.system_prompt()
