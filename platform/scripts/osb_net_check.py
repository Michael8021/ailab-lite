"""Check that a sandbox created through OpenSandbox cannot reach the internet."""

from __future__ import annotations

from ailab_platform.sandbox.opensandbox import OpenSandboxProvider

CHECK = "python -c \"import socket; socket.create_connection(('pypi.org', 443), 3); print('NET_OPEN')\""


def main() -> None:
    provider = OpenSandboxProvider()
    sandbox_id = provider.acquire("lease_net", "harbor.ailab.local/sandbox/sbx-office:lite")
    try:
        result = provider.exec(sandbox_id, CHECK, 30)
        print("exit", result.exit_code)
        print("stdout", result.stdout)
        print("stderr", result.stderr[-500:])
        print("BLOCKED" if "NET_OPEN" not in result.stdout else "OPEN")
    finally:
        provider.release(sandbox_id)


if __name__ == "__main__":
    main()
