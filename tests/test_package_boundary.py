"""The powerocean package stays free of Home Assistant."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ef_powerocean_tcpmodbus"
    / "powerocean"
)
MODULES = sorted(PACKAGE.glob("*.py"))


@pytest.mark.parametrize("module", MODULES, ids=lambda path: path.stem)
def test_imports_nothing_from_home_assistant_or_the_integration(
    module: Path,
) -> None:
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level <= 1, f"{module.name} imports from the integration"
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.split(".")[0] != "homeassistant", f"{module.name}: {name}"


def test_imports_without_home_assistant_installed() -> None:
    """Every module loads with Home Assistant made unimportable."""
    script = f"""
import importlib, sys, types
sys.modules["homeassistant"] = None
parent = types.ModuleType("powerocean_parent")
parent.__path__ = [{str(PACKAGE.parent)!r}]
sys.modules["powerocean_parent"] = parent
for name in {[module.stem for module in MODULES if module.stem != "__init__"]!r}:
    importlib.import_module(f"powerocean_parent.powerocean.{{name}}")
"""
    subprocess.run([sys.executable, "-c", script], check=True)
