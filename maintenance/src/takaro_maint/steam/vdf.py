"""Valve KeyValues ("VDF") text, read and written with the standard library only.

``steamcmd +app_info_print`` is the only place Steam publishes what a branch currently
points at — the build id, and one content manifest per depot — and it publishes it as a
KeyValues document wrapped in console noise. Everything Steam discovery knows comes out
of that document, so it is parsed here rather than pattern-matched: a regex over the noise
would happily read a manifest id out of the wrong branch's block.

The grammar is small and this implementation covers exactly it: quoted and bare tokens,
``{``/``}`` blocks, ``//`` comments, tabs and CRLF. Duplicate keys keep the last value,
the way Valve's own reader does. Anything unbalanced is an error rather than a partial
document, because a truncated block is the known failure mode of ``app_info_print`` on a
non-TTY and it has to be distinguishable from "this branch has no manifests".
"""

from __future__ import annotations

import re
from typing import Any

#: One parsed document: values are either scalars (always strings in Valve's text format)
#: or nested documents.
Document = dict[str, Any]

_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
_WHITESPACE = " \t\r\n"
_DELIMITERS = ' \t\r\n"{}'

#: ``AppID : 294420, change number : 39026857/39026857, last change : Mon Sep 21 12:53:46 2026``
#: — printed before the block, and the only place the change number appears.
_HEADER_RE = re.compile(
    r"AppID\s*:\s*(?P<app>\d+)\s*,\s*change number\s*:\s*(?P<change>\d+)(?:/\d+)?\s*,\s*last change\s*:(?P<last>.*)$",
    re.MULTILINE,
)


class VdfError(ValueError):
    """The text is not the KeyValues document it was expected to be."""


class _Reader:
    """A cursor over the text. Kept explicit so a block can be parsed out of the middle."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def _skip(self) -> None:
        text, end = self.text, len(self.text)
        while self.pos < end:
            char = text[self.pos]
            if char in _WHITESPACE:
                self.pos += 1
            elif char == "/" and text.startswith("//", self.pos):
                newline = text.find("\n", self.pos)
                self.pos = end if newline < 0 else newline + 1
            else:
                return

    def peek(self) -> str | None:
        self._skip()
        return self.text[self.pos] if self.pos < len(self.text) else None

    def token(self, what: str) -> str:
        char = self.peek()
        if char is None:
            raise VdfError(f"unexpected end of document while reading {what}")
        if char in "{}":
            raise VdfError(f"unexpected {char!r} where {what} was expected")
        return self._quoted() if char == '"' else self._bare()

    def _quoted(self) -> str:
        self.pos += 1  # the opening quote
        text, end = self.text, len(self.text)
        parts: list[str] = []
        while True:
            if self.pos >= end:
                raise VdfError("unexpected end of document inside a quoted string")
            char = text[self.pos]
            if char == '"':
                self.pos += 1
                return "".join(parts)
            if char == "\\":
                self.pos += 1
                if self.pos >= end:
                    raise VdfError("unexpected end of document inside a quoted string")
                parts.append(_ESCAPES.get(text[self.pos], text[self.pos]))
                self.pos += 1
                continue
            parts.append(char)
            self.pos += 1

    def _bare(self) -> str:
        text, end = self.text, len(self.text)
        start = self.pos
        while self.pos < end and text[self.pos] not in _DELIMITERS:
            self.pos += 1
        return text[start : self.pos]

    def pair(self) -> tuple[str, str | Document]:
        key = self.token("a key")
        char = self.peek()
        if char is None or char == "}":
            raise VdfError(f"key {key!r} has no value")
        if char == "{":
            self.pos += 1
            return key, self.block(key)
        return key, self.token(f"the value of {key!r}")

    def block(self, key: str) -> Document:
        """Everything up to the matching ``}``. The last of two duplicate keys wins."""
        found: Document = {}
        while True:
            char = self.peek()
            if char is None:
                raise VdfError(f"unexpected end of document: the block of {key!r} is never closed")
            if char == "}":
                self.pos += 1
                return found
            name, value = self.pair()
            found[name] = value

    def document(self) -> Document:
        found: Document = {}
        while True:
            char = self.peek()
            if char is None:
                return found
            if char == "}":
                raise VdfError("unbalanced '}': a block is closed that was never opened")
            name, value = self.pair()
            found[name] = value


def parse(text: str) -> Document:
    """The whole text as nested dictionaries of strings."""
    return _Reader(text).document()


def _header(text: str, app: int) -> Document:
    """The ``AppID :`` line's change number, or an empty mapping when it was not printed."""
    for match in _HEADER_RE.finditer(text):
        if int(match.group("app")) != app:
            continue
        return {"changeNumber": int(match.group("change")), "lastChange": match.group("last").strip()}
    return {}


def _block_start(text: str, app: int) -> int | None:
    """Where the app's own block begins: the first line that is exactly ``"<app>"``."""
    needle = f'"{app}"'
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.strip() == needle:
            return offset
        offset += len(line)
    return None


def extract_app(text: str, app: int) -> tuple[Document, Document]:
    """``(the app's block, the header facts)`` out of a whole ``app_info_print`` capture.

    The console noise around the block is skipped by finding the app id on a line of its
    own, and parsing stops at the block's matching brace, so the ``Unloading Steam API``
    trailer is never fed to the parser.

    A capture with no block, or with a block carrying no ``depots.branches``, is the
    truncation ``app_info_print`` is documented to produce when it is not attached to a
    terminal. It is reported as such rather than returned as an app with no branches,
    because "Steam published nothing" and "we were handed half a document" must not look
    alike to the caller deciding whether to retry.
    """
    start = _block_start(text, app)
    if start is None:
        raise VdfError(f"app_info_print printed no block for {app}")
    key, value = _Reader(text[start:]).pair()
    if not isinstance(value, dict):
        raise VdfError(f"app_info_print printed no block for {app}: {key!r} is a scalar")
    depots = value.get("depots")
    branches = depots.get("branches") if isinstance(depots, dict) else None
    if not isinstance(branches, dict) or not branches:
        raise VdfError(f"app_info_print printed no branch data for {app}")
    return value, _header(text, app)


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def dump(document: Document) -> str:
    """The inverse of :func:`parse`: tabs, quoted strings, braces on their own lines.

    Steam's own spelling, so a fixture recorded from the real tool and one a test mutated
    and wrote back go through exactly the same parser on the way in.
    """
    lines: list[str] = []
    _dump_into(document, 0, lines)
    return "".join(lines)


def _dump_into(document: Document, depth: int, lines: list[str]) -> None:
    indent = "\t" * depth
    for key, value in document.items():
        if isinstance(value, dict):
            lines.append(f"{indent}{_quote(str(key))}\n{indent}{{\n")
            _dump_into(value, depth + 1, lines)
            lines.append(f"{indent}}}\n")
        else:
            lines.append(f"{indent}{_quote(str(key))}\t\t{_quote(str(value))}\n")
