#!/usr/bin/env python3
"""Dump the symbols the plugin wants, straight out of a Dragonwilds server build.

Reads the wanted-name table out of ../src/sym.cpp (single source of truth), resolves each name
against <binary>.sym and prints a Markdown table; with --write it also saves
docs/symbols-<buildid>.md next to the plugin sources.

  python3 tools/symdump.py [--binary PATH] [--write] [--json]

The .sym format (verified): u32 N, then N x 20-byte records {u64 rva, u32 line, u32 fileOff,
u32 nameOff} sorted by rva, then a '\n'-separated name table shared by file paths and demangled
function signatures. A function's address is the lowest rva of its contiguous record run;
vaddr = rva + the first PT_LOAD p_vaddr read from the ELF.
"""
import argparse
import json
import mmap
import os
import re
import struct
import sys

# Point DRAGONWILDS_SERVER_BINARY at your dedicated server's binary; the .sym file is
# expected next to it (that is how SteamCMD ships it).
DEFAULT_BINARY = os.environ.get(
    "DRAGONWILDS_SERVER_BINARY",
    "RSDragonwilds/Binaries/Linux/RSDragonwildsServer-Linux-Shipping",
)
HERE = os.path.dirname(os.path.abspath(__file__))


def wanted_names(sym_cpp):
    """Parse the kWanted[] table out of src/sym.cpp -> [(key, signature_or_None)]."""
    text = open(sym_cpp, encoding="utf8").read()
    block = text.split("const Want kWanted[] = {", 1)[1].split("\n};", 1)[0]
    out = []
    for m in re.finditer(r'\{"((?:[^"\\]|\\.)*)",\s*(nullptr|"(?:[^"\\]|\\.)*")\}', block):
        key = m.group(1)
        sig = None if m.group(2) == "nullptr" else m.group(2)[1:-1]
        out.append((key, sig))
    return out


def elf_info(path):
    with open(path, "rb") as f:
        eh = f.read(64)
        if eh[:4] != b"\x7fELF":
            raise SystemExit(f"{path}: not an ELF")
        e_phoff, e_shoff = struct.unpack_from("<QQ", eh, 32)
        phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from("<HHHHH", eh, 54)
        f.seek(e_phoff)
        ph = f.read(phentsize * phnum)
        load_base = None
        for i in range(phnum):
            p_type, _, _, vaddr = struct.unpack_from("<IIQQ", ph, i * phentsize)
            if p_type == 1:
                load_base = vaddr
                break
        f.seek(e_shoff)
        sh = f.read(shentsize * shnum)
        secs = [struct.unpack_from("<IIQQQQIIQQ", sh, i * shentsize) for i in range(shnum)]
        f.seek(secs[shstrndx][4])
        strtab = f.read(secs[shstrndx][5])
        info = {"loadBase": load_base, "buildId": "", "sections": {}}
        for s in secs:
            name = strtab[s[0]:strtab.index(b"\0", s[0])].decode()
            info["sections"][name] = {"addr": s[3], "off": s[4], "size": s[5]}
            if name == ".note.gnu.build-id":
                f.seek(s[4])
                d = f.read(s[5])
                ns, ds, _ = struct.unpack_from("<III", d, 0)
                start = 12 + ((ns + 3) // 4) * 4
                info["buildId"] = d[start:start + ds].hex()
        return info


def resolve(sym_path, wanted):
    by_exact = {sig: key for key, sig in wanted if sig}
    by_base = {key: key for key, sig in wanted if not sig}
    with open(sym_path, "rb") as fh:
        m = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        n = struct.unpack_from("<I", m, 0)[0]
        names_base = 4 + n * 20
        if names_base > len(m):
            raise SystemExit(".sym record count does not fit the file")
        # pass 1: name-table offsets we care about
        off_to_key = {}
        pos = names_base
        while pos < len(m):
            nl = m.find(b"\n", pos)
            end = nl if nl >= 0 else len(m)
            line = m[pos:end].decode("utf8", "replace")
            key = by_exact.get(line)
            if key is None:
                key = by_base.get(line.split("(")[0])
            if key is not None:
                off_to_key[pos - names_base] = (key, line)
            if nl < 0:
                break
            pos = end + 1
        # pass 2: lowest rva per name + record run
        out = {}
        for i in range(n):
            rva, _line, _fo, no = struct.unpack_from("<QIII", m, 4 + i * 20)
            hit = off_to_key.get(no)
            if hit is None:
                continue
            key, sig = hit
            e = out.setdefault(key, {"rva": None, "sig": sig, "records": 0, "first": i, "last": i})
            if e["rva"] is None or rva < e["rva"]:
                e["rva"] = rva
                e["sig"] = sig
            e["records"] += 1
            e["last"] = i
        for e in out.values():
            e["contiguous"] = (e["last"] - e["first"] + 1) == e["records"]
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=DEFAULT_BINARY)
    ap.add_argument("--sym", default=None, help="defaults to <binary>.sym")
    ap.add_argument("--write", action="store_true", help="write docs/symbols-<buildid>.md")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    sym_path = args.sym or args.binary + ".sym"
    info = elf_info(args.binary)
    wanted = wanted_names(os.path.join(HERE, "..", "src", "sym.cpp"))
    res = resolve(sym_path, wanted)
    base = info["loadBase"]

    rows = []
    for key, _sig in wanted:
        e = res.get(key)
        if e and e["rva"] is not None:
            rows.append((key, e["rva"], e["rva"] + base, e["sig"], e["records"], e["contiguous"]))
        else:
            rows.append((key, None, None, "", 0, True))
    found = sum(1 for r in rows if r[1] is not None)

    if args.json:
        print(json.dumps({"buildId": info["buildId"], "loadBase": base, "resolved": found,
                          "wanted": len(rows),
                          "symbols": {r[0]: {"rva": r[1], "addr": r[2], "signature": r[3],
                                             "records": r[4], "contiguous": r[5]} for r in rows}},
                         indent=2))
        return 0 if found else 1

    text = [f"# Dragonwilds server symbols — build-id `{info['buildId']}`", ""]
    text.append(f"- binary: `{args.binary}`")
    text.append(f"- `.sym`: `{sym_path}`")
    text.append(f"- first `PT_LOAD` p_vaddr (load base): `0x{base:x}` — **vaddr = rva + load base**")
    for sec in (".text", ".init", ".fini", ".rodata"):
        s = info["sections"].get(sec)
        if s:
            text.append(f"- `{sec}`: addr `0x{s['addr']:x}` size `0x{s['size']:x}`")
    text.append(f"- resolved **{found}/{len(rows)}** wanted names")
    text.append("")
    text.append("| name | rva | vaddr | records | contiguous | signature |")
    text.append("|---|---|---|---|---|---|")
    for key, rva, addr, sig, recs, cont in rows:
        if rva is None:
            text.append(f"| `{key}` | — | — | 0 | — | **MISSING** |")
        else:
            text.append(f"| `{key}` | `0x{rva:x}` | `0x{addr:x}` | {recs} | "
                        f"{'yes' if cont else 'NO'} | `{sig}` |")
    out = "\n".join(text) + "\n"
    print(out)
    if args.write:
        docs = os.path.join(HERE, "..", "docs")
        os.makedirs(docs, exist_ok=True)
        p = os.path.join(docs, f"symbols-{info['buildId']}.md")
        open(p, "w", encoding="utf8").write(out)
        print(f"wrote {os.path.normpath(p)}", file=sys.stderr)
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
