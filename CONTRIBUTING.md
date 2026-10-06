# Contributing to EF-PowerOcean-TcpModbus

Thank you for helping improve this Home Assistant custom integration. Bug reports, device testing, documentation fixes, translations, and code contributions are all welcome.

## Before You Start

- Search the [existing issues](https://github.com/MaxGrmm/EF-PowerOcean-TcpModbus/issues) before opening a new one.
- Use the appropriate issue template. Bug reports must include the requested device, firmware, Home Assistant, and integration versions.
- For a significant behavior change or a new writable register, open an issue first. Modbus writes can change how the inverter operates, so agreeing on the behavior and evidence before implementation saves review time.
- Never publish credentials, a complete serial number, a private IP address, or an unreviewed diagnostics file. Home Assistant diagnostics redact known identifiers, but check the resulting file before attaching it.

## Development Setup

If you do not have write access, fork the repository on GitHub and clone you fork locally.

The project and CI use Python 3.14, which Home Assistant requires. You may install Python using any method you prefer. [`uv`](https://docs.astral.sh/uv/getting-started/installation/) is recommended for creating the development environment and running project commands:

```shell
git clone https://github.com/YOUR-USERNAME/EF-PowerOcean-TcpModbus.git
cd EF-PowerOcean-TcpModbus
uv venv --python 3.14
uv pip install -r requirements-development.txt
uv run pre-commit install
```

Create a branch from the latest `main` and keep it focused on one fix or feature.

The tests run against a real Home Assistant installed by [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component), which provides the `hass` fixture and `MockConfigEntry`. CI runs them against the version pinned in [requirements-test.txt](requirements-test.txt) and against the oldest Home Assistant release allowed by [hacs.json](hacs.json). When raising that minimum, update both `hacs.json` and the matching `pytest-homeassistant-custom-component` pin in the workflow. For manual end-to-end testing, link or copy `custom_components/ef_powerocean_tcpmodbus` into the `custom_components` directory in Home Assistant.

## Run the Checks

Run the same core checks used by CI:

```shell
uv run pytest
uv run pre-commit run --all-files
```

Pre-commit runs Ruff checks and formatting, Prettier for Markdown, and basic YAML, JSON, whitespace, and line-ending checks. It's recommended to have pre-commit installed (see above) so that it runs automatically on commit. This is a required step in CI and it will fail on any deviaton. Pull requests are also validated by HACS and hassfest.

All checks should pass before requesting review.

## Releases

Keep unreleased user-facing changes under the `Unreleased` heading in [CHANGELOG.md](CHANGELOG.md). When it is time to publish, run the `Prepare Release` workflow from the Actions tab and enter the version without a `v` prefix. It verifies that the Unreleased section is not empty, moves it to a dated version heading, updates the integration manifest, and opens a release pull request.

After that pull request is merged, the `Publish Release` workflow creates the matching `v<version>` tag and GitHub release. The release description starts with that version's changelog section, followed by GitHub's generated release notes. Versions containing a hyphen, such as `2.6.0-beta.7`, are published as prereleases.

## Architecture

The integration separates

- Modbus transport
- Register decoding
- Polling and persistence
- Energy processing
- Control decisions
- Home Assistant

Keep changes within the layer that owns the behavior and place tests alongside the corresponding behavior in the test suite. Follow existing nearby code when deciding where new functionality belongs.

## Coding Guidelines

- Follow the existing style and Home Assistant async patterns.
- Add type hints to new and changed functions.
- Use module loggers (`logging.getLogger(__name__)`) and include actionable context. Do not log secrets or full device identifiers.
- Add or update focused tests for every behavior change and bug fix.
- Avoid unrelated refactoring in the same pull request.
- Do not add a dependency when the standard library or an existing dependency is sufficient. Any new runtime dependency must also be declared in [manifest.json](custom_components/ef_powerocean_tcpmodbus/manifest.json).

### Registers and Telemetry

Register changes need stronger evidence than a field name or a successful read:

- Read the [technical details](README.md#technical-details) and [protocol notes](EcoFlow_PowerOcean_Modbus.md) before changing register handling.
- Add decoding and block-planning tests where the change affects either behavior.
- Treat missing or invalid telemetry as unavailable. Do not replace it with zero, especially for `total_increasing` energy sensors, because that can corrupt Home Assistant long-term statistics.
- Use synthetic values and placeholder identifiers in tests and fixtures.

### Register Scan

[scripts/register_scan.py](scripts/register_scan.py) compares a live inverter against the register map in [const.py](custom_components/ef_powerocean_tcpmodbus/const.py) and prints a report to attach to an issue. It is read-only: it never writes a register and never takes Modbus control away from the EcoFlow app. The map, the decoding and the block planning are imported from the integration, so the report cannot drift from the code.

When requesting support for a new inverter model, this is the script that should be run to compare the registers.

To run it:

```shell
uv pip install -r requirements-development.txt
uv run python scripts/register_scan.py <inverter_ip>
```

Reading the result:

- Every register reading zero usually means Modbus TCP is switched off in the EcoFlow app, not that the map differs.
- A refused register that is readable elsewhere means the address moved on that model. Add an `address_overrides` entry to its `RegisterDef` rather than changing the shared address.
- A `maybe ...` in the `looks like` column is only a guess from the value's magnitude. It's an educated guess, but can be wrong. Use it as a reference together with the [protocol notes](EcoFlow_PowerOcean_Modbus.md).

### Control Test

[scripts/control_feature_scan.py](scripts/control_feature_scan.py) checks which Modbus controls a live inverter follows. Like the register scan it imports the map, the model traits and the control constants from the integration, and it sends commands the way the control manager does: the heartbeat first, then the setpoint, then the control word.

Unlike the scan, it **writes**. It runs in two parts:

1. **Without the heartbeat**, the way the integration writes them: battery saver, LED brightness and backup reserve are each changed to another value, confirmed by you in the EcoFlow app, and changed back. A register read-back only proves the words arrived, so the app answer is what counts.
2. **With the heartbeat**, the three control methods of the EcoFlow Open Modbus Protocol, each both ways at the test power: battery (40571), system power at the meter (40542) and inverter power (40544). Each is judged on whether the measured power reaches the setpoint and whether System Status (40530, bits 7-10) reports the method that was sent. The integration's own modes, such as Hold and Export Solar First, are built on these and not tested separately. Last, it exports with the system method and switches the export off and on again the way the grid feed switch does (limited mode and a 0 W cap, then the cap and mode put back), checking on the meter that the export stops and resumes. The feed-in settings are never changed otherwise.

It also learns the 32-bit write word order from one setpoint write before anything else, and watches the inverter hand back to the app at the end. Ctrl+C at any point puts back the setpoints and any setting it was in the middle of checking.

Turn off Modbus Control in the integration first and wait a minute; the script refuses to start while the inverter reports Modbus control. Have the EcoFlow app open, then:

```shell
uv pip install -r requirements-development.txt
uv run python scripts/control_feature_scan.py <inverter_ip>
```

It takes 5 to 10 minutes. `--power` sets the test power (default 1500 W), `--skip-manual` leaves out the app checks and `--no-handback-wait` skips the final 80 s. A method counts as followed once the measurement has stayed within the integration's tolerance for three samples in a row, so the verdicts match what the Control Status sensor would show.

### Writable Registers and Battery Control

Writable changes require confirmation against real hardware. A write response or register readback alone is not proof that firmware applied a command; describe the observed physical or application behavior, inverter model, and firmware version in the pull request.

Keep Modbus control opt-in and fail-safe. New control paths must respect control authority, existing command safety checks, configured power limits, and state-of-charge guards. Tests must cover rejection and failure paths as well as successful writes.

### Battery Modes

Each Battery Mode is one entry in `MODES` in [plans.py](custom_components/ef_powerocean_tcpmodbus/plans.py), saying what it runs depending on the solar surplus. The control manager carries out every mode by the same rules, guards included, so a new mode should not need changes there. Besides its entry, a new mode needs:

- a `ControlFeature` value in [models.py](custom_components/ef_powerocean_tcpmodbus/models.py) and a `CONTROL_FEATURES` entry in [const.py](custom_components/ef_powerocean_tcpmodbus/const.py), with a `default_power` if it has a power setting;
- its name in the translations and in the `mode` options of [services.yaml](custom_components/ef_powerocean_tcpmodbus/services.yaml);
- a row in each outcome table in [test_control_behaviour.py](tests/test_control_behaviour.py), and in the mode table in [README.md](README.md).

### Entities and Translations

When adding or changing an entity:

- Update the entity definition and its platform implementation as needed.
- Keep [strings.json](custom_components/ef_powerocean_tcpmodbus/strings.json), [translations/en.json](custom_components/ef_powerocean_tcpmodbus/translations/en.json), and [translations/de.json](custom_components/ef_powerocean_tcpmodbus/translations/de.json) in sync.
- Add state translations for enum entities.
- Update the entity tables or behavior documentation in [README.md](README.md).
- Preserve entity keys and unique IDs unless a migration is included. Changing them can orphan existing entities and dashboard references.

## Pull Requests

Push your branch to your fork and open a pull request against this repository's `main` branch. This project follows the standard fork-and-pull-request workflow in [GitHub's contribution guide](https://docs.github.com/en/get-started/exploring-projects-on-github/contributing-to-a-project). Before opening a pull request:

- Update your branch from `main` if it conflicts or is materially outdated.
- Explain the problem, the chosen solution, and how you verified it.
- Link the related issue.
- Include tests for changed behavior.
- Update [CHANGELOG.md](CHANGELOG.md) under the appropriate category.
- Update [README.md](README.md) when setup, entities, or user-visible behavior changes.
- Ensure all required GitHub Actions checks pass, including tests, pre-commit,
  HACS validation, and hassfest.

Keep review discussions technical. Avoid rewriting reviewed history unless
requested or agreed with reviewers.
