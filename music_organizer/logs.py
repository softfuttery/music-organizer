"""Bounded log-file readers used by the web control plane."""

from pathlib import Path


def tail_lines(path: Path, limit: int, block_size: int = 8192) -> list[str]:
    if limit <= 0 or not path.is_file():
        return []
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and newline_count <= limit:
            size = min(block_size, position)
            position -= size
            handle.seek(position)
            chunk = handle.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]
