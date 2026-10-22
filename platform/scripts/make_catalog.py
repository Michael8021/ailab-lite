"""Model catalog helper for deploy.sh (standard library only: runs on the host, not in the platform venv).

make_catalog.py custom OUT --base-url URL --name UPSTREAM_MODEL [--label 显示名] [--key-env AILAB_KEY_MAIN] [--no-tools]
    write a catalog with that one model (the default) plus the offline demo model
make_catalog.py check FILE          validate a catalog; prints the model ids
make_catalog.py key-env FILE        print the api_key_env of the default model ("" if it needs no key)
make_catalog.py default FILE        print the id of the default model
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from urllib.parse import urlparse

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KEY_RE = re.compile(r"^AILAB_KEY_[A-Z0-9_]+$")
DEMO = """
[[models]]
id = "ailab-demo"
name = "离线演示"
description = "不调用任何模型，按固定脚本演示完整链路；不花 token，可离线使用。"
provider = "mock"
weight = 0.1
"""


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def model_id(name: str) -> str:
    """A catalog id from the upstream model name ("Qwen/Qwen3-32B" -> "qwen3-32b")."""
    mid = re.sub(r"[^a-z0-9._-]+", "-", name.split("/")[-1].lower()).strip("-._")[:64]
    return mid if ID_RE.match(mid) else "main-model"


def custom(args: argparse.Namespace) -> None:
    url = urlparse(args.base_url)
    if url.scheme not in ("http", "https") or not url.netloc:
        sys.exit(f"--model-base-url must be an http(s) URL, got {args.base_url!r}")
    if args.base_url.rstrip("/").endswith("/chat/completions"):
        sys.exit("--model-base-url is the part before /chat/completions (e.g. https://api.example.com/v1)")
    if args.key_env and not KEY_RE.match(args.key_env):
        sys.exit("--key-env must look like AILAB_KEY_SOMETHING")
    lines = [
        "# Written by deploy.sh (--model-base-url ...). Edit it, or replace it with --models-file; platform/models.toml",
        "# documents every field. This file is not in git: it describes this deployment only.",
        "",
        "[[models]]",
        f"id = {_toml_str(model_id(args.name))}",
        f"name = {_toml_str(args.label or args.name)}",
        f"description = {_toml_str(args.description or '平台接入的模型')}",
        'provider = "openai"',
        f"base_url = {_toml_str(args.base_url.rstrip('/'))}",
        f"upstream_model = {_toml_str(args.name)}",
    ]
    if args.key_env:
        lines.append(f"api_key_env = {_toml_str(args.key_env)}")
    if args.no_tools:
        lines.append("tools = false  # chat only: no files, no sandbox")
    lines += ["weight = 1", "default = true", DEMO]
    text = "\n".join(lines)
    tomllib.loads(text)  # never write something the platform cannot read
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(model_id(args.name))


def load(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    models = data.get("models")
    if not isinstance(models, list) or not models:
        sys.exit(f"{path}: needs at least one [[models]] entry")
    ids = [str(m.get("id", "")) for m in models]
    bad = [i for i in ids if not ID_RE.match(i)]
    if bad or len(set(ids)) != len(ids):
        sys.exit(f"{path}: model ids must be unique and use letters, digits . _ - (got {ids})")
    for m in models:
        env = m.get("api_key_env")
        if env and not KEY_RE.match(env):
            sys.exit(f"{path}: api_key_env of {m['id']} must look like AILAB_KEY_SOMETHING")
        if m.get("provider", "openai") == "openai" and not m.get("base_url"):
            sys.exit(f"{path}: {m['id']} needs a base_url")
    return models


def default(models: list[dict]) -> dict:
    return next((m for m in models if m.get("default")), models[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("custom")
    c.add_argument("out")
    c.add_argument("--base-url", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--label", default="")
    c.add_argument("--description", default="")
    c.add_argument("--key-env", default="")
    c.add_argument("--no-tools", action="store_true")
    for name in ("check", "key-env", "default"):
        sub.add_parser(name).add_argument("file")
    args = parser.parse_args()
    if args.cmd == "custom":
        custom(args)
        return
    models = load(args.file)
    if args.cmd == "check":
        print(" ".join(m["id"] for m in models))
    elif args.cmd == "key-env":
        print(default(models).get("api_key_env", ""))
    else:
        print(default(models)["id"])


if __name__ == "__main__":
    main()
