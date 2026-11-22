"""Write a contents list of a Markdown file's headings.

The list goes between a `<!-- toc -->` and a `<!-- tocstop -->` line, and files
without them are left alone. It lists the second-level headings, or the levels
down to N with `<!-- toc depth=N -->`, nested by level. Anchors follow GitHub's
rules, so the links work on the repository page. Exits 1 when a file changed, as
pre-commit fixers do.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

START = re.compile(r"^<!-- toc(?: depth=(?P<depth>[2-6]))? -->$")
END = "<!-- tocstop -->"
TITLE = "## Contents"
HEADING = re.compile(r"^(?P<level>#{1,6}) (?P<text>.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def slug(text: str, seen: dict[str, int]) -> str:
    """Return GitHub's anchor for a heading, numbering repeats as it does."""
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).replace("`", "")
    base = re.sub(r"[^\w\- ]", "", plain.strip().lower()).replace(" ", "-")
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def contents(lines: list[str], depth: int) -> list[str]:
    """List the headings outside the contents block and code, nested by level."""
    entries: list[str] = []
    seen: dict[str, int] = {}
    in_fence = in_block = False
    for line in lines:
        stripped = line.strip()
        if START.match(stripped):
            in_block = True
        elif stripped == END:
            in_block = False
        elif FENCE.match(line):
            in_fence = not in_fence
        elif not in_block and not in_fence and (match := HEADING.match(line)):
            # GitHub numbers repeated anchors across every heading level.
            level, text = len(match["level"]), match["text"]
            anchor = slug(text, seen)
            if 2 <= level <= depth:
                indent = "  " * (level - 2)
                entries.append(f"{indent}- [{text}](#{anchor})")
    return entries


def update(path: Path) -> bool:
    """Rewrite the contents block of one file; return whether it changed."""
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    starts = [
        (i, match)
        for i, line in enumerate(lines)
        if (match := START.match(line.strip()))
    ]
    if not starts:
        return False
    start, match = starts[0]
    end = next(
        (i for i, line in enumerate(lines) if line.strip() == END and i > start),
        None,
    )
    if end is None:
        return False
    depth = int(match["depth"] or 2)
    block = [lines[start].strip(), "", TITLE, "", *contents(lines, depth), "", END]
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
