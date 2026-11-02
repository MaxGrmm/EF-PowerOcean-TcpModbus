"""Every error raised to the user has a message in every language."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

import pytest

from custom_components.ef_powerocean_tcpmodbus import const

INTEGRATION = Path(const.__file__).parent
LANGUAGES = sorted(path.stem for path in (INTEGRATION / "translations").glob("*.json"))
SOURCE = "\n".join(path.read_text() for path in INTEGRATION.glob("*.py"))

RAISED = (
    set(re.findall(r'translation_domain=DOMAIN,\s*translation_key="(\w+)"', SOURCE))
    | set(re.findall(r'block_writes\(\s*"(\w+)"', SOURCE))
    | set(re.findall(r'translation_key="(\w+)" if message is None', SOURCE))
)


def _messages(language: str) -> dict[str, str]:
    path = INTEGRATION / "translations" / f"{language}.json"
    exceptions = json.loads(path.read_text())["exceptions"]
    return {key: value["message"] for key, value in exceptions.items()}


def test_the_scan_finds_the_errors() -> None:
    assert {"not_connected", "write_rejected", "identity_mismatch"} <= RAISED


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_raised_error_has_a_message(language: str) -> None:
    assert RAISED - _messages(language).keys() == set()


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_language_uses_the_same_placeholders(language: str) -> None:
    def placeholders(message: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(message) if name}

    english = _messages("en")
    for key, message in _messages(language).items():
        assert placeholders(message) == placeholders(english[key]), key
