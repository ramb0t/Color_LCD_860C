# Motor ⇄ Display UART Protocol (TSDZ2 / TSDZ8)

This documents the serial protocol between the **display** (850C / 860C / SW102)
and the **motor controller** (TSDZ2 / TSDZ8). The display is the UART master of
the conversation: it drives the boot handshake and, once running, polls the
motor on a fixed cadence.

It was reverse-engineered from the display firmware in this repo —
`firmware/common/src/state.c` (frame build/parse) and
`firmware/860C_850C/src/usart1.c` (wire framing / RX state machine). A working
reference implementation of the **motor** side lives in
[`tools/motor_emulator.py`](../tools/motor_emulator.py).

> This is the **runtime motor protocol**. It is unrelated to the bootloader's
> firmware-update protocol (`docs/bootloader-uart-protocol.md`).

## Physical layer

| Parameter | Value |
|-----------|-------|
| Baud rate | 19200 |
| Frame     | 8 data bits, no parity, 1 stop bit (8N1) |
| Levels    | 3.3 V UART (use a 3.3 V USB-serial adapter on the bench) |
| Wiring    | display TX → motor RX, motor TX → display RX, common GND |

Source: `firmware/860C_850C/src/usart1.c` (`USART_BaudRate = 19200`,
`USART_WordLength_8b`, `USART_StopBits_1`, `USART_Parity_No`).

## Frame format

Both directions use the same structure. Only the start byte differs.

```
+--------+--------+--------+-----------------+---------+---------+
| START  |  len   |  type  |   payload ...   | crc_lo  | crc_hi  |
+--------+--------+--------+-----------------+---------+---------+
  byte 0   byte 1   byte 2   bytes 3..len-1   byte len  byte len+1
```

| Field   | Meaning |
|---------|---------|
| `START` | `0x43` for **motor → display**, `0x59` for **display → motor**. |
| `len`   | Number of bytes from `START` through the last payload byte, i.e. `3 + len(payload)`. **This is the count the CRC covers**, *not* including the CRC bytes. |
| `type`  | Frame type (see table below). |
| payload | `len - 3` bytes, type-specific. |
| `crc`   | CRC-16 over bytes `[0 .. len-1]`, appended **low byte first**. |

**Total bytes on the wire = `len + 2`.**

The display's RX state machine (`usart1.c`) reads the start byte, then `len`,
then exactly `len` further bytes (payload + the two CRC bytes), then validates
the CRC before accepting the frame. It holds only **one** received packet at a
time — a new packet that arrives before the previous one is consumed is dropped.
So the emulator should send at most one reply per request and avoid flooding.

### CRC

CRC-16/MODBUS: polynomial `0xA001` (reflected `0x8005`), initial value
`0xFFFF`, no final XOR, result stored little-endian. Reference (`crc16()` in
`firmware/common/src/utils.c`):

```c
void crc16(uint8_t ui8_data, uint16_t *ui16_crc) {
    *ui16_crc ^= (uint16_t) ui8_data;
    for (unsigned i = 8; i > 0; i--) {
        if (*ui16_crc & 0x0001)
            *ui16_crc = (*ui16_crc >> 1) ^ 0xA001;
        else
            *ui16_crc >>= 1;
    }
}
```

Check value: CRC of the ASCII bytes `"123456789"` is `0x4B37`.

## Frame types

`frame_type_t` in `firmware/common/src/state.c`:

| Value | Name               | Direction in handshake |
|-------|--------------------|------------------------|
| 0     | `ALIVE`            | motor → display (unprompted, boot only) |
| 1     | `STATUS`           | both — display requests, motor reports init status |
| 2     | `PERIODIC`         | both — display polls, motor returns telemetry |
| 3     | `CONFIGURATIONS`   | display → motor (settings push) |
| 4     | `FIRMWARE_VERSION` | both — display requests, motor reports version |

The display only *processes* a reply whose `type` matches the frame it is
currently waiting for (gated by the `g_motor_init_state` machine). Practical
rule for emulating the motor: **reply to each received `0x59` frame with a
`0x43` frame of the same type.** The single exception is `ALIVE`, which the
motor must send unprompted (see handshake step 1).

## Boot handshake

The display boots in state `MOTOR_INIT_GET_MOTOR_ALIVE` → `WAIT_MOTOR_ALIVE`
and walks this sequence (`motor_init()` / `communications()` in `state.c`):

1. **Wait for ALIVE.** The display sends nothing; it waits for an unprompted
   `ALIVE` (type 0) frame from the motor. → on receipt, advances to request the
   firmware version.
2. **Firmware version.** Display sends a `FIRMWARE_VERSION` (type 4) request;
   motor replies `FIRMWARE_VERSION` with `[error_states, major, minor, patch]`.
   The display **rejects the motor** unless `major`/`minor` match the build's
   `TSDZ2_FIRMWARE_MAJOR`/`MINOR` (in `firmware/common/Makefile.common`) and
   `patch >= 43`.
3. **Configurations.** Display sends `CONFIGURATIONS` (type 3, 88-byte frame).
   No reply is required for the config frame itself.
