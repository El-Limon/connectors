#!/usr/bin/env python3
"""Offline twin of resolve.cpp for a stripped, RTTI-only PIE.

Why this exists
---------------
`DuneSandboxServer-Linux-Shipping` has no `.symtab` and no DWARF, so the usual "dump the symbols and
grep" loop is impossible. What it does have is 42 942 exported `_ZTV*` vtables — and, because it is a
PIE, **every vtable slot in the file image is 0**: the real target is the `r_addend` of an
`R_X86_64_RELATIVE` entry in `.rela.dyn` at `r_offset == the slot's vaddr`.

So this tool indexes `.rela.dyn` once and then answers the questions the plugin needs answered
*before* it is allowed to hook anything:

  * what are the slots of class X, and what does each point at?
  * which slots does class X override relative to its base?  (-> that is where the game's own code is)
  * does a byte pattern match exactly once in .text, and what does its RIP-relative operand resolve to?
  * is an address a real function entry, per `.eh_frame_hdr`?

Everything is read-only.  Usage:

    vtprobe.py <binary> slots   _ZTV7UObject [--count N]
    vtprobe.py <binary> diff    _ZTV13AGameModeBase _ZTV24ADuneSandboxGameModeBase
    vtprobe.py <binary> find    ADuneCharacter          # search .dynsym for matching _ZTV names
    vtprobe.py <binary> sig     "4C 8D 2D ?? ?? ?? ??" [--rip-off 3]
    vtprobe.py <binary> funcs   0x1023ace0 [0x...]      # is each an .eh_frame function entry?

No third-party dependencies: pure `struct` over an mmap'd file.
"""
import argparse, bisect, mmap, re, struct, sys

