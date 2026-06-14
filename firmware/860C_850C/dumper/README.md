# Flash dumper (850C / 860C / 860C V1.5)

Streams the entire internal flash — **including the proprietary bootloader** in
the first 20K (`0x08000000`–`0x08004FFF`) — out **USART1 TX (PA9)** as Intel HEX.

On start it **latches the soft power rail** (so you don't have to hold the power
button) and turns the **LCD backlight on** as a "dumping" indicator. When the
dump finishes it **cuts the power latch and the board powers itself off** — a
clean, unambiguous end of capture.

## Why this exists

These displays ship with a proprietary bootloader and read-out protection (RDP /
GD32 SPC). An ST-Link `mass_erase` wipes everything (and clears RDP), destroying
the bootloader. This tool backs the bootloader up **first** so you can restore it
after an ST-Link flash.

It works because RDP only blocks the **SWD debug port** from reading flash — CPU
code running *from* flash can read flash freely. This app does exactly that.

## Build

```sh
make                       # -> dumper.bin (flash this), dumper.hex, dumper.lst
make BOARD=DISPLAY_860C_V12   # other pinout (default is DISPLAY_860C_V13 = V1.3/V1.5)
```

Cortex-M3 thumb; runs unmodified on the Cortex-M4 GD32F303 (V1.5). The `BOARD`
define selects the version-specific power-latch/backlight pins from `pins.h`.

## Flash it (the safe way — preserves the bootloader)

Flash `dumper.bin` **via the manufacturer bootloader update path** (same way you
install a normal firmware release), **not** via ST-Link. It is linked at the
`0x5000` bootloader offset and is accepted like any normal app.

> Do **not** run an ST-Link `mass_erase` until you have a verified dump.

## Capture (3.3V USB-serial adapter: display PA9 → adapter RX, common GND)

```sh
sudo systemctl stop ModemManager     # Fedora: stops it probing/corrupting the VCP
stty -F /dev/ttyUSB0 115200 raw -echo
cat /dev/ttyUSB0 > dump.hex           # start this first, then tap the power button
```

Tap (don't hold) the power button — the dumper latches power itself, dumps, then
powers off, ending the capture. The `:00000001FF` EOF record marks completion.
Each line is Intel-HEX checksummed; a lossy/multi-pass capture can be salvaged
with `ihex_extract.py` (validates checksums, merges duplicate passes).

## Reassemble

```sh
arm-none-eabi-objcopy -I ihex -O binary dump.hex dump.bin
dd if=dump.bin of=bootloader.bin bs=1 count=20480     # first 0x5000 = bootloader
```

**Verify before trusting:** the first 8 bytes of `bootloader.bin` must look like a
vector table — word0 a stack pointer (`0x2000xxxx`), word1 a reset handler
(`0x0800xxxx`). If it is all `0xFF`/`0x00`, GD32 SPC is blocking CPU reads on this
part and the dump is not usable.

## Restore after an ST-Link mass_erase (RDP now cleared)

```sh
openocd ... -c "init; reset halt; flash write_image erase bootloader.bin 0x08000000; reset run; shutdown"
# then flash your application .bin at 0x08005000 via the same path
```

## Config (top of `main.c`)

- `DUMP_LEN` — `0x00100000` (1MB, GD32F303 V1.5). Use `0x00080000` for 512K parts
  (GD32F103, 850C/860C ≤V1.3), or `0x00005000` for the bootloader region only.
  **Do not exceed the part's real flash size — over-reading can hard-fault.**
- `DUMP_BAUD` — `115200`.
