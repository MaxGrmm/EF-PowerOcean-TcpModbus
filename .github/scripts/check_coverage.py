"""Fail when any integration module is below the coverage Home Assistant asks for.

The test-coverage rule of the quality scale counts every module, so the total
alone could hide one that fell behind. Reads the report `pytest --cov-report=json`
writes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MINIMUM = 95.0


def main(report: Path) -> int:
    files = json.loads(report.read_text())["files"]
    below = {
        name: data["summary"]["percent_covered"]
        for name, data in sorted(files.items())
        if data["summary"]["percent_covered"] < MINIMUM
    }
    for name, percent in below.items():
        print(f"{name}: {percent:.1f}% (minimum {MINIMUM:.0f}%)")
    if not below:
        print(f"Every module is covered to at least {MINIMUM:.0f}%.")
    return 1 if below else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")))
