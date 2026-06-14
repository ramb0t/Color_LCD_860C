# 860C bootloader UART firmware-download protocol

Reverse-engineered by passively probing the 860C display's serial bus with a
Digilent Analog Discovery 3 (logic analyzer) during a firmware flash, then
decoding the raw line samples offline. Verified **byte-exact**: the
reconstructed payload matches the flashed
`releases/.../860C_V13-v20.1C.5-1-860C-bootloader.bin` over its full length
(extra bytes are zero padding from the final block).

> Two reference builds appear below — `v20.1C.5-1` for the byte-exact
> reconstruction check, `v20.1C.5-2` for the vector-table example (reset
> `0x19621`). Both are 0x5000-linked; the version difference is harmless.

## Physical / link layer

| Property      | Value                                                      |
|---------------|-----------------------------------------------------------|
| Levels        | TTL UART, idle-high                                        |
| Download baud | **57600**, 8N1                                             |
| Normal baud   | 9600, 8N1 (display↔motor runtime traffic, *not* this)     |
| Wiring        | **two wires** — host→display (blocks) and display→host (ready/ACK) |

> Note: the runtime display protocol is 9600 baud and unrelated to the stock
> TSDZ2 (`0x43/0x59`) and Bafang-config (1200 baud) protocols. The bootloader
> download switches the line to 57600.

> Capture caveat: the AD3 traces here only probed the **host→display** wire. The
> **display→host** bytes (`0xA5`/`0x85`, below) were read from the actual flash
> session; addresses/checksums/blocks are from the AD3 trace, the response side
> is from the live capture.

## Direction A — host → display (the firmware)

What the host transmits:

```
[poll: 0x5A every ~31 ms]                   while waiting for the display to power on
  ( 2060-byte block )  ( wait for 0x85 )  ...   one block per ACK
[2 x F2F2 terminator block]                 end of transfer
```

- **Poll**: host sends `0x5A` every ~31 ms until the display is ready (see below).
- **Blocks**: fixed-size **2060-byte** blocks; the host sends the next one only
  after the display ACKs the previous (`0x85`). The ~200 ms inter-block gap is the
  display writing flash before it ACKs.
- **End of transfer**: two `F2 F2` terminator blocks (address 0, zero payload).
- Every block ends with `0D 0A` (CRLF).

## Direction B — display → host (ready + flow control)

What the display transmits (idle `0xFF`):

| Byte   | Meaning                                                          |
|--------|-----------------------------------------------------------------|
| `0xFF` | idle line                                                       |
| `0xA5` | **ready / hello** — sent once when the display enters bootloader on power-on. The host waits for this before flashing. |
| `0x85` | **ACK** — one per **data** block successfully received/written. |
| `0x8F` | **NAK** — block rejected (bad checksum). Host resends the same block; the bootloader accepts the retry and continues. |

Time-correlated against the host→display blocks in the same capture:

- `0xA5` appears **once**, immediately before the first block.
- Exactly **one `0x85` per data block** (145 blocks → 145 ACKs), arriving
  **~60 ms after** each block ends (the display erasing/writing flash).
- The **two `F2 F2` terminators are NOT ACKed** — the display boots the new
  firmware after them.

Flow control: **send data block → wait for `0x85` → send next**; on `0x8F`
(NAK) **resend the same block**; send the terminators without waiting.

Confirmed by sending a deliberately corrupt block (`flash860c.py --corrupt-block`):
the bootloader replied `0x8F`, accepted the corrected resend (`0x85`), and the
flash completed and booted normally. So the **checksum is validated** and
**per-block retry is supported**. Disassembly matches: the handler at `0x3e2–0x412`
sums the 2048 payload bytes, compares to the stored big-endian word at offset
`2054`, and on mismatch emits `0x8F` and returns failure (no write).

For the 294 KiB image observed: **145 data blocks** (`F0 F0` ×144 + `F1 F1` ×1)
followed by **2** `F2 F2` terminators = 147 blocks.

## Block format (2060 bytes) — fully decoded

