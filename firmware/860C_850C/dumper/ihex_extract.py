#!/usr/bin/env python3
"""Reconstruct a verified binary from a (possibly multi-pass / lossy) Intel HEX
capture. Validates every record checksum, merges duplicate addresses (from
capture restarts) and reports any conflicts or gaps in the requested range."""
import sys

def main():
    path  = sys.argv[1] if len(sys.argv) > 1 else "dump.hex"
    start = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x08000000
    length= int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x5000
    out   = sys.argv[4] if len(sys.argv) > 4 else "bootloader.bin"

    mem = {}          # absolute addr -> byte
    conflicts = 0
    bad_lines = 0
    upper = 0
    for raw in open(path, "r", errors="replace"):
        line = raw.strip()
        if not line.startswith(":"):
            continue
        try:
            b = bytes.fromhex(line[1:])
        except ValueError:
            bad_lines += 1; continue
        if len(b) < 5 or (sum(b) & 0xFF) != 0:   # checksum must zero out
            bad_lines += 1; continue
        n, alo, typ = b[0], (b[1] << 8) | b[2], b[3]
        data = b[4:4 + n]
        if len(data) != n:
            bad_lines += 1; continue
        if typ == 0x04:
            upper = (data[0] << 8) | data[1]
        elif typ == 0x00:
            base = (upper << 16) | alo
            for i, v in enumerate(data):
                a = base + i
                if a in mem and mem[a] != v:
                    conflicts += 1
                mem[a] = v

    missing = [a for a in range(start, start + length) if a not in mem]
    print(f"valid-byte addresses collected: {len(mem)}")
    print(f"bad/corrupt records skipped   : {bad_lines}")
    print(f"duplicate-address CONFLICTS   : {conflicts}")
    print(f"range {start:#010x}..{start+length:#010x} missing bytes: {len(missing)}")
    if missing:
        print(f"  first missing: {missing[0]:#010x}")
        sys.exit(1)
    if conflicts:
        print("  CONFLICTS present -> capture unreliable, re-dump"); sys.exit(2)
    blob = bytes(mem[a] for a in range(start, start + length))
    open(out, "wb").write(blob)
    print(f"wrote {out} ({len(blob)} bytes)")
    print(f"  vector check: SP={int.from_bytes(blob[0:4],'little'):#010x} "
          f"reset={int.from_bytes(blob[4:8],'little'):#010x}")

if __name__ == "__main__":
    main()
