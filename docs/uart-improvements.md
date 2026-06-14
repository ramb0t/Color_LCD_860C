# UART Implementation — Review & Improvements

Review of the **runtime motor UART** driver on the color displays
(`firmware/860C_850C/src/usart1.c`, `uart.c`) and the SW102 equivalent
(`firmware/SW102/src/sw102/uart.c`). For the wire protocol itself see
[`docs/motor-uart-protocol.md`](motor-uart-protocol.md).

## How it works today

- **RX**: interrupt-driven byte state machine in `USART1_IRQHandler`
  (`usart1.c:92`). State 0 waits for start byte `0x43`; state 1 reads the `len`
  byte into `ui8_rx[1]`; state 2 accumulates `len` more bytes, runs CRC-16, and
  on a match `memcpy`s the frame into `ui8_rx_buffer` and sets
  `ui8_received_package_flag`.
- **TX**: DMA (DMA1_Channel4 → `USART1->DR`). `uart_send_tx_buffer()` just calls
  `usart1_start_dma_transfer(len)` (`usart1.c:168`).
- **Consumer**: polls `uart_get_rx_buffer_rdy()` (`uart.c:18`), which returns the
  buffer and clears the flag.

Overall the design is sound: ISR-driven RX, DMA TX, CRC validation, overrun
(`ORE`) flag cleared so the peripheral can't lock up, and a single-frame hold
flag that prevents torn reads. The items below are the gaps.

## Issues

### 1. RX buffer overflow — no bounds check on `len` (correctness / safety)

**Where:** `usart1.c:133-160` (and the same pattern in
`SW102/src/sw102/uart.c`).

`ui8_rx[1]` is the length byte **from the wire** and is never validated against
the buffer size (`UART_NUMBER_DATA_BYTES_TO_RECEIVE` = 29).

```c
case 2:
  ui8_rx[ui8_rx_cnt + 2] = ui8_byte_received;   // line 134
  ++ui8_rx_cnt;
  if (ui8_rx_cnt >= ui8_rx[1])                   // line 138 — stop at wire len
  {
    ...
    memcpy(ui8_rx_buffer, ui8_rx, ui8_rx[1] + 2); // line 160
  }
```

The last write index is `ui8_rx[1] + 1`. The array `ui8_rx[29]` is valid for
indices `0..28`, so any `len >= 28` writes past the end of `ui8_rx`, and the
`memcpy` then overruns `ui8_rx_buffer` as well. A corrupted length byte — UART
noise, a brownout glitch, a half-connected motor cable — is enough to trigger
it. Clean bench traffic never exceeds `len = 27`, so it stays hidden until the
line gets noisy.

`ui8_rx` is function-static, so the corruption hits adjacent statics rather than
a return address, but it is still silent memory corruption.

**Fix:** reject a bad length and resync before writing.

```c
case 1:
  ui8_rx[1] = ui8_byte_received;
  if (ui8_rx[1] > UART_NUMBER_DATA_BYTES_TO_RECEIVE - 2) {
    ui8_state_machine = 0;   // impossible length, drop and hunt for next start
  } else {
    ui8_state_machine = 2;
  }
  break;
```

(Guarding in state 1 is cheapest — it rejects before a single payload byte is
stored. A guard at the top of state 2 works too.)

### 2. No framing timeout / idle-line resync (robustness)

Resync today happens only via the length byte plus CRC failure. If a length
byte is corrupted *smaller-but-valid*, the machine consumes the next real
frame's bytes as payload, fails CRC, and discards — recovering only after one or
more good frames are lost. There is no UART IDLE-line interrupt or inter-frame
timeout to force a clean restart.

**Improvement:** enable the USART IDLE interrupt and reset the state machine
(`ui8_state_machine = 0; ui8_rx_cnt = 0;`) on an idle line. At 19200 baud frames
are short and the motor polls on a fixed cadence, so an idle gap reliably marks
a frame boundary. Low effort, faster recovery, and it composes with fix #1.

### 3. Dead TXE branch (cleanup)

`usart1.c:107-111` handles `USART_IT_TXE`, but the TXE interrupt is never
enabled (only `USART_IT_RXNE` is, at `usart1.c:88`); TX goes out via DMA. The
branch can never run. Remove it to make the ISR's actual responsibilities clear.

### 4. `uart_send_tx_buffer()` ignores its argument (API honesty)

```c
void uart_send_tx_buffer(uint8_t *tx_buffer, uint8_t ui8_len) {
  usart1_start_dma_transfer(ui8_len);   // tx_buffer unused
}
```

The DMA source address is fixed once in `usart1_init()` to `uart_get_tx_buffer()`
(`usart1.c:38`). The `tx_buffer` parameter is ignored, and the call only works
because every caller writes that same global before calling. Either honor the
argument (`DMA_SetMemoryBaseAddr` per transfer) or drop the parameter so the
signature stops implying flexibility that isn't there.

### 5. Empty `uart_init()` (minor)

`uart_init()` (`uart.c:10`) is empty; the real setup is `usart1_init()`. Either
have `uart_init()` call `usart1_init()` or document that `usart1_init()` is the
entry point, so the layering reads cleanly.

## Priority

| # | Issue | Severity | Effort |
|---|-------|----------|--------|
| 1 | RX length-byte overflow | High (memory corruption) | Low |
| 2 | Idle-line resync | Medium (robustness) | Low–Med |
| 3 | Dead TXE branch | Low (cleanup) | Trivial |
| 4 | `uart_send_tx_buffer` arg | Low (clarity) | Trivial |
| 5 | Empty `uart_init` | Low (clarity) | Trivial |

Do #1 first — it is the only one that can corrupt memory, and the fix is a
two-line bounds check. Apply the same fix to the SW102 driver, which carries the
identical state machine.

## See also

- [`docs/motor-uart-protocol.md`](motor-uart-protocol.md) — the wire protocol.
- `firmware/860C_850C/src/usart1.c` — RX state machine, DMA TX.
- `firmware/SW102/src/sw102/uart.c` — SW102 driver (same RX pattern).
- `firmware/common/include/uart.h` — buffer-size constants.
