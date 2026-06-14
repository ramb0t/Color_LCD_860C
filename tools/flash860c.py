#!/usr/bin/env python3
"""860C bootloader UART flasher (reverse-engineered protocol).

Builds the bootloader block stream from a firmware .bin and (optionally) sends
it over serial at 57600 8N1. See ../docs/bootloader-uart-protocol.md.

  Block (2060 B): magic(2) | addr_be32(4) | payload(2048) | sum_be32(4) | 0D 0A
    magic   F0F0 data, F1F1 last data block, F2F2 terminator
    addr    0x08004000 + index*0x800   (big-endian)
    sum     big-endian 32-bit sum of the 2048 payload bytes
  Flow (host<->display):
    host polls 0x5A until display sends 0xA5 (ready, on power-on), then for each
    block: host sends block, display replies 0x85 (ACK); two F2F2 terminators.

Usage:
  flash860c.py firmware.bin                         # dry-run: build + self-verify
  flash860c.py firmware.bin --out stream.bin        # also save framed stream
  flash860c.py firmware.bin --flash --port /dev/ttyACM0 --yes   # actually flash

!! WRITES TO THE DEVICE. Block format/addresses/checksums are verified byte-exact
   against a real flash; the 0xA5/0x85 handshake is from a live capture, but exact
   error/NAK/retry semantics are not fully characterised. A bad/partial flash can
   brick the display. Have a working SWD/JTAG recovery path (make flash_jtag) ready.
"""
import sys, argparse, struct, time

BASE   = 0x08004000      # protocol ADDRESS-FIELD base (NOT the physical load addr).
                         # The app physically runs at 0x08005000; the bootloader
                         # applies a +0x1000 mapping (field 0x4000 -> phys 0x5000).
                         # 0x4000 is correct and matches the factory tool byte-for-byte.
                         # Do NOT change to 0x5000 -- it shifts the image and bricks boot.
BLK    = 2048            # payload bytes per block
CRLF   = b"\x0d\x0a"
SYNC   = 0x5A

def build_block(magic, addr, payload):
    assert len(payload)==BLK
    body = bytes([magic,magic]) + struct.pack(">I",addr) + payload
    return body + struct.pack(">I", sum(payload)) + CRLF   # sum of 2048 payload bytes

def build_stream(fw, base=BASE, preamble=134, terminators=2):
    out = bytearray([SYNC]*preamble)
    nblk = (len(fw)+BLK-1)//BLK
    for i in range(nblk):
        chunk = fw[i*BLK:(i+1)*BLK].ljust(BLK, b"\x00")     # zero-pad last
        magic = 0xF1 if i==nblk-1 else 0xF0                  # F1F1 on final data block
        out.append(SYNC)
        out += build_block(magic, base + i*0x800, chunk)
    for _ in range(terminators):                            # F2F2 terminators
        out.append(SYNC)
        out += build_block(0xF2, 0x00000000, b"\x00"*BLK)
    return bytes(out), nblk

def build_blocks(fw, base=BASE, terminators=2):
    """list of raw 2060-byte blocks (no SYNC prefix), for ACK-gated flashing."""
    blocks=[]; nblk=(len(fw)+BLK-1)//BLK
    for i in range(nblk):
        chunk=fw[i*BLK:(i+1)*BLK].ljust(BLK,b"\x00")
        magic=0xF1 if i==nblk-1 else 0xF0
        blocks.append(build_block(magic, base+i*0x800, chunk))
    for _ in range(terminators):
        blocks.append(build_block(0xF2, 0x00000000, b"\x00"*BLK))
    return blocks

