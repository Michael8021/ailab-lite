"""Skill files: recognising a skill among a user's files, and unpacking it safely.

A personal skill is a file in the user's library, in one of two shapes (the same as Claude's skills):

* ``SKILL.md`` alone (any file name ending in .md): YAML-ish front matter with ``name`` and ``description``
  (``title`` optional), then the instructions.
* a ``.zip`` holding ``SKILL.md`` at its root or in one top-level folder, next to scripts and assets the
  instructions refer to by relative path.

The Worker calls ``inspect`` when a file is uploaded and stores the result in the file's meta; the Agent calls
``unpack`` to put a zip skill's files into the sandbox. Neither ever writes outside the paths they return.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_SKILL_MD = 100 * 1024
MAX_ENTRIES = 200
MAX_UNPACKED = 30 * 1024 * 1024
MAX_ZIP = 10 * 1024 * 1024


class SkillFileError(ValueError):
    """Why a file that looks like a skill cannot be used (shown to the user, in Chinese)."""


def frontmatter(text: str) -> dict[str, str]:
    """`key: value` lines between the leading `---` fences (enough for name / title / description)."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out = {}
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip().strip("'\"")
    return out


def _describe(text: str) -> dict:
    meta = frontmatter(text)
    name, description = meta.get("name", "").strip(), meta.get("description", "").strip()
    if not name or not description:
        raise SkillFileError("SKILL.md 开头需要 --- 包起来的 name 和 description 两项")
    if not NAME_RE.match(name):
        raise SkillFileError(f"技能名 {name!r} 不合规：只能用小写字母、数字和 -，最长 40 个字符")
    return {"name": name, "title": meta.get("title") or name, "description": description[:500]}


def _members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The zip's files, refusing anything that could land outside its folder or is not a plain file."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ENTRIES:
        raise SkillFileError(f"压缩包里文件太多（{len(infos)} 个，最多 {MAX_ENTRIES} 个）")
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise SkillFileError(f"解压后超过 {MAX_UNPACKED // 1024 // 1024} MB")
    for i in infos:
        path = PurePosixPath(i.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in i.filename:
            raise SkillFileError(f"压缩包里有不安全的路径：{i.filename}")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise SkillFileError(f"压缩包里不能有符号链接：{i.filename}")
    return infos


def _skill_root(infos: list[zipfile.ZipInfo]) -> str:
    """'' when SKILL.md is at the root, 'folder/' when the zip wraps everything in one folder."""
    names = {i.filename for i in infos}
    if "SKILL.md" in names:
        return ""
    tops = {PurePosixPath(n).parts[0] for n in names}
    if len(tops) == 1 and f"{next(iter(tops))}/SKILL.md" in names:
        return f"{next(iter(tops))}/"
    raise SkillFileError("压缩包的根目录（或唯一的文件夹）里没有 SKILL.md")


def inspect(data: bytes, filename: str) -> dict | None:
    """The skill a file holds, None if it is not one. Raises SkillFileError if it tries to be one and fails."""
    lower = filename.lower()
    if lower.endswith(".md"):
        text = data[:MAX_SKILL_MD].decode("utf-8", "replace")
        fm = frontmatter(text)
        if "name" not in fm and "description" not in fm:
            return None  # an ordinary markdown file
        if len(data) > MAX_SKILL_MD:
            raise SkillFileError(f"SKILL.md 超过 {MAX_SKILL_MD // 1024} KB")
        return {**_describe(text), "kind": "md", "files": 1}
    if lower.endswith(".zip"):
        if len(data) > MAX_ZIP:
            raise SkillFileError(f"技能压缩包超过 {MAX_ZIP // 1024 // 1024} MB")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return None
        with zf:
            names = [i.filename for i in zf.infolist()]
            if not any(PurePosixPath(n).name == "SKILL.md" for n in names):
                return None  # an ordinary zip
            infos = _members(zf)
            root = _skill_root(infos)
            md = zf.getinfo(root + "SKILL.md")
            if md.file_size > MAX_SKILL_MD:
                raise SkillFileError(f"SKILL.md 超过 {MAX_SKILL_MD // 1024} KB")
            return {**_describe(zf.read(md).decode("utf-8", "replace")), "kind": "zip", "files": len(infos)}
    return None


def unpack(data: bytes, filename: str) -> tuple[str, dict[str, bytes]]:
    """(SKILL.md text, {relative path: bytes}) of a skill file, checked again: never trust the stored meta."""
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = _members(zf)
            root = _skill_root(infos)
            files = {i.filename[len(root) :]: zf.read(i) for i in infos if i.filename.startswith(root)}
        return files["SKILL.md"].decode("utf-8", "replace"), files
    text = data.decode("utf-8", "replace")
    return text, {"SKILL.md": data}
