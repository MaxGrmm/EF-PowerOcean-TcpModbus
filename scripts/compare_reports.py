#!/usr/bin/env python3
"""Compare two control test reports, typically before and after a firmware update.

    uv run python scripts/compare_reports.py <old report> <new report>

A report is any of: the JSON the run_control_test action saves under
config/ef_powerocean_tcpmodbus/, the integration's diagnostics download (its last
control test is taken), the action's response copied from Developer Tools as YAML,
or the file control_feature_scan.py --json writes.

Only behaviour that changed is listed. Lines marked ! are likely the firmware: a
method that is no longer followed, a method no longer reported, a ramp that got
much slower. Lines marked ~ are likely the conditions, such as a test skipped
because the battery was full; run again in other conditions to tell.

Exits 1 when anything significant changed, so it can gate a release.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from utils import core


def load(path: str) -> dict[str, Any]:
    """Return the report in *path*, from whichever form it was saved in."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        content = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # noqa: PLC0415 - only needed for a pasted response
        except ImportError:
            sys.exit(f"{path} is not JSON, and PyYAML is not installed to read it.")
        content = yaml.safe_load(text)

    if isinstance(content, dict) and "features" in content:
        return content
    # A diagnostics download keeps the last report under data.control_test.
    report = ((content or {}).get("data") or {}).get("control_test", {})
    if isinstance(report, dict) and (last := report.get("last_report")):
        return last
    sys.exit(f"{path} holds no control test report.")


def describe(report: dict[str, Any]) -> str:
    return (
        f"{report.get('model')} firmware {report.get('firmware_version')}, "
        f"{report.get('source')} {report.get('created_at', '')[:16]}, "
        f"{report.get('outcome')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("old", help="the report to compare against")
    parser.add_argument("new", help="the newer report")
    arguments = parser.parse_args()

    old, new = load(arguments.old), load(arguments.new)
    print(f"old: {describe(old)}")
    print(f"new: {describe(new)}")
    if old.get("model") != new.get("model"):
        print("Warning: the reports are from different models.")
    for report, name in ((old, "old"), (new, "new")):
        if report.get("schema_version") != core.REPORT_SCHEMA_VERSION:
            print(
                f"Warning: the {name} report has schema version "
                f"{report.get('schema_version')}, this script reads "
                f"{core.REPORT_SCHEMA_VERSION}."
            )
    print()

    differences = core.compare_reports(old, new)
    if not differences:
        print("No change in behaviour.")
        return 0
    for difference in differences:
        print(difference)
    return 1 if any(difference.significant for difference in differences) else 0


if __name__ == "__main__":
    sys.exit(main())
