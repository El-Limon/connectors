"""SHA256SUMS in GNU coreutils format, sorted by file name."""

from __future__ import annotations

from pathlib import Path

from .. import net


def write_checksums(out: Path, files: list[Path]) -> Path:
    path = out / "SHA256SUMS"
    lines = [f"{net.sha256_file(file)}  {file.name}" for file in sorted(files, key=lambda p: p.name)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
