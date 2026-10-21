"""Minimal OpenSandbox SDK probe against the local dev server."""

from __future__ import annotations

from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.config import ConnectionConfigSync

IMAGE = "harbor.ailab.local/sandbox/sbx-office:lite"


def main() -> None:
    config = ConnectionConfigSync(domain="127.0.0.1:8090", api_key="dev-osb-key", request_timeout=timedelta(seconds=120))
    sandbox = SandboxSync.create(IMAGE, connection_config=config, timeout=timedelta(minutes=5))
    print("sandbox", sandbox.id)
    try:
        execution = sandbox.commands.run("id; python -c 'import pptx; print(pptx.__version__)'; ls -la /workspace")
        print("exit", execution.exit_code)
        for line in execution.logs.stdout:
            print("OUT", line.text)
        for line in execution.logs.stderr:
            print("ERR", line.text)
    finally:
        sandbox.destroy()
        print("destroyed")


if __name__ == "__main__":
    main()
