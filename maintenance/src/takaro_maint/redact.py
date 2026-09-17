"""Keep credentials out of everything the command writes.

Every environment value whose name looks like a secret is collected once and replaced
with ``<redacted>`` in every stdout/stderr write and in every log line the harness keeps.
"""

from __future__ import annotations

import os
import re

_SECRET_NAME = re.compile(r"(TOKEN|PASSWORD|SECRET|APIKEY|API_KEY|PRIVATE_KEY)", re.IGNORECASE)
_MIN_SECRET_LEN = 6

PLACEHOLDER = "<redacted>"


def secret_values(environ: dict[str, str] | None = None) -> list[str]:
    """Environment values worth hiding, longest first so substrings cannot leak."""
    env = os.environ if environ is None else environ
    values = {value for name, value in env.items() if _SECRET_NAME.search(name) and len(value) >= _MIN_SECRET_LEN}
    return sorted(values, key=len, reverse=True)


def redact(text: str, extra: list[str] | None = None) -> str:
    """Replace every known secret value in ``text`` with the placeholder."""
    for value in secret_values():
        text = text.replace(value, PLACEHOLDER)
    for value in extra or []:
        if value and len(value) >= _MIN_SECRET_LEN:
            text = text.replace(value, PLACEHOLDER)
    return text
