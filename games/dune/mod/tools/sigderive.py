#!/usr/bin/env python3
"""Derive a byte signature for a function and prove it matches exactly once.

Used for the `string-xref` strategy in src/resolve.cpp. On Dune this is not a last resort but the
ONLY route to the globals that have neither a symbol nor a vtable (`GEngine`, `GUObjectArray`), since
the binary is stripped of every function symbol.

  # the address is already known (from the offline dissection, or from tools/vtprobe.py)
  python3 tools/sigderive.py --binary DuneSandboxServer-Linux-Shipping --addr 0x9aff7fb

Then verify the result independently before it goes into the wanted table:

  python3 tools/vtprobe.py DuneSandboxServer-Linux-Shipping sig "<pattern>" --rip-off <n>

⚠️ Do NOT try to anchor a Dune pattern on a direct string xref. Only ~69% of this binary's UTF-16LE
literals are referenced by a `lea` at all: UE 5.2's structured logging holds `UE_LOG` format strings
in static `FStaticBasicLogRecord` structs in `.data.rel.ro`, wired up by `R_X86_64_RELATIVE`
relocations, so the code `lea`s the *record*, never the string. To find the function that logs a
string S, search `.rela.dyn` addends for S's address, take the record's address, and xref THAT — each
record tends to have exactly one referencing site, which makes it a sharper tool than a direct xref.

Output is a `pattern` string in the exact form ParseSignature() accepts ("48 8B ?? E8 ..."), the
number of matches it has in .text, and the length it needed to become unique. A pattern is only
usable when the match count is **1**; the plugin rejects anything else at boot, on purpose.

Bytes that are part of a rel32 displacement (E8/E9 call/jmp and RIP-relative operands) are
wildcarded, because those differ between builds even when the code does not.
"""
import argparse
import struct
import sys


def load_text(path, pe):
    d = open(path, "rb").read()
    if pe:
        pe_off, = struct.unpack_from("<I", d, 0x3C)
        nsec, = struct.unpack_from("<H", d, pe_off + 6)
        opt_size, = struct.unpack_from("<H", d, pe_off + 20)
        sec_off = pe_off + 24 + opt_size
        image_base, = struct.unpack_from("<Q", d, pe_off + 24 + 24)
        for i in range(nsec):
            o = sec_off + i * 40
            name = d[o:o + 8].rstrip(b"\0").decode()
            vsize, vaddr, rawsize, rawoff = struct.unpack_from("<IIII", d, o + 8)
            if name == ".text":
                return d[rawoff:rawoff + rawsize], vaddr, image_base
        raise SystemExit("no .text in the PE image")
    if d[:4] != b"\x7fELF":
        raise SystemExit("%s is neither an ELF nor a PE (--pe)" % path)
    e_shoff, = struct.unpack_from("<Q", d, 0x28)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x3A)
    shstr_off, = struct.unpack_from("<Q", d, e_shoff + e_shstrndx * e_shentsize + 0x18)
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        sh_name, = struct.unpack_from("<I", d, o)
        sh_addr, sh_off, sh_size = struct.unpack_from("<QQQ", d, o + 0x10)
        end = d.index(b"\0", shstr_off + sh_name)
        if d[shstr_off + sh_name:end] == b".text":
            return d[sh_off:sh_off + sh_size], sh_addr, 0
    raise SystemExit("no .text in the ELF image")


# Opcodes whose operand ends in a 4-byte relative displacement that shifts between builds.
def wildcard_mask(buf):
    """Returns a list of booleans: True where the byte must be wildcarded."""
    mask = [False] * len(buf)
    i = 0
    while i < len(buf):
        b = buf[i]
        if b in (0xE8, 0xE9):  # call rel32 / jmp rel32
            for k in range(i + 1, min(i + 5, len(buf))):
                mask[k] = True
            i += 5
            continue
        # 0F 8x rel32 (jcc near)
        if b == 0x0F and i + 1 < len(buf) and 0x80 <= buf[i + 1] <= 0x8F:
            for k in range(i + 2, min(i + 6, len(buf))):
                mask[k] = True
            i += 6
            continue
        # RIP-relative operand: modrm mod=00 rm=101 after a common opcode. Conservative: wildcard
        # the 4 displacement bytes of `48 8B 0D xx xx xx xx`-shaped loads.
        if b in (0x48, 0x4C) and i + 2 < len(buf) and buf[i + 1] in (0x8B, 0x8D, 0x89) and (buf[i + 2] & 0xC7) == 0x05:
            for k in range(i + 3, min(i + 7, len(buf))):
                mask[k] = True
            i += 7
            continue
        i += 1
    return mask


def to_pattern(buf, mask, n):
    return " ".join("??" if mask[i] else "%02X" % buf[i] for i in range(n))


def count_matches(text, pattern, limit=3):
    parts = pattern.split()
    want = [None if p == "??" else int(p, 16) for p in parts]
    n = len(want)
    hits, first = 0, -1
    start = want[0]
    for i in range(len(text) - n + 1):
        if text[i] != start:
            continue
        for k in range(1, n):
            w = want[k]
            if w is not None and text[i + k] != w:
                break
        else:
            if hits == 0:
                first = i
            hits += 1
            if hits >= limit:
                break
    return hits, first


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--pe", action="store_true", help="the binary is a Windows PE, not an ELF")
    ap.add_argument("--addr", type=lambda v: int(v, 0), help="virtual address of the function")
    ap.add_argument("--rva", type=lambda v: int(v, 0), help="RVA of the function (PE, from the PDB)")
    ap.add_argument("--max", type=int, default=64, help="longest pattern to try (bytes)")
    args = ap.parse_args()

    text, text_addr, image_base = load_text(args.binary, args.pe)
    if args.rva is not None:
        off = args.rva - text_addr
    elif args.addr is not None:
        off = args.addr - (text_addr + (image_base if args.pe else 0))
    else:
        raise SystemExit("pass --addr or --rva")
    if off < 0 or off >= len(text):
        raise SystemExit("that address is not inside .text (off=%d, .text size=%d)" % (off, len(text)))

    buf = text[off:off + args.max]
    mask = wildcard_mask(buf)
    for n in range(6, len(buf) + 1):
        if mask[n - 1]:
            continue  # never end on a wildcard: it hides how specific the pattern is
        pattern = to_pattern(buf, mask, n)
        if pattern.startswith("??"):
            continue
        hits, first = count_matches(text, pattern)
        if hits == 1:
            print("pattern  %s" % pattern)
            print("length   %d bytes" % n)
            print("matches  1 (unique in .text)")
            print("at       0x%x" % (text_addr + first + (image_base if args.pe else 0)))
            return 0
    print("no unique pattern within %d bytes - widen --max or pick a different anchor" % args.max, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
