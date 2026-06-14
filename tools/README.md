# 860C bootloader tools

`flash860c.py` — flash 860C firmware over the reverse-engineered bootloader UART
protocol (see [`../docs/bootloader-uart-protocol.md`](../docs/bootloader-uart-protocol.md)).

> **Capture/decode tooling** (generic AD3 UART sniffing — `ad3stream.py`,
> `ad3decode.py`) lives in `lab-tools/analog-discovery/` (`ramb0t/lab-tools`),
> since it's instrument-generic and reusable. It was used to derive this protocol.

## Flash — `flash860c.py`

Builds the bootloader block stream from a firmware `.bin` and optionally transmits
it at 57600 8N1.

```
python3 flash860c.py firmware.bin                       # dry-run: build + self-verify
python3 flash860c.py firmware.bin --out stream.bin      # also save the framed stream
python3 flash860c.py firmware.bin --flash --port /dev/ttyACM0 --yes   # actually flash
```

- **Dry-run by default** — builds the stream, self-verifies every block's address
  + checksum, transmits nothing. Verified to produce a **byte-identical** stream
  to the captured vendor flash.
- **Flashing is ACK-gated**: polls `0x5A` until the display sends `0xA5`
  (ready / power-on), then sends each data block and waits for `0x85` (ACK) before
  the next; on `0x8F` (NAK) it resends. `F2F2` terminators are sent without ACK.
- `--flash` requires **both** `--port` and `--yes` or it refuses.
- `--corrupt-block N` — send block N with a bad checksum first (probe the NAK
  path), print the display's reply, then send it correctly.
- Options: `--ready-byte` (`0xA5`), `--ack-byte` (`0x85`), `--nak-byte` (`0x8F`),
  `--poll-ms`, `--ready-timeout`, `--block-timeout`, `--retries`,
  `--corrupt-block N`.
- `--base` is the protocol **address-field** base (default `0x08004000`), **not**
  the physical load address — the app runs at `0x08005000` and the bootloader
  applies a `+0x1000` mapping. **Leave it at `0x4000`** (matches the factory tool
  byte-for-byte); changing it shifts the image and bricks the boot. The image
  must fit the bootloader's **512 KiB write cap** (`0x5000…0x80000`), which holds
  even on the 1 MB GD32F303 V1.5. See `docs/bootloader-uart-protocol.md`.

> ✅ Verified flashing real 860C firmware on hardware (handshake, ACK/NAK-resend,
> checksum validation all confirmed).
>
> ⚠️ **Brick risk** still applies — only flash with a working SWD/JTAG recovery
> path ready (`cd firmware && make flash_jtag`). The bootloader region is never
> written, so a bad app flash is recoverable by re-flashing.

Requires `pyserial`.
