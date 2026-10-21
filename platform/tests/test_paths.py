import pytest

from ailab_platform.paths import OUTPUTS, PathError, normalize_path


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/workspace", "/workspace"),
        ("/workspace/a.txt", "/workspace/a.txt"),
        ("/workspace/./sub//a.txt", "/workspace/sub/a.txt"),
        ("/workspace/sub/../a.txt", "/workspace/a.txt"),
    ],
)
def test_accepts_paths_inside_workspace(path, expected) -> None:
    assert normalize_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "",
        "workspace/a",
        "/workspace-evil/a",
        "/workspaceX",
        "/workspace/../etc/passwd",
        "/etc/passwd",
        "//workspace/a",
        "/workspace/a\x00b",
        "/workspace/../workspace-evil",
    ],
)
def test_rejects_paths_outside_workspace(path) -> None:
    with pytest.raises(PathError):
        normalize_path(path)


def test_outputs_scope() -> None:
    assert normalize_path("/workspace/outputs/x.pptx", OUTPUTS) == "/workspace/outputs/x.pptx"
    with pytest.raises(PathError):
        normalize_path("/workspace/outline.json", OUTPUTS)
    with pytest.raises(PathError):
        normalize_path("/workspace/outputs/../secret", OUTPUTS)