4. **Status poll.** Display sends `STATUS` (type 1) requests; motor replies
   `STATUS` with one payload byte = the init status. Returning
   `MOTOR_INIT_STATUS_INIT_OK` (`2`) moves the display straight to `READY`.
   (Values: `0` RESET, `1` GOT_CONFIG, `2` INIT_OK.)
5. **Running.** In `READY` the display streams `PERIODIC` (type 2) requests
   every 100 ms; the motor replies with a `PERIODIC` telemetry frame each time.

If the display reboots, it returns to step 1 (sending nothing). A motor/emulator
must therefore resume unprompted `ALIVE` frames when the `PERIODIC` stream stops,
or the display will hang at "waiting for motor".

## Frame payloads

Byte offsets below are **absolute positions in the frame** (i.e. the same
`p_rx_buffer[i]` indices used in `state.c`). Payload starts at offset 3.

### ALIVE (type 0), motor → display

No payload. `len = 3`, total 5 bytes on the wire.

### FIRMWARE_VERSION (type 4), motor → display

`len = 7`, total 9 bytes.

| Offset | Field |
|--------|-------|
| 3 | error states |
| 4 | version major |
| 5 | version minor |
| 6 | version patch |

### STATUS (type 1), motor → display

`len = 4`, total 6 bytes.

| Offset | Field |
|--------|-------|
| 3 | motor init status (`0` RESET / `1` GOT_CONFIG / `2` INIT_OK) |

### PERIODIC (type 2), motor → display

`len = 27`, total **29 bytes** (`UART_NUMBER_DATA_BYTES_TO_RECEIVE`). This is the
live telemetry frame parsed in `communications()`:

| Offset | Field | Notes |
|--------|-------|-------|
| 3  | battery voltage, low 8 bits | full value = `[3] \| ((([4] & 0x30) << 4))` (10-bit ADC); volts = `adc * 866 / 10000` |
| 4  | battery voltage high bits | bits 4–5 (mask `0x30`) are voltage bits 8–9 |
| 5  | battery current `_x5` | amps ×5 |
| 6  | wheel speed `_x10`, low | value = `([6] \| ([7] << 8)) & 0x3FF`, km/h ×10 |
| 7  | wheel speed high + torque high bits | low 2 bits of speed; bits 6–7 (`0xC0`) feed torque-sensor high bits |
| 8  | status flags | bit0 braking, bits1–3 hall sensors, bit4 speed-limit-high, bit5 voltage-cutoff, bit6 voltage-shutdown, bit7 pwm-frequency |
| 9  | ADC throttle | |
| 10 | motor temperature **or** throttle ADC map | meaning depends on `optional_ADC_function` |
| 11 | pedal torque sensor, low 8 bits | high bits come from `[7] & 0xC0` |
| 12 | pedal torque delta, low | |
| 13 | pedal torque delta, high | |
| 14 | pedal cadence | rpm |
| 15 | motor PWM duty cycle | |
| 16 | motor speed ERPS, low | |
| 17 | motor speed ERPS, high | |
| 18 | FOC angle / field-weakening | bits 0–3 FOC angle, bits 4–5 field-weakening (patch ≥ 52) |
| 19 | error states | `0` = no error |
| 20 | motor current `_x5` | amps ×5 |
| 21 | wheel-speed tick counter, byte 0 | 24-bit little-endian |
| 22 | wheel-speed tick counter, byte 1 | |
| 23 | wheel-speed tick counter, byte 2 | |
| 24 | pedal torque delta boost, low | |
| 25 | pedal torque delta boost, high | |
| 26 | pedal torque increment (patch ≥ 52) / ADC battery current (older) | |

### Display → motor frames

The emulator does not need to interpret these to satisfy the display — it only
needs to recognise the `type` byte and reply in kind. For completeness, the
build logic is `rt_send_tx_package()` in `state.c`:

- **PERIODIC (type 2)** — `len = 13`. Carries assist level, lights/walk/cruise
  flags, power limit, riding mode, speed limits, and virtual throttle.
- **CONFIGURATIONS (type 3)** — `len = 86`, total 88 bytes
  (`UART_NUMBER_DATA_BYTES_TO_SEND`). Full bike/motor configuration push
  (low-voltage cutoff, wheel perimeter, max current, assist tables, etc.).
- **STATUS (type 1)** and **FIRMWARE_VERSION (type 4)** — `len = 3`, request-only
  frames (header + type, no payload) that ask the motor to report.

## Worked example

A complete `ALIVE` frame (motor → display):

```
0x43 0x03 0x00 <crc_lo> <crc_hi>
```

CRC-16/MODBUS over `43 03 00` = `0xE480` → `crc_lo = 0x80`, `crc_hi = 0xE4`.
Full frame: `43 03 00 80 E4`.

## See also

- [`tools/motor_emulator.py`](../tools/motor_emulator.py) — runnable motor
  emulator that implements everything above.
- `firmware/common/src/state.c` — `rt_send_tx_package()` (build),
  `communications()` (parse), `motor_init()` (handshake state machine).
- `firmware/860C_850C/src/usart1.c` — RX state machine and wire framing.
- `firmware/common/src/utils.c` — `crc16()`.
- `firmware/common/include/uart.h` — buffer-size constants.
