#!/usr/bin/env python3
"""Merge Takaro dev-rig settings into a UE .ini, preserving every unknown key.

Usage: render-config.py <ini-path> <<< JSON
  JSON: {"[Section]": {"Key": "value" | ["v1","v2"]}}
A list value means "this key may repeat": every existing occurrence in that
section is dropped and the list is written instead. A string value replaces the
first occurrence (later duplicates are dropped) or is appended to the section.
Sections and keys not mentioned are left byte-identical.
"""
import json
import os
import sys


def main():
    path = sys.argv[1]
    desired = json.load(sys.stdin)

    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()

    out = []
    cur = None
    seen_sections = set()
    written = set()  # (section, key) already emitted

    def flush_section(section):
        """Append any desired keys of <section> that were not seen."""
        for key, val in desired.get(section, {}).items():
            if (section, key) in written:
                continue
            written.add((section, key))
            for v in (val if isinstance(val, list) else [val]):
                out.append("%s=%s" % (key, v))

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if cur is not None:
                flush_section(cur)
                if out and out[-1].strip():
                    out.append("")
            cur = stripped
            seen_sections.add(cur)
            out.append(line)
            continue
        if cur in desired and "=" in stripped and not stripped.startswith(";"):
            key = stripped.split("=", 1)[0].strip()
            if key in desired[cur]:
                if (cur, key) in written:
                    continue  # drop duplicate / stale occurrence
                written.add((cur, key))
                val = desired[cur][key]
                for v in (val if isinstance(val, list) else [val]):
                    out.append("%s=%s" % (key, v))
                continue
        out.append(line)

    if cur is not None:
        flush_section(cur)

    for section in desired:
        if section in seen_sections:
            continue
        if out and out[-1].strip():
            out.append("")
        out.append(section)
        flush_section(section)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".takaro-new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
