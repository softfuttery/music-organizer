"""Uncached, race-aware regular-file content comparison."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def stable_regular_files_equal(
    first: Path,
    second: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> bool:
    """Compare stable regular files without ``filecmp``'s process-wide cache."""

    def descriptor_fingerprint(
        value: os.stat_result,
    ) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def path_matches_descriptor(
        path_value: os.stat_result,
        descriptor_value: os.stat_result,
    ) -> bool:
        # Windows can expose a different ctime through stat() and fstat() for
        # the same open file, so path identity intentionally excludes ctime.
        return (
            path_value.st_dev,
            path_value.st_ino,
            path_value.st_size,
            path_value.st_mtime_ns,
        ) == (
            descriptor_value.st_dev,
            descriptor_value.st_ino,
            descriptor_value.st_size,
            descriptor_value.st_mtime_ns,
        )

    chunk_size = max(int(chunk_size), 1)
    try:
        first_before = first.stat(follow_symlinks=False)
        second_before = second.stat(follow_symlinks=False)
        if not stat.S_ISREG(first_before.st_mode) or not stat.S_ISREG(
            second_before.st_mode
        ):
            return False
        if first_before.st_size != second_before.st_size:
            return False

        with first.open("rb") as first_handle, second.open("rb") as second_handle:
            first_descriptor_before = os.fstat(first_handle.fileno())
            second_descriptor_before = os.fstat(second_handle.fileno())
            while True:
                first_chunk = first_handle.read(chunk_size)
                second_chunk = second_handle.read(chunk_size)
                if first_chunk != second_chunk:
                    return False
                if not first_chunk:
                    break
            first_descriptor_after = os.fstat(first_handle.fileno())
            second_descriptor_after = os.fstat(second_handle.fileno())

        first_after = first.stat(follow_symlinks=False)
        second_after = second.stat(follow_symlinks=False)
    except (OSError, ValueError):
        return False

    return (
        path_matches_descriptor(first_before, first_descriptor_before)
        and path_matches_descriptor(first_after, first_descriptor_after)
        and descriptor_fingerprint(first_descriptor_before)
        == descriptor_fingerprint(first_descriptor_after)
        and path_matches_descriptor(second_before, second_descriptor_before)
        and path_matches_descriptor(second_after, second_descriptor_after)
        and descriptor_fingerprint(second_descriptor_before)
        == descriptor_fingerprint(second_descriptor_after)
    )
