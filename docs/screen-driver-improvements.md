# Planned Improvements: Screen Driver (850C/860C, incl. GD32F303 V1.5)

Notes on improving the LCD draw path in
`firmware/860C_850C/src/ugui_driver/ugui_display_8x0c.c`. Context: the new
860C V1.5 uses a **GD32F303RGT6 (LQFP64)**.

## Background

- No RAM framebuffer exists, and none is possible: 320×240×16bpp = ~150 KB,
  while the F103/GD32 has 20–64 KB RAM. Pixels are written directly into the
  LCD controller's own GRAM.
- The bus is a **bit-banged 8080 parallel interface**: data on `GPIOB[0:15]`
  (`LCD_BUS__PORT`), control (DC/CS/WR/RD/RES) on GPIOC. Each write sets
  `GPIOB->ODR` then pulses WR low/high in `lcd_write_cycle()`, with the strobe
  width set by a NOP-delay loop (`wait_pulse`, `write_pulse_duration`). No DMA.
- The shared screen/`Field` layer (`common/`) already does **dirty-field
  redraw** — only changed fields are repushed each 20 ms tick. The common case
  is therefore already cheap; gains below concentrate on plots, screen
  switches, large fonts, slow panels, and freeing CPU for the motor UART.

## FSMC/EXMC is NOT available — do not pursue

The hardware parallel-LCD controller (FSMC on STM32F1, EXMC on GD32F30x) is
only bonded out on **100-pin (V) and 144-pin (Z)** packages, and uses fixed
pins on **GPIOD/GPIOE**. The **GD32F303RGT6 is LQFP64** — it lacks GPIOE and
nearly all of GPIOD, so the peripheral is not pinned out at all. Our bus is on
GPIOB and control on GPIOC, which would not match FSMC's pinout regardless.
Conclusion: the bit-bang GPIO driver is the only option on this part.

## 1. Retune the NOP delay — REQUIRED (correctness, not perf)

`write_pulse_duration` (default 75, dropped to 0–3 per panel) sets the WR
strobe width as a count of NOPs. This is **CPU-clock-dependent**: at the
F303's higher core clock the same NOP count produces a *shorter* real pulse,
which can drop below the panel's ~100 ns minimum write cycle (already flagged
`FIXME` in `lcd_write_cycle`) → blank or flaky display.

Action: re-derive `write_pulse_duration` for the actual F303 core clock, or
better, gate the strobe on a hardware timer so it is expressed in ns, not
NOPs. This is mandatory for the port to work reliably.

## 2. Inline the write cycle + span fills — LOW EFFORT, BROAD BENEFIT

- Inline `lcd_write_cycle()` / drop the `volatile` loop entirely when
  `write_pulse_duration == 0`. Per-pixel function-call + loop-setup overhead
  dominates once the NOP count is small.
- Add a horizontal-run (span) fill for font glyphs and plots. `HW_FillFrame`
  already sets the color once and only strobes WR; `lcd_pixel_set` and glyph
  rendering still take the full per-pixel path including a `C_TRANSPARENT`
  test each pixel.
- Set the GRAM window once per field (`lcd_set_xy`) and stream, rather than
  re-addressing per pixel. Hoist `C_TRANSPARENT` checks out of inner loops.

## 3. DMA + timer-driven GPIO ("poor man's FSMC") — HIGH EFFORT, HIGHEST CEILING

Even without FSMC, the GD32F303 has DMA and timers:

- RAM pixel buffer → **DMA** → `GPIOB->ODR` (the data bus is a whole port,
  which suits this directly).
- A **timer** fires the DMA request *and* drives the WR strobe (PC5) via
  PWM/output-compare on the same timebase, keeping data and WR edge in sync.

Result: pixels stream CPU-free, timing is in HCLK units (removing the fragile
NOP coupling), and throughput approaches FSMC. The tricky part is WR/data
synchronization. Pursue only if measurement shows a real bottleneck (slow
panels, heavy plot/screen-switch redraws) or CPU headroom for the UART is
needed.

## Rough throughput reference (320×240 = 76,800 px, ~128 MHz)

| Path                                  | per pixel | full screen |
|---------------------------------------|-----------|-------------|
| Bit-bang, 75 NOPs (slow panels)       | ~1.9 µs   | ~150 ms     |
| Bit-bang, 0 NOPs (fast path)          | ~80–120 ns| ~7 ms       |
| DMA + timer GPIO (est.)               | ~30 ns    | ~2.4 ms, CPU free |

(FSMC would land near the DMA row but is not available on LQFP64.)

## Priority

1. NOP retune — required for the F303 port to work at all.
2. Inline write cycle + span fills — cheap, helps everywhere.
3. DMA + timer GPIO — only if a measured bottleneck justifies the effort.
