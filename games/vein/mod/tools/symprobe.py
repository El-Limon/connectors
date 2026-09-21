#!/usr/bin/env python3
"""Probe a VEIN server binary for the symbols the plugin wants, one strategy at a time.

This is the offline twin of src/resolve.cpp: it reads the wanted-name table out of
src/resolve.cpp (single source of truth) and reports, per strategy, which names that binary can
actually give us. Run it against a freshly installed depot *before* deploying the plugin so the
strategy decision in docs/symbols-<buildid>.md is a measurement rather than a guess.

  python3 tools/symprobe.py --binary /path/to/VeinServer-Linux-Test [--json] [--write]

Strategies, in the same order resolve.cpp tries them:
  symtab     .symtab of the ELF (mangled names, demangled here with c++filt)
  depotsym   <binary>.sym in the Dragonwilds depot format, if the depot ships one
  dynsym     .dynsym of the ELF
  signature  not probed here - it needs a pattern per name; see tools/sigderive.py

Exit status is 0 when every *required* name resolved by some strategy, 1 otherwise.
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BINARY = os.environ.get("VEIN_SERVER_BINARY", "Vein/Binaries/Linux/VeinServer-Linux-Test")

# ---------------------------------------------------------------------------------------------
# the wanted table


def wanted_names(resolve_cpp):
    """Parse kWanted[] out of src/resolve.cpp -> [(key, signature_or_None, required)]."""
    text = open(resolve_cpp, encoding="utf8").read()
    block = text.split("const Want kWanted[] = {", 1)[1].split("\n};", 1)[0]
    out = []
    # FN("key", "sig" | nullptr, true|false)   /   DATA("key")
    for m in re.finditer(
        r'FN\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*((?:nullptr)|(?:"(?:[^"\\]|\\.)*"(?:\s*"(?:[^"\\]|\\.)*")*))\s*,'
        r"\s*(true|false)\s*\)",
        block,
        re.S,
    ):
        key = m.group(1)
        raw = m.group(2)
        sig = None
        if raw != "nullptr":
            sig = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', raw))
        out.append((key, sig, m.group(3) == "true"))
    for m in re.finditer(r'DATA\(\s*"((?:[^"\\]|\\.)*)"\s*\)', block):
        out.append((m.group(1), None, False))
    return out


def base_name(key):
    return key.rsplit("::", 1)[-1]


# ---------------------------------------------------------------------------------------------
# ELF


class Elf:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        if d[:4] != b"\x7fELF" or d[4] != 2:
            raise SystemExit("%s is not an ELF64 image" % path)
        self.etype, = struct.unpack_from("<H", d, 16)
        e_shoff, = struct.unpack_from("<Q", d, 0x28)
        e_phoff, = struct.unpack_from("<Q", d, 0x20)
        e_phentsize, e_phnum = struct.unpack_from("<HH", d, 0x36)
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x3A)
        self.load_base = None
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, p_flags = struct.unpack_from("<II", d, off)
            p_vaddr, = struct.unpack_from("<Q", d, off + 0x10)
            if p_type == 1 and self.load_base is None:
                self.load_base = p_vaddr
        self.sections = {}
        shstr_off, = struct.unpack_from("<Q", d, e_shoff + e_shstrndx * e_shentsize + 0x18)
        for i in range(e_shnum):
            off = e_shoff + i * e_shentsize
            sh_name, sh_type = struct.unpack_from("<II", d, off)
            sh_addr, sh_off, sh_size = struct.unpack_from("<QQQ", d, off + 0x10)
            sh_entsize, = struct.unpack_from("<Q", d, off + 0x38)
            end = d.index(b"\0", shstr_off + sh_name)
            name = d[shstr_off + sh_name:end].decode()
            self.sections[name] = dict(addr=sh_addr, off=sh_off, size=sh_size, entsize=sh_entsize)
        self.build_id = ""
        note = self.sections.get(".note.gnu.build-id")
        if note:
            o = note["off"]
            namesz, descsz, _ = struct.unpack_from("<III", d, o)
            desc = o + 12 + ((namesz + 3) & ~3)
            self.build_id = d[desc:desc + descsz].hex()

    def symbols(self, dynamic):
        symname = ".dynsym" if dynamic else ".symtab"
        strname = ".dynstr" if dynamic else ".strtab"
        sym = self.sections.get(symname)
        strs = self.sections.get(strname)
        if not sym or not strs or not sym["entsize"]:
            return
        d = self.data
        n = sym["size"] // sym["entsize"]
        for i in range(n):
            off = sym["off"] + i * sym["entsize"]
            st_name, st_info = struct.unpack_from("<IB", d, off)
            st_value, = struct.unpack_from("<Q", d, off + 8)
            if not st_name:
                continue
            end = d.index(b"\0", strs["off"] + st_name)
            yield d[strs["off"] + st_name:end].decode("utf8", "replace"), st_value, st_info


def demangle_all(names):
    """Batch-demangle through c++filt; falls back to the identity map when it is missing."""
    if not names:
        return {}
    try:
        p = subprocess.run(["c++filt", "-n"], input="\n".join(names), capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return {n: n for n in names}
    return dict(zip(names, p.stdout.splitlines()))


def name_matches(line, key, sig):
    if sig:
        return line == sig
    return line.split("(", 1)[0] == key


# ---------------------------------------------------------------------------------------------
# strategies


def probe_elf_symbols(elf, wanted, dynamic, found):
    bases = {base_name(k) for k, _, _ in wanted if k not in found}
    if not bases:
        return {}
    candidates = []
    plain = {}
    for name, value, info in elf.symbols(dynamic):
        if not value:
            continue
        if name in bases:
            plain[name] = value
        if name.startswith("_Z") and any(b in name for b in bases):
            candidates.append((name, value))
    table = demangle_all([c[0] for c in candidates])
    out = {}
    for key, sig, _req in wanted:
        if key in found:
            continue
        if key in plain:
            out[key] = (plain[key], key)
            continue
        for name, value in candidates:
            line = table.get(name, name)
            if name_matches(line, key, sig):
                out[key] = (value, line)
                break
    return out


def probe_depot_sym(path, elf, wanted, found):
    if not os.path.exists(path):
        return {}, "no file at %s" % path
    with open(path, "rb") as f:
        blob = f.read()
    if len(blob) < 8:
        return {}, ".sym too small"
    n, = struct.unpack_from("<I", blob, 0)
    rec_bytes = n * 20
    if 4 + rec_bytes > len(blob):
        return {}, ".sym record count does not fit the file"
    names = blob[4 + rec_bytes:]
    by_exact, by_base = {}, {}
    for key, sig, _req in wanted:
        if key in found:
            continue
        (by_exact if sig else by_base)[sig or key] = key
    off_to_key = {}
    pos = 0
    while pos < len(names):
        nl = names.find(b"\n", pos)
        end = len(names) if nl < 0 else nl
        line = names[pos:end].decode("utf8", "replace")
        if line in by_exact:
            off_to_key.setdefault(pos, []).append((by_exact[line], line))
        base = line.split("(", 1)[0]
        if base in by_base:
            off_to_key.setdefault(pos, []).append((by_base[base], line))
        if nl < 0:
            break
        pos = end + 1
    out = {}
    for i in range(n):
        rva, _line, _fileoff, name_off = struct.unpack_from("<QIII", blob, 4 + i * 20)
        for key, line in off_to_key.get(name_off, ()):
            if key not in out or rva < out[key][0]:
                out[key] = (rva + elf.load_base, line)
    return out, "%d records" % n


# ---------------------------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", default=DEFAULT_BINARY)
    ap.add_argument("--sym", default=None, help="depot .sym path (default: <binary>.sym)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true", help="write docs/symbols-<buildid>.md")
    args = ap.parse_args()

    wanted = wanted_names(os.path.join(HERE, "..", "src", "resolve.cpp"))
    elf = Elf(args.binary)
    sym_path = args.sym or (args.binary + ".sym")

    found, how, sigs = {}, {}, {}
    notes = {}
    order = [
        ("symtab", lambda f: probe_elf_symbols(elf, wanted, False, f)),
        ("depotsym", None),
        ("dynsym", lambda f: probe_elf_symbols(elf, wanted, True, f)),
    ]
    counts = {}
    for name, fn in order:
        if name == "depotsym":
            got, note = probe_depot_sym(sym_path, elf, wanted, found)
            notes[name] = note
        else:
            got = fn(found)
            notes[name] = "%s present" % name if got else "%s: no hits" % name
        counts[name] = len(got)
        for key, (addr, line) in got.items():
            found[key] = addr
            how[key] = name
            sigs[key] = line

    required = [k for k, _s, r in wanted if r]
    missing_required = [k for k in required if k not in found]

    if args.json:
        print(json.dumps(
            dict(binary=args.binary, buildId=elf.build_id, loadBase=elf.load_base,
                 type="PIE" if elf.etype == 3 else "EXEC", strategies=counts, notes=notes,
                 resolved={k: dict(addr=hex(v), how=how[k], signature=sigs[k]) for k, v in found.items()},
                 wanted=len(wanted), required=len(required), missingRequired=missing_required),
            indent=2))
    else:
        print("binary      %s" % args.binary)
        print("type        %s" % ("PIE (ET_DYN)" if elf.etype == 3 else "EXEC (non-PIE)"))
        print("buildId     %s" % (elf.build_id or "(none)"))
        print("loadBase    0x%x" % elf.load_base)
        print("sections    %s" % ", ".join(s for s in (".symtab", ".dynsym", ".text", ".rodata") if s in elf.sections))
        print()
        for name, _ in order:
            print("strategy %-9s %4d names   (%s)" % (name, counts[name], notes[name]))
        print()
        print("| name | how | addr | signature |")
        print("|---|---|---|---|")
        for key, _sig, req in wanted:
            mark = "**%s**" % key if req else key
            if key in found:
                print("| %s | %s | 0x%x | `%s` |" % (mark, how[key], found[key], sigs[key]))
            else:
                print("| %s | - | - | unresolved |" % mark)
        print()
        print("resolved %d/%d (required %d/%d)" % (len(found), len(wanted),
                                                   len(required) - len(missing_required), len(required)))
        if missing_required:
            print("MISSING REQUIRED: %s" % ", ".join(missing_required))

    if args.write and elf.build_id:
        path = os.path.join(HERE, "..", "docs", "symbols-%s.md" % elf.build_id[:16])
        with open(path, "w", encoding="utf8") as f:
            f.write("# Symbols - build id `%s`\n\n" % elf.build_id)
            f.write("Generated by `tools/symprobe.py` against `%s`.\n\n" % args.binary)
            for name, _ in order:
                f.write("- strategy `%s`: %d names (%s)\n" % (name, counts[name], notes[name]))
            f.write("\n| name | how | addr | signature |\n|---|---|---|---|\n")
            for key, _sig, req in wanted:
                mark = "**%s**" % key if req else key
                if key in found:
                    f.write("| %s | %s | 0x%x | `%s` |\n" % (mark, how[key], found[key], sigs[key]))
                else:
                    f.write("| %s | - | - | unresolved |\n" % mark)
        print("wrote %s" % path)

    return 1 if missing_required else 0


if __name__ == "__main__":
    sys.exit(main())
