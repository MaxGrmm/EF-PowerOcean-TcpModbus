"""Print the newest Home Assistant version on PyPI, beta or not."""

import json
import urllib.request

from packaging.version import Version

URL = "https://pypi.org/pypi/homeassistant/json"

with urllib.request.urlopen(URL, timeout=30) as response:
    releases = json.load(response)["releases"]

# Skip versions whose every file was yanked.
available = [v for v, files in releases.items() if any(not f["yanked"] for f in files)]
print(max(available, key=Version))
