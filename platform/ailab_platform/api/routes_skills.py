"""Skills each user can switch on and off (like Claude's Customize > Skills).

A skill that is off is not listed in the model's system prompt, cannot be read with load_skill, has no
button in the composer, and is refused when a message asks for it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..agent.prompts import skills
from ..deps import current_user, get_db
from ..models import User

router = APIRouter(prefix="/v1/skills", tags=["skills"])


class SkillToggle(BaseModel):
    enabled: bool


def disabled_skills(user: User) -> list[str]:
    return list((user.settings or {}).get("disabled_skills") or [])


def _out(name: str, meta: dict, disabled: list[str]) -> dict:
    return {"name": name, "title": meta["title"], "description": meta["description"], "builtin": True, "enabled": name not in disabled}


@router.get("")
def list_skills(user: User = Depends(current_user)) -> list[dict]:
    off = disabled_skills(user)
    return [_out(name, meta, off) for name, meta in skills().items()]


@router.put("/{name}")
def toggle_skill(name: str, body: SkillToggle, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    available = skills()
    if name not in available:
        raise HTTPException(status_code=404, detail=f"unknown skill: {name}")
    off = [s for s in disabled_skills(user) if s in available and s != name]
    if not body.enabled:
        off.append(name)
    user.settings = {**(user.settings or {}), "disabled_skills": sorted(off)}  # a new dict: SQLAlchemy sees the change
    db.commit()
    return _out(name, available[name], off)
