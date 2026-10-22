"""Scripts that the Broker copies into a sandbox and runs there (they use the sandbox image's libraries)."""

from pathlib import Path

RENDER_PREVIEW = Path(__file__).with_name("render_preview.py")
