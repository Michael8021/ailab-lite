"""Exercise the OpenSandbox provider without the rest of the platform."""

from __future__ import annotations

from ailab_platform.sandbox.opensandbox import OpenSandboxProvider


def main() -> None:
    provider = OpenSandboxProvider()
    print("health", provider.health())
    sandbox_id = provider.acquire("lease_probe", "harbor.ailab.local/sandbox/sbx-office:lite")
    print("sandbox", sandbox_id)
    try:
        result = provider.exec(sandbox_id, "id; python -c 'import pptx; print(pptx.__version__)'", 60)
        print("exec", result.exit_code, result.stdout, result.stderr)
        provider.put_file(sandbox_id, "/workspace/outputs/probe.txt", b"hello opensandbox")
        print("read", provider.get_file(sandbox_id, "/workspace/outputs/probe.txt"))
        print("list", provider.list_files(sandbox_id, "/workspace/outputs"))
    finally:
        provider.release(sandbox_id)
        print("released")


if __name__ == "__main__":
    main()
