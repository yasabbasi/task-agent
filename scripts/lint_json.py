"""Fail when a JSON file in the repo is not valid JSON with a two-space indent."""

import json
import sys
from pathlib import Path

DOT_FOLDERS = {".github", ".claude"}


def checked(path: Path) -> bool:
    return all(part in DOT_FOLDERS or not part.startswith(".") for part in path.parts[:-1])


def main() -> int:
    bad = []
    for path in sorted(p for p in Path(".").rglob("*.json") if checked(p)):
        text = path.read_text()
        try:
            formatted = json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
        except json.JSONDecodeError:
            formatted = ""
        if text != formatted:
            bad.append(path)
            print(f"{path}: not valid JSON with a two-space indent")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