| Offset | Size | Field                                                              |
|--------|------|--------------------------------------------------------------------|
| 0      | 2    | **Magic**: `F0 F0` data · `F1 F1` last data block · `F2 F2` terminator |
| 2      | 4    | **Address field**, big-endian — physical write addr = **field + 0x1000** (see note); NOT the literal destination |
| 6      | 2048 | **Firmware payload** (2 KiB; last block zero-padded)               |
| 2054   | 4    | **Checksum**: big-endian 32-bit *sum of the 2048 payload bytes*     |
| 2058   | 2    | **Terminator** `0D 0A` (CRLF)                                       |

Verified across all 145 data blocks of the reference flash (100 %):

- **Address field** = `0x08004000 + block_index × 0x800`
  (first block `0x08004000`, +2 KiB per block, last data block `0x0804C000`).
- **Checksum** = `struct.pack('>I', sum(payload_2048_bytes))`.
- **Magic**: `F0 F0` for normal data, `F1 F1` for the final data block,
  `F2 F2` for the two end-of-transfer terminators (address `0x00000000`,
  all-zero payload, checksum 0).
- The `F1 F1` final block also triggers a **GPIOC finalize step** (port
  `0x40011000`, bit mask `0x2000`) in the bootloader — a completion/indicator
  action, from disassembly.

### ⚠️ The address field is NOT the physical write address

The app **runs at `0x08005000`** (20 KiB bootloader below it), not `0x08004000`.
Proven three ways: the release `.bin` is 0x5000-linked (its reset handler
`0x08019621` resolves to real code — a CMSIS `.data` copy loop — only at base
`0x5000`, file offset `0x14621`; at base `0x4000`/offset `0x15621` it lands in an
ASCII string); the on-device dump shows the app vector at `0x5000` with
`0x4000–0x5000` left as zeros; and the repo linker script / `VTOR` use `0x5000`.

Yet block 0 — which carries the image's vector table (payload offset 0) — has
address field `0x08004000`. So the field value is offset **`+0x1000`** from the
physical destination: **field `0x4000` → physical `0x5000`**. (Block 2's field
`0x5000` carries non-vector payload `4c f8 04 3b…`; that is correct app code at
its real home `0x6000`. If the field were physical, the jump to `0x5000` would hit
that and crash — it doesn't.)

**Mechanism confirmed by disassembly** of the `F0 F0` write handler — the address
is computed as `physical = field + 0x1000`, then consumed by the erase/program
routine:

```
0x42a: ldr  r2, =0x08004000
0x42c: str  r0, [r1,#8]      ; ctx[+8] = field (provisional)
0x42e: cmp  r0, r2
0x430: bcc  <nak>            ; field < 0x4000 -> NAK (write floor)
0x432: add  r0, r0, #0x1000  ; physical = field + 0x1000
0x436: str  r0, [r1,#8]      ; ctx[+8] = physical (erase/program reads this)
```

The two bounds are checked asymmetrically:
- **Lower** bound is on the **field**, before the `+0x1000` (`0x42e`):
  `field ≥ 0x08004000` → physical `≥ 0x08005000`. **The protocol can never touch
  the bootloader region; the `+0x1000` is the write protection.**
- **Upper** bound is on the **physical** address, in the erase/write routine
  (`0x442`: `r8 = 0x08080000`; `0x448`: `cmp ctx[+8], r8`): physical
  `< 0x08080000`, i.e. `field < 0x0807F000`.

Net: a hard **512 KiB physical ceiling** (`0x08080000`) — still applies on the
1 MB GD32F303 V1.5, so a bootloader-flashed app must fit `0x5000…0x80000`.

**For a flasher: build blocks with field-base `0x08004000`; the 0x5000-linked
image lands correctly at `0x5000`.** Do **not** "correct" the base to `0x5000` —
that maps to physical `0x6000`, shifts the image, and bricks the boot.

> The `0x4000–0x5000` zeros in a device dump are not an erase artifact: those
> 4096 bytes are `0x00` inside the **bootloader's own 20 KiB region** (tail
> padding of `bootloader.bin`, offsets `0x4000–0x5000`), not the app image — and
> the protocol never writes below `0x5000` anyway.

## Reconstructing the firmware

