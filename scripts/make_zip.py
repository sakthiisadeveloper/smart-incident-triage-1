"""Package the repository for submission (python scripts/make_zip.py -> ../smart-incident-triage.zip)."""

from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".local-data", "triage_common.egg-info"}
SKIP_FILES = {".env"}


def main() -> None:
    target = ROOT.parent / f"{ROOT.name}.zip"
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if any(part in SKIP_DIRS for part in rel.parts) or path.name in SKIP_FILES or path.suffix in (".db", ".pyc"):
                continue
            if path.is_file():
                zf.write(path, Path(ROOT.name) / rel)
                count += 1
    print(f"wrote {target} ({count} files)")


if __name__ == "__main__":
    main()
