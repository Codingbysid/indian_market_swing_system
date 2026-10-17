"""Confirm the v5 candidate zip does not ship sklearn or xgboost."""

from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    banned = []
    with zipfile.ZipFile(destination, "w") as archive:
        for folder in ("swing_v5", "runtime_v5"):
            for path in (ROOT / folder).rglob("*.py"):
                archive.write(path, path.relative_to(ROOT).as_posix())
    names = zipfile.ZipFile(destination).namelist()
    for name in names:
        if "sklearn" in name or "xgboost" in name:
            banned.append(name)
    return {"files": len(names), "banned": banned, "timeout_seconds": 300, "memory_mb": 1024, "deployed": False}


if __name__ == "__main__":
    print(build(ROOT / "dist" / "v5_candidate.zip"))
