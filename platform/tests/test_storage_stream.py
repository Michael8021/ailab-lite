from __future__ import annotations

from ailab_platform.storage import LocalStorage


def test_local_storage_streams_a_file_without_loading_it(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdef" * 1000)

    storage.put_file("objects/a.bin", source, "application/octet-stream")
    assert storage.size("objects/a.bin") == 6000
    assert b"".join(storage.stream("objects/a.bin", chunk_size=7)) == source.read_bytes()
    assert storage.get_bytes("objects/a.bin") == source.read_bytes()