Concatenate `payload = block[6 : 6+2048]` across the data blocks in order:

```
145 data blocks × 2048 = 296960 bytes
  - real image          = 295593 bytes   (matches flashed .bin byte-for-byte)
  - trailing 0x00        =  1367 bytes   (last-block zero padding)
```

Sanity check — the payload begins with a valid STM32/GD32 Cortex-M vector table
(values below are for the `v20.1C.5-2` build):

| Vector       | Value        |
|--------------|--------------|
| Initial SP   | `0x20010000` |
| Reset        | `0x08019621` |
| NMI / faults | `0x08019665` |

The image is **linked at `0x08005000`**: the reset handler `0x08019621` falls at
file offset `0x14621` (= `reset − 0x5000`), which is real Thumb code — a CMSIS
`.data` copy loop whose literal pool holds `_sdata=0x20000000`,
`_edata=0x200018a8`. At the (wrong) base `0x4000` the same vector would point at
file offset `0x15621`, which is an ASCII string — impossible for code. This is
why the reconstructed payload concatenates straight from block 0 with no offset,
even though the app physically lands at `0x5000` (see the address-field note).

## Implementing a flasher (`tools/flash860c.py`)

1. Open the port at **57600 8N1**.
2. **Handshake**: send `0x5A` every ~31 ms and read the line; when the display is
   powered on it sends `0xA5` (ready). Stop polling once `0xA5` is seen.
3. For each 2 KiB chunk of the firmware `.bin`, with **address field**
   `A = 0x08004000 + i×0x800`, send the 2060-byte block:
   `magic(2) | A_be32(4) | payload(2048, zero-pad last) | sum_be32(4) | 0D 0A`
   - `magic` = `F0 F0`, except `F1 F1` on the final data block.
   - `sum_be32` = big-endian 32-bit sum of the 2048 payload bytes.
   - **Wait for `0x85` (ACK) before sending the next block** (ACK arrives ~60 ms
     after the block). On `0x8F` (NAK) **resend the same block**.
   - The field-base `0x08004000` is **correct** even though the app runs at
     `0x08005000` — the bootloader applies the `+0x1000` mapping. **Do not** set
     the field-base to `0x5000` or prepend a `0x1000` pad: that shifts the image
     and bricks the boot. (This is verified: the factory tool uses `0x4000` and
     `flash860c.py` reproduces its stream byte-for-byte.)
4. Send two `F2 F2` terminator blocks (address 0, 2048 zero payload, checksum 0,
   `0D 0A`) **without** waiting for an ACK.

> ✅ **Verified end-to-end as a writer.** `flash860c.py` has flashed real 860C
> firmware over this protocol and the display booted. The full handshake
> (`0xA5` ready → blocks → `0x85` ACK / `0x8F` NAK-and-resend → unACKed
> terminators) is confirmed on hardware, including checksum rejection and retry.
> Still: keep a working SWD/JTAG recovery path (`cd firmware && make flash_jtag`)
> ready — the bootloader region is never written, so a bad app flash is always
> recoverable by re-flashing.

## Tooling

- **Capture/decode** (generic AD3 UART sniffing): `lab-tools/analog-discovery/`
  (`ad3stream.py`, `ad3decode.py`) — used to record and decode the bus.
- **Flasher** (860C-specific): [`tools/flash860c.py`](../tools/flash860c.py) —
  builds/sends this block protocol from a firmware `.bin`. See
  [`tools/README.md`](../tools/README.md).

Wiring: AD3 **DIO0→display TX**, **DIO1→display RX (host side)**, **DGND→GND**.
2 MHz sampling = ~17 samples/bit at 57600, ~208 at 9600 — ample for either.

### Reproduce this capture

```sh
# 1. close the WaveForms GUI (AD3 is single-owner)
cd log                                                          # capture lands in cwd
python3 ~/Dev/Lab/lab-tools/analog-discovery/src/ad3stream.py bootloader 2000000
#    ^ start, then run the factory flash (or flash860c.py), Ctrl-C when done
python3 ~/Dev/Lab/lab-tools/analog-discovery/src/ad3decode.py bootloader_<stamp>.u8
```
