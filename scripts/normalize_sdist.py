"""Normalize a source distribution archive for a controlled reproducible build."""

from __future__ import annotations

import argparse
import copy
import gzip
import os
import tarfile
from pathlib import Path


def normalize_sdist(path: Path, source_date_epoch: int) -> None:
    """Rewrite an sdist with deterministic tar and gzip metadata."""
    temporary_path = path.with_suffix(".normalized.tar.gz")
    with (
        tarfile.open(path, mode="r:gz") as source,
        temporary_path.open("wb") as destination,
        gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=destination,
            mtime=source_date_epoch,
        ) as gzip_output,
        tarfile.open(fileobj=gzip_output, mode="w") as target,
    ):
        for member in sorted(source.getmembers(), key=lambda candidate: candidate.name):
            normalized = copy.copy(member)
            normalized.mtime = source_date_epoch
            normalized.uid = 0
            normalized.gid = 0
            normalized.uname = ""
            normalized.gname = ""
            normalized.pax_headers = {}
            if normalized.isdir():
                normalized.mode = 0o755
            elif normalized.isfile():
                normalized.mode = 0o644
            elif normalized.issym():
                normalized.mode = 0o777
            target.addfile(
                normalized,
                source.extractfile(member) if member.isfile() else None,
            )
    temporary_path.replace(path)


def main() -> None:
    """Normalize the provided source distribution using the build epoch."""
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    normalize_sdist(args.path, int(os.environ.get("SOURCE_DATE_EPOCH", "0")))


if __name__ == "__main__":
    main()
