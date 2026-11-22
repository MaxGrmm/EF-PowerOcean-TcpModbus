"""Write a contents list of a Markdown file's second-level headings.

The list goes between a `<!-- toc -->` and a `<!-- tocstop -->` line, and files
without them are left alone. Anchors follow GitHub's rules, so the links work on
the repository page. Exits 1 when a file changed, as pre-commit fixers do.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

START = "<!-- toc -->"
END = "<!-- tocstop -->"
TITLE = "## Contents"
HEADING = re.compile(r"^## (?P<text>.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def slug(text: str, seen: dict[str, int]) -> str:
    """Return GitHub's anchor for a heading, numbering repeats as it does."""
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).replace("`", "")
    base = re.sub(r"[^\w\- ]", "", plain.strip().lower()).replace(" ", "-")
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def contents(lines: list[str]) -> list[str]:
    """List the second-level headings outside the contents block and code."""
    entries: list[str] = []
    seen: dict[str, int] = {}
    in_fence = in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == START:
            in_block = True
        elif stripped == END:
            in_block = False
        elif FENCE.match(line):
            in_fence = not in_fence
        elif not in_block and not in_fence and (match := HEADING.match(line)):
            text = match["text"]
            entries.append(f"- [{text}](#{slug(text, seen)})")
    return entries


def update(path: Path) -> bool:
    """Rewrite the contents block of one file; return whether it changed."""
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == START)
        end = next(
            i for i, line in enumerate(lines) if line.strip() == END and i > start
        )
    except StopIteration:
        return False
    block = [START, "", TITLE, "", *contents(lines), "", END]
    updated = "\n".join([*lines[:start], *block, *lines[end + 1 :]]) + "\n"
    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    print(f"Updated contents in {path}")
    return True


def main(paths: list[str]) -> int:
    changed = [update(Path(path)) for path in paths]
    return 1 if any(changed) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
