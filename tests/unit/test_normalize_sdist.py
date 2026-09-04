from __future__ import annotations

import gzip
import importlib.util
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "normalize_sdist.py"


def load_module() -> object:
    """Load the standalone sdist normalizer as a testable module."""
    spec = importlib.util.spec_from_file_location("normalize_sdist", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load sdist normalizer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_sdist(path: Path, file_mode: int) -> None:
    """Create equivalent sdists that differ only in source file permissions."""
    root = "stock_profiler-0.1.0.dev0"
    contents = b"Apache-2.0\n"
    with (
        path.open("wb") as destination,
        gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=1) as gzip_output,
        tarfile.open(fileobj=gzip_output, mode="w") as archive,
    ):
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o775
        archive.addfile(directory)

        license_file = tarfile.TarInfo(f"{root}/LICENSE")
        license_file.mode = file_mode
        license_file.size = len(contents)
        archive.addfile(license_file, io.BytesIO(contents))


def test_normalize_sdist_ignores_source_file_permission_bits(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    write_sdist(first, 0o644)
    write_sdist(second, 0o664)

    module = load_module()
    module.normalize_sdist(first, 1_700_000_000)  # type: ignore[attr-defined]
    module.normalize_sdist(second, 1_700_000_000)  # type: ignore[attr-defined]

    assert first.read_bytes() == second.read_bytes()