class Elf:
    def __init__(self, path):
        self.f = open(path, 'rb')
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        m = self.m
        assert m[:4] == b'\x7fELF' and m[4] == 2, 'not an ELF64'
        (self.e_type,) = struct.unpack_from('<H', m, 0x10)
        e_shoff, = struct.unpack_from('<Q', m, 0x28)
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<HHH', m, 0x3a)
        secs = []
        for i in range(e_shnum):
            o = e_shoff + i * e_shentsize
            name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from('<IIQQQQIIQQ', m, o)
            secs.append(dict(name=name, type=typ, addr=addr, off=off, size=size, link=link, entsize=entsize))
        strtab = secs[e_shstrndx]
        def nm(x):
            b = m[strtab['off'] + x['name']:strtab['off'] + x['name'] + 64]
            return b.split(b'\0')[0].decode()
        self.sec = {nm(s): s for s in secs}
        self.secs = secs
        # lowest PT_LOAD p_vaddr
        e_phoff, = struct.unpack_from('<Q', m, 0x20)
        e_phentsize, e_phnum = struct.unpack_from('<HH', m, 0x36)
        self.loads = []
        for i in range(e_phnum):
            o = e_phoff + i * e_phentsize
            p_type, p_flags, p_offset, p_vaddr = struct.unpack_from('<IIQQ', m, o)
            p_filesz, p_memsz = struct.unpack_from('<QQ', m, o + 0x20)
            if p_type == 1:
                self.loads.append(dict(flags=p_flags, off=p_offset, vaddr=p_vaddr, filesz=p_filesz, memsz=p_memsz))
        self.load_base = min(l['vaddr'] for l in self.loads)
        self._rela = None
        self._fdes = None

    def va_to_off(self, va):
        for l in self.loads:
            if l['vaddr'] <= va < l['vaddr'] + l['filesz']:
                return l['off'] + (va - l['vaddr'])
        return None

    # ---- .dynsym -------------------------------------------------------------------------------
    def dynsyms(self):
        sym, dstr = self.sec['.dynsym'], self.sec['.dynstr']
        n = sym['size'] // 24
        base, sbase = sym['off'], dstr['off']
        for i in range(n):
            o = base + i * 24
            st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack_from('<IBBHQQ', self.m, o)
            if not st_name:
                continue
            end = self.m.find(b'\0', sbase + st_name)
            yield self.m[sbase + st_name:end].decode('utf-8', 'replace'), st_value, st_size

    def vtable(self, name):
        for n, v, sz in self.dynsyms():
            if n == name:
                return v, sz
        return None, None

    # ---- .rela.dyn: the only place a PIE's vtable slots really live ------------------------------
    def rela(self):
        if self._rela is None:
            s = self.sec['.rela.dyn']
            d = {}
            for i in range(s['size'] // 24):
                off, info, addend = struct.unpack_from('<QQq', self.m, s['off'] + i * 24)
                if info & 0xffffffff == 8:  # R_X86_64_RELATIVE
                    d[off] = addend
            self._rela = d
        return self._rela

    def slots(self, ztv, count=None):
        """(offset_to_top, typeinfo, [slot targets]). Stops at an embedded sub-vtable header, which
        is what multiple inheritance packs into the same _ZTV symbol."""
        addr, size = self.vtable(ztv)
        if addr is None:
            raise SystemExit('%s: not in .dynsym' % ztv)
        r = self.rela()
        top = r.get(addr, 0)
        ti = r.get(addr + 8, 0)
        maxn = (size - 16) // 8 if size >= 16 else 0
        if count:
            maxn = min(maxn, count)
        out = []
        for i in range(maxn):
            va = addr + 16 + i * 8
            t = r.get(va)
            if t is None:
                # no relocation at all: an offset-to-top word of an embedded sub-vtable
                if i + 1 < maxn and r.get(va + 8) == ti:
                    break
                out.append(None)
                continue
            out.append(t)
        return addr, size, top, ti, out

    # ---- .eh_frame_hdr function-entry table -----------------------------------------------------
    def fdes(self):
        if self._fdes is None:
            s = self.sec['.eh_frame_hdr']
            o = s['off']
            ver, ehenc, cntenc, tabenc = self.m[o], self.m[o + 1], self.m[o + 2], self.m[o + 3]
            if (ver, cntenc, tabenc) != (1, 0x03, 0x3b):
                raise SystemExit('unsupported .eh_frame_hdr encodings %r' % ((ver, ehenc, cntenc, tabenc),))
            (count,) = struct.unpack_from('<I', self.m, o + 8)
            base = s['addr']
            t = []
            for i in range(count):
                (rel,) = struct.unpack_from('<i', self.m, o + 12 + i * 8)
                t.append(base + rel)
            self._fdes = t
        return self._fdes

    def is_func(self, va):
        t = self.fdes()
        i = bisect.bisect_left(t, va)
        return i < len(t) and t[i] == va

    # ---- signatures ------------------------------------------------------------------------------
    def scan(self, pattern, limit=8):
        parts = pattern.split()
        rx = b''
        for p in parts:
            rx += b'.' if p in ('?', '??') else re.escape(bytes([int(p, 16)]))
        text = self.sec['.text']
        blob = self.m[text['off']:text['off'] + text['size']]
        return [text['addr'] + mo.start() for mo in re.finditer(rx, blob, re.S)][:limit], text

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('binary')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('slots'); p.add_argument('ztv'); p.add_argument('--count', type=int)
    p = sub.add_parser('diff'); p.add_argument('base'); p.add_argument('derived')
    p = sub.add_parser('find'); p.add_argument('needle')
    p = sub.add_parser('sig'); p.add_argument('pattern'); p.add_argument('--rip-off', type=int)
    p = sub.add_parser('funcs'); p.add_argument('addrs', nargs='+')
    a = ap.parse_args()
    e = Elf(a.binary)

    if a.cmd == 'find':
        hits = sorted({n for n, _, _ in e.dynsyms() if n.startswith('_ZTV') and a.needle in n})
        for h in hits:
            addr, size = e.vtable(h)
            print('%-52s 0x%08x size=%-6d slots=%d' % (h, addr, size, max(0, (size - 16) // 8)))
        print('%d match(es)' % len(hits))

    elif a.cmd == 'slots':
        addr, size, top, ti, sl = e.slots(a.ztv, a.count)
        print('%s @0x%x size=%d offset-to-top=%d typeinfo=0x%x slots=%d' % (a.ztv, addr, size, top, ti, len(sl)))
        if all(s in (None, 0) for s in sl):
            print('!! every slot is 0/unrelocated - read .rela.dyn, not the file image')
        for i, t in enumerate(sl):
            mark = '' if t is None else ('' if e.is_func(t) else '   <-- NOT an .eh_frame function entry')
            print('  [%3d] %s%s' % (i, 'none' if t is None else '0x%08x' % t, mark))

    elif a.cmd == 'diff':
        _, _, _, _, b = e.slots(a.base)
        _, _, _, _, d = e.slots(a.derived)
        print('%s: %d slots · %s: %d slots' % (a.base, len(b), a.derived, len(d)))
        for i in range(min(len(b), len(d))):
            if b[i] != d[i]:
                print('  [%3d] base=0x%-10x derived=0x%-10x  OVERRIDE' % (i, b[i] or 0, d[i] or 0))
        if len(d) > len(b):
            print('  derived adds %d new virtual(s) from slot %d' % (len(d) - len(b), len(b)))

    elif a.cmd == 'sig':
        hits, text = e.scan(a.pattern)
        print('%d match(es) in .text (the resolver accepts EXACTLY ONE)' % len(hits))
        for h in hits:
            line = '  0x%08x' % h
            if a.rip_off is not None:
                off = e.va_to_off(h)
                (disp,) = struct.unpack_from('<i', e.m, off + a.rip_off)
                target = h + a.rip_off + 4 + disp
                line += '  rip-operand -> 0x%08x' % target
                t = e.sec['.text']
                line += ' (in .text)' if t['addr'] <= target < t['addr'] + t['size'] else ' (data)'
            print(line)

    elif a.cmd == 'funcs':
        for s in a.addrs:
            va = int(s, 0)
            print('0x%08x  %s' % (va, 'function entry' if e.is_func(va) else 'NOT a function entry'))

if __name__ == '__main__':
    main()
