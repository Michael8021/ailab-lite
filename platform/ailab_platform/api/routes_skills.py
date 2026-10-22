"""Skills: 通用技能 (built in, everyone) and 个人技能 (a user's own skill files in their library).

Each user switches any of them on and off (like Claude's Customize > Skills). A skill that is off is not listed
in the model's system prompt, cannot be read with load_skill, has no button in the composer, and is refused
when a message asks for it. A personal skill is just a file: upload it to the library and the Worker recognises
it (see skillfiles.py); delete the file and the skill is gone.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.prompts import skills
from ..deps import current_user, get_db
from ..locks import user_critical_section
from ..models import FileObject, FileStatus, User

router = APIRouter(prefix="/v1/skills", tags=["skills"])


class SkillToggle(BaseModel):
    enabled: bool


def disabled_skills(user: User) -> list[str]:
    return list((user.settings or {}).get("disabled_skills") or [])


def personal_skills(db: Session, user: User) -> list[dict]:
    """The user's skill files, newest first; `error` says why one cannot be used (bad file, name taken)."""
    builtin = skills()
    files = db.scalars(
        select(FileObject)
        .where(FileObject.user_id == user.id, FileObject.status == FileStatus.READY)
        .order_by(FileObject.created_at.desc())
    )
    out, seen = [], set()
    for f in files:
        meta = f.meta or {}
        if not (meta.get("skill") or meta.get("skill_error")):
            continue
        item = {"file_id": f.id, "filename": f.filename, "created_at": f.created_at, **(meta.get("skill") or {})}
        if meta.get("skill_error"):
            item["error"] = meta["skill_error"]
        elif item["name"] in builtin:
            item["error"] = f"与通用技能 {item['name']} 重名，请在 SKILL.md 里换个 name"
        elif item["name"] in seen:
            item["error"] = "有更新的同名技能文件，这一份不再使用"
        else:
            seen.add(item["name"])
        out.append(item)
    return out


def usable_personal(db: Session, user: User) -> dict[str, dict]:
    """name -> skill for the personal skills that work and are switched on."""
    off = set(disabled_skills(user))
    return {s["name"]: s for s in personal_skills(db, user) if not s.get("error") and s["name"] not in off}


@router.get("")
def list_skills(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    off = disabled_skills(user)
    builtin = [
        {"name": n, "title": m["title"], "description": m["description"], "scope": "builtin", "builtin": True, "enabled": n not in off}
        for n, m in skills().items()
    ]
    personal = [
        {
            "name": s.get("name", ""),
            "title": s.get("title") or s["filename"],
            "description": s.get("description", ""),
            "scope": "personal",
            "builtin": False,
            "enabled": not s.get("error") and s.get("name") not in off,
            "file_id": s["file_id"],
            "filename": s["filename"],
            "kind": s.get("kind"),
            "files": s.get("files"),
            "error": s.get("error"),
        }
        for s in personal_skills(db, user)
    ]
    return builtin + personal


@router.put("/{name}")
def toggle_skill(name: str, body: SkillToggle, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    builtin = skills()
    personal = {s["name"]: s for s in personal_skills(db, user) if not s.get("error")}
    if name not in builtin and name not in personal:
        raise HTTPException(status_code=404, detail=f"unknown skill: {name}")
    known = set(builtin) | set(personal)
    with user_critical_section(db, user.id):
        db.refresh(user)  # current_user was loaded before the lock: another request may have changed settings
        off = [s for s in disabled_skills(user) if s in known and s != name]
        if not body.enabled:
            off.append(name)
        user.settings = {**(user.settings or {}), "disabled_skills": sorted(off)}  # a new dict: SQLAlchemy sees the change
        db.commit()
    return {"name": name, "enabled": body.enabled, "scope": "builtin" if name in builtin else "personal"}
