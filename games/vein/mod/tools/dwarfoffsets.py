#!/usr/bin/env python3
"""Derive the UE object-model offsets from the depot's DWARF, so `reflect.cpp` can be cross-checked
against the compiler's own truth instead of against a third-party layout ini.

The Linux depot ships `VeinServer-Linux-Test.debug` (962 MiB, full DWARF, 1.1 M symbols) next to the
stripped executable, linked by `.gnu_debuglink`. This tool reads the member offsets of the structs
`reflect.cpp` walks, by asking `gdb` for `ptype /o`. Run it inside a container that has gdb:

  docker run --rm -v <dir with the .debug>:/bin-ro:ro -v $PWD:/src -w /src debian:bookworm \\
      sh -c 'apt-get -qq update && apt-get -qq install -y gdb python3 && \\
             python3 tools/dwarfoffsets.py --debug /bin-ro/VeinServer-Linux-Test.debug'

gdb is used rather than a hand-rolled DWARF walk because `.debug_pubtypes` on this build covers only
~3 300 C runtime types and none of the UE ones, so there is no index to jump with; gdb builds its
own and answers in about a minute. Everything here is a *cross-check*: `reflect.cpp` still discovers
every offset at boot and uses what it discovered.

Output is the same key set `/health.diagnostics.reflect` reports, so the two can be diffed directly.
`--json` prints it machine-readably; the exit status is 1 when a wanted struct was not found.
"""
import argparse
import json
import re
import subprocess
import sys

# struct name in DWARF -> {member name in DWARF: key used by reflect.cpp / /health}
WANTED = {
    "UObjectBase": {
        "ClassPrivate": "objClass",
        "NamePrivate": "objName",
        "OuterPrivate": "objOuter",
        "ObjectFlags": "objFlags",
        "InternalIndex": "objIndex",
    },
    "UStruct": {
        "SuperStruct": "structSuper",
        "ChildProperties": "structChildProperties",
        "PropertiesSize": "structPropertiesSize",
    },
    "UClass": {"ClassDefaultObject": "classDefaultObject"},
    "UFunction": {"FunctionFlags": "funcFunctionFlags", "Func": "funcFunc"},
    "FField": {"NextField": "fieldNext", "Next": "fieldNext", "NamePrivate": "fieldName",
               "ClassPrivate": "fieldClass"},
    "FProperty": {"Offset_Internal": "propOffsetInternal", "ElementSize": "propElementSize"},
}


# `ptype /o` prints one line per member as
#     /*     16      |       8 */    class UClass *ClassPrivate;
# with nested aggregates indented further. We only ever look a member up by name and keep the first
# hit, so the nesting does not need to be modelled.
MEMBER_RE = re.compile(r"^/\*\s*(\d+)\s*\|\s*\d+\s*\*/\s+.*?([A-Za-z_]\w*)\s*;")


def gdb_ptype(path, structs, gdb="gdb"):
    """{struct: {member: offset}} via one gdb run over all wanted structs."""
    cmd = [gdb, "-batch", "-nx", "-ex", "set confirm off", "-ex", "set print pretty off"]
    for s in structs:
        cmd += ["-ex", "echo \\n@@@ %s\\n" % s, "-ex", "ptype /o struct %s" % s]
    cmd.append(path)
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    out, current = {}, None
    for line in p.stdout.splitlines():
        if line.startswith("@@@ "):
            current = line[4:].strip()
            out[current] = {}
            continue
        if current is None:
            continue
        m = MEMBER_RE.match(line.strip())
        if m:
            out[current].setdefault(m.group(2), int(m.group(1)))
    return out, p.stderr


def fill_missing_by_offsetof(path, raw, gdb="gdb"):
    """Ask gdb for `&((Struct*)0)->Member` for every member ptype /o did not place."""
    todo = [(st, m) for st, mapping in WANTED.items() for m in mapping if m not in raw.get(st, {})]
    if not todo:
        return raw
    cmd = [gdb, "-batch", "-nx", "-ex", "set confirm off"]
    for st, m in todo:
        cmd += ["-ex", "echo \\n@@@ %s %s\\n" % (st, m),
                "-ex", "print/d (int)(long)&((struct %s *)0)->%s" % (st, m)]
    cmd.append(path)
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    key = None
    for line in p.stdout.splitlines():
        if line.startswith("@@@ "):
            key = tuple(line[4:].split())
            continue
        m = re.match(r"\$\d+ = (-?\d+)", line.strip())
        if m and key and int(m.group(1)) >= 0:
            raw.setdefault(key[0], {}).setdefault(key[1], int(m.group(1)))
        if m:
            key = None
    return raw


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--debug", required=True, help="path to VeinServer-Linux-Test.debug")
    ap.add_argument("--gdb", default="gdb")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    raw, err = gdb_ptype(args.debug, list(WANTED), args.gdb)
    # `ptype /o` prints the offset on the *opening* line of a nested aggregate, so members whose
    # type is a wrapper (TObjectPtr, FName, ...) are not on a line the member regex can match.
    # gdb can be asked for those directly, which is exact and cheap.
    raw = fill_missing_by_offsetof(args.debug, raw, args.gdb)
    missing = [n for n in WANTED if not raw.get(n)]
    if missing and err.strip():
        print(err.strip()[:500], file=sys.stderr)

    derived = {}
    for struct, mapping in WANTED.items():
        found = raw.get(struct, {})
        for member, key in mapping.items():
            if member in found:
                derived.setdefault(key, found[member])

    if args.json:
        print(json.dumps(dict(derived=derived, raw=raw, missing=missing), indent=2, sort_keys=True))
    else:
        print("DWARF-derived offsets (%s)" % args.debug)
        for k in sorted(derived):
            print("  %-24s 0x%x  (%d)" % (k, derived[k], derived[k]))
        if missing:
            print("\nstructs gdb could not type: %s" % ", ".join(missing))
        print("\nCompare against /health.diagnostics.reflect (same key names).")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
