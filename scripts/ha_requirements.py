"""Print the requirements this integration gets from the installed Home Assistant.

Users run us with whatever Home Assistant installs for our own requirements and for
the integrations we depend on (pymodbus and modbus-connection via modbus, today).
Tests should run against exactly those versions, so CI installs this output
rather than pins of our own.

Usage: python -m pip install $(python scripts/ha_requirements.py)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWN_MANIFEST = ROOT / "custom_components" / "ef_powerocean_tcpmodbus" / "manifest.json"


def _components_dir() -> Path:
    # Locate without importing: importing a component needs the very
    # requirements we are about to install.
    spec = importlib.util.find_spec("homeassistant.components")
    if spec is None or not spec.submodule_search_locations:
        sys.exit("Home Assistant is not installed")
    return Path(spec.submodule_search_locations[0])


def collect() -> list[str]:
    components = _components_dir()
    own = json.loads(OWN_MANIFEST.read_text())
    requirements: list[str] = list(own.get("requirements", []))

    # Integrations we need loaded, followed through their own hard dependencies.
    pending = [*own.get("dependencies", []), *own.get("after_dependencies", [])]
    seen: set[str] = set()
    while pending:
        domain = pending.pop()
        if domain in seen:
            continue
        seen.add(domain)
        manifest_path = components / domain / "manifest.json"
        if not manifest_path.is_file():
            continue  # Not shipped by this Home Assistant version.
        manifest = json.loads(manifest_path.read_text())
        requirements.extend(manifest.get("requirements", []))
        pending.extend(manifest.get("dependencies", []))

    return list(dict.fromkeys(requirements))


if __name__ == "__main__":
    print("\n".join(collect()))
