"""The model catalog: which models users can choose, and how each one is reached.

Users select a catalog ``id``. Everything about the upstream (URL, upstream model name, the key)
lives here on the server and is never sent to a browser or accepted from a request.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import settings

log = logging.getLogger("ailab.catalog")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KEY_ENV_PREFIX = "AILAB_KEY_"  # so a typo cannot make the catalog send e.g. AILAB_SERVICE_TOKEN upstream
PROVIDERS = {"openai", "mock"}


class CatalogError(ValueError):
    pass


def secret(name: str) -> str:
    """A model key: the process environment first, then the .env file used on dev machines."""
    value = os.environ.get(name, "")
    if value:
        return value
    try:
        from dotenv import dotenv_values

        return (dotenv_values(settings.env_file_path()).get(name) or "").strip()
    except Exception:
        return ""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    description: str = ""
    provider: str = "openai"
    base_url: str = ""
    api_key_env: str = ""
    upstream_model: str = ""
    weight: float = 1.0
    tools: bool = True
    default: bool = False
    enabled: bool = True
    # Thinking models (DeepSeek V4.x) refuse the next turn of a tool conversation unless the previous
    # reasoning_content is sent back; others reject or ignore it. Hence a per-model switch.
    echo_reasoning: bool = False

    def api_key(self) -> str:
        return secret(self.api_key_env) if self.api_key_env else ""

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self.provider == "mock":
            return True
        return bool(self.base_url) and (not self.api_key_env or bool(self.api_key()))

    def public(self) -> dict:
        """What a user may see. Deliberately without base_url, upstream_model and anything key-related."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "weight": self.weight,
            "tools": self.tools,
        }


class Catalog:
    def __init__(self, models: list[ModelSpec]):
        self.models = {m.id: m for m in models}

    def get(self, model_id: str | None) -> ModelSpec | None:
        return self.models.get(model_id or "")

    def available(self) -> list[ModelSpec]:
        return [m for m in self.models.values() if m.available]

    def default_id(self) -> str | None:
        """The preselected model: the one marked default if usable, else the first usable one."""
        usable = self.available()
        for m in usable:
            if m.default:
                return m.id
        return usable[0].id if usable else None

    def weight_of(self, model_id: str) -> float:
        """Quota multiplier. Unknown ids (usage recorded before a model was renamed) count as 1."""
        spec = self.models.get(model_id)
        return spec.weight if spec else 1.0


def parse_catalog(data: dict) -> Catalog:
    raw = data.get("models")
    if not isinstance(raw, list) or not raw:
        raise CatalogError("the catalog needs at least one [[models]] entry")
    specs: list[ModelSpec] = []
    for i, item in enumerate(raw, 1):
        where = f"models[{i}]"
        unknown = set(item) - set(ModelSpec.__dataclass_fields__)
        if unknown:
            raise CatalogError(f"{where}: unknown keys {sorted(unknown)}")
        mid = str(item.get("id", ""))
        if not ID_RE.match(mid):
            raise CatalogError(f"{where}: bad id {mid!r} (letters, digits, '.', '_', '-')")
        if any(s.id == mid for s in specs):
            raise CatalogError(f"{where}: duplicate id {mid!r}")
        spec = ModelSpec(**{**item, "id": mid, "name": str(item.get("name") or mid)})
        if spec.provider not in PROVIDERS:
            raise CatalogError(f"{where} ({mid}): provider must be one of {sorted(PROVIDERS)}")
        if spec.provider == "openai" and not spec.base_url:
            raise CatalogError(f"{where} ({mid}): base_url is required for provider 'openai'")
        if spec.api_key_env and not spec.api_key_env.startswith(KEY_ENV_PREFIX):
            raise CatalogError(f"{where} ({mid}): api_key_env must start with {KEY_ENV_PREFIX}")
        if not spec.weight > 0:
            raise CatalogError(f"{where} ({mid}): weight must be > 0")
        specs.append(spec)
    if sum(1 for s in specs if s.default) > 1:
        raise CatalogError("more than one model is marked default")
    return Catalog(specs)


def load_catalog(path: Path | str) -> Catalog:
    try:
        with open(path, "rb") as fh:
            return parse_catalog(tomllib.load(fh))
    except FileNotFoundError as exc:
        raise CatalogError(f"model catalog not found: {path} (set AILAB_MODELS_FILE)") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"{path}: {exc}") from exc


@lru_cache
def get_catalog() -> Catalog:
    catalog = load_catalog(settings.models_path)
    for spec in catalog.models.values():
        if not spec.available:
            why = "disabled" if not spec.enabled else f"key {spec.api_key_env} is not set"
            log.warning("model %s is hidden from users: %s", spec.id, why)
    log.info("model catalog: %s (default %s)", [m.id for m in catalog.available()], catalog.default_id())
    return catalog


def reset_catalog() -> None:
    get_catalog.cache_clear()