def verify(stream, base):
    """re-parse our own stream; confirm checksums + addresses are self-consistent."""
    p=0
    while p<len(stream) and stream[p]==SYNC: p+=1            # skip leading preamble
    idx=0; ok=True; data=0
    while p < len(stream)-2059:
        if stream[p]==SYNC: p+=1; continue
        if not (stream[p]==stream[p+1] and stream[p] in (0xF0,0xF1,0xF2)):
            p+=1; continue
        b=stream[p:p+2060]; p+=2060
        magic=b[:2].hex(); addr=struct.unpack(">I",b[2:6])[0]
        payload=b[6:2054]; csum=struct.unpack(">I",b[2054:2058])[0]; term=b[2058:2060]
        if magic in ("f0f0","f1f1"):
            exp=base+idx*0x800
            if addr!=exp or csum!=sum(payload) or term!=CRLF: ok=False
            data+=1; idx+=1
        elif magic=="f2f2":
            if addr!=0 or csum!=0 or term!=CRLF: ok=False
    return ok, data

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--base", default=hex(BASE),
                    help="protocol address-field base (default 0x08004000). "
                         "NOT the physical load addr (app runs at 0x5000; bootloader maps +0x1000). "
                         "Leave at 0x4000 -- changing it bricks the boot.")
    ap.add_argument("--preamble", type=int, default=134)
    ap.add_argument("--poll-ms", type=float, default=31.0, help="0x5A poll interval while waiting")
    ap.add_argument("--ready-byte", default="0xA5", help="display 'ready' byte (default 0xA5)")
    ap.add_argument("--ack-byte", default="0x85", help="display per-block ACK byte (default 0x85)")
    ap.add_argument("--nak-byte", default="0x8F", help="display NAK/bad-checksum byte (default 0x8F)")
    ap.add_argument("--ready-timeout", type=float, default=60.0, help="seconds to wait for power-on/ready")
    ap.add_argument("--block-timeout", type=float, default=2.0, help="seconds to wait for each block ACK")
    ap.add_argument("--retries", type=int, default=3, help="resend attempts per block on missing ACK")
    ap.add_argument("--corrupt-block", type=int, default=-1,
                    help="send this data block with a BAD checksum first (probe NAK), "
                         "print the display's response, then send it correctly")
    ap.add_argument("--out", help="save framed stream to this file")
    ap.add_argument("--flash", action="store_true", help="actually transmit over serial")
    ap.add_argument("--port", help="serial device, e.g. /dev/ttyACM0")
    ap.add_argument("--yes", action="store_true", help="confirm you accept brick risk")
    a=ap.parse_args()

    fw=open(a.binary,"rb").read()
    base=int(a.base,0)
    stream,nblk=build_stream(fw, base=base, preamble=a.preamble)
    ok,data=verify(stream, base)
    print(f"firmware : {a.binary}  {len(fw)} bytes")
    print(f"field base: {hex(base)} (app runs at 0x08005000, +0x1000 mapping)   "
          f"blocks: {nblk} data (last=F1F1) + 2 terminators")
    print(f"stream   : {len(stream)} bytes   self-verify: {'OK' if ok else 'FAILED'} ({data} data blocks)")
    if not ok:
        print("ABORT: stream failed self-verification"); sys.exit(1)

    if a.out:
        open(a.out,"wb").write(stream); print(f"framed stream -> {a.out}")

    if not a.flash:
        print("\nDRY-RUN (no serial). Re-run with --flash --port <dev> --yes to write.")
        return

    if not a.port or not a.yes:
        print("\nREFUSING: --flash requires both --port <dev> and --yes (brick risk)."); sys.exit(2)

    import serial
    READY=int(a.ready_byte,0); ACK=int(a.ack_byte,0); NAK=int(a.nak_byte,0)
    blocks=build_blocks(fw, base=base)
    print(f"\n!! FLASHING over {a.port} @ 57600 8N1 — do not disconnect.")
    ser=serial.Serial(a.port, 57600, bytesize=8, parity="N", stopbits=1, timeout=0.05)

    # 1) handshake: poll 0x5A while waiting for the display to power on and send READY (0xA5)
    print(f"  waiting for display ready (0x{READY:02x}) — power on the display now...")
    ser.reset_input_buffer()
    t_end=time.time()+a.ready_timeout; ready=False
    while time.time()<t_end:
        ser.write(bytes([SYNC])); ser.flush()
        r=ser.read(64)
        if r and READY in r:
            print(f"  display ready: {r.hex()}"); ready=True; break
        time.sleep(a.poll_ms/1000.0)
    if not ready:
        print("  ABORT: no ready byte within timeout."); ser.close(); sys.exit(3)

    # 2) send each block; data blocks (F0/F1) are ACKed with 0x85, the F2F2
    #    terminators are NOT ACKed (display boots after them).
    for n,blk in enumerate(blocks):
        is_term = blk[0]==0xF2
        if is_term:
            ser.write(blk); ser.flush()
            print(f"\r  block {n+1}/{len(blocks)} (terminator, no ACK)", end="", flush=True)
            continue
        if n==a.corrupt_block:                              # probe NAK/error behaviour
            bad=bytearray(blk); bad[2057]^=0xFF             # flip a checksum byte
            print(f"\n  [corrupt] block {n}: sending BAD checksum "
                  f"({blk[2054:2058].hex()} -> {bytes(bad[2054:2058]).hex()})")
            ser.reset_input_buffer(); ser.write(bytes(bad)); ser.flush()
            td=time.time()+a.block_timeout; resp=b""
            while time.time()<td:
                r=ser.read(16)
                if r: resp+=r
            print(f"  [corrupt] display replied: {resp.hex() or '(nothing)'}  "
                  f"(0x85=ACK accepted, other=NAK/error, nothing=ignored/abort)")
            print(f"  [corrupt] now sending block {n} correctly to recover...")
        for attempt in range(1,a.retries+1):
            ser.write(blk); ser.flush()
            td=time.time()+a.block_timeout; got=False; buf=b""
            while time.time()<td:
                r=ser.read(8)
                if r:
                    buf+=r
                    if ACK in r: got=True; break
                    if NAK in r: break          # bad checksum -> resend now
            if got: break
            print(f"\n  block {n}: {'NAK' if NAK in buf else 'no ACK'} "
                  f"(attempt {attempt}/{a.retries}, got {buf.hex() or 'nothing'}), resending...")
        else:
            print(f"\n  ABORT at block {n}: no ACK after {a.retries} attempts."); ser.close(); sys.exit(4)
        print(f"\r  block {n+1}/{len(blocks)} ACKed", end="", flush=True)
    ser.close()
    print(f"\nDONE: {len(blocks)} blocks sent. Verify the display booted the new firmware.")

if __name__=="__main__":
    main()
