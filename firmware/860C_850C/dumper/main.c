/*
 * Flash dumper for 850C / 860C (STM32F10x / GD32F10x / GD32F303) displays
 *
 * Streams the entire internal flash (including the proprietary bootloader in
 * the first 20K) out USART1 as Intel HEX. Built with USE_WITH_BOOTLOADER so it
 * links at 0x08000000 + 0x5000 and is accepted by the manufacturer bootloader
 * update path -- flashing it this way PRESERVES the bootloader and does NOT
 * touch read-out protection (unlike an ST-Link mass_erase).
 *
 * Read-out protection blocks the SWD debug port from reading flash, but CPU
 * code executing from flash can read flash freely, which is what this does.
 * (Confirmed on GD32F303RGT6 / 860C V1.5: SPC does not block CPU reads.)
 *
 * Behaviour:
 *   1. asserts the soft power latch  -> no need to hold the power button
 *   2. turns the LCD backlight on    -> visual "dumping" indication
 *   3. streams flash as Intel HEX out USART1 TX (PA9), 8N1, DUMP_BAUD
 *   4. cuts the power latch when done -> the board powers itself off
 *
 * The pinout (power latch, backlight) is version specific; build with the
 * matching -DDISPLAY_* (Makefile defaults to DISPLAY_860C_V13, which is the
 * 860C V1.3 / V1.5 pinout).
 *
 * Capture on a PC via the display UART (e.g. ST-Link VCP) -> dump.hex, then:
 *   arm-none-eabi-objcopy -I ihex -O binary dump.hex dump.bin
 *   # or use ihex_extract.py to validate + merge a lossy/multi-pass capture
 *
 * Released under the GPL License, Version 3.
 */

#include "stm32f10x.h"
#include "stm32f10x_rcc.h"
#include "stm32f10x_gpio.h"
#include "stm32f10x_usart.h"
#include "stm32f10x_flash.h"
#include "pins.h"
#include <stdint.h>

/* --- configuration --------------------------------------------------------- */
#define DUMP_BAUD        115200u
#define FLASH_BASE_ADDR  0x08000000u
/* GD32F303RGT6 (860C V1.5) = 1MB. GD32F103 (850C/860C <=V1.3) = 512K.
 * Set to 0x00005000 if you only want the 20K bootloader region. Over-reading
 * past the real flash size can hard-fault, so do not exceed the part's flash. */
#define DUMP_LEN         0x00100000u
/* Pause after each HEX line so a slow/over-eager host (ModemManager probing,
 * VCP buffering) never drops bytes. 0 to disable. */
#define LINE_DELAY_MS    1u

/* --- SysTick busy-delay (no interrupts) ------------------------------------ */
static void delay_ms(uint32_t ms)
{
  if (ms == 0) return;
  SysTick->LOAD = (SystemCoreClock / 1000u) - 1u;
  SysTick->VAL  = 0u;
  SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk | SysTick_CTRL_ENABLE_Msk;
  while (ms--)
    while ((SysTick->CTRL & SysTick_CTRL_COUNTFLAG_Msk) == 0u) { }
  SysTick->CTRL = 0u;
}

/* --- clock (matches main firmware SetSysClockTo128Mhz) --------------------- */
static void clock_init(void)
{
  RCC_DeInit();
  RCC_HSEConfig(RCC_HSE_ON);
  if (RCC_WaitForHSEStartUp() != SUCCESS)
    while (1) { }                       /* no HSE -> stuck; nothing we can do */

  FLASH_PrefetchBufferCmd(FLASH_PrefetchBuffer_Enable);
  FLASH_SetLatency(FLASH_Latency_2);

  RCC_HCLKConfig(RCC_SYSCLK_Div1);
  RCC_PCLK2Config(RCC_HCLK_Div1);       /* USART1 is on APB2 (PCLK2) */
  RCC_PCLK1Config(RCC_HCLK_Div2);
  RCC_PLLConfig(RCC_PLLSource_HSE_Div1, RCC_PLLMul_16);   /* 8MHz * 16 = 128MHz */
  RCC_PLLCmd(ENABLE);
  while (RCC_GetFlagStatus(RCC_FLAG_PLLRDY) == RESET) { }
  RCC_SYSCLKConfig(RCC_SYSCLKSource_PLLCLK);
  while (RCC_GetSYSCLKSource() != 0x08) { }

  SystemCoreClockUpdate();              /* baud + delay_ms depend on this */
}

/* --- power latch + backlight (version-specific pins from pins.h) ----------- */
static void board_power_on(void)
{
  GPIO_InitTypeDef gpio;

  RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA | RCC_APB2Periph_GPIOC |
#if defined(DISPLAY_860C_V12) || defined(DISPLAY_860C_V13)
                         RCC_APB2Periph_GPIOD |
#endif
                         RCC_APB2Periph_AFIO, ENABLE);

  gpio.GPIO_Speed = GPIO_Speed_50MHz;
  gpio.GPIO_Mode  = GPIO_Mode_Out_PP;

  gpio.GPIO_Pin = SYSTEM_POWER_ON_OFF__PIN;
  GPIO_Init(SYSTEM_POWER_ON_OFF__PORT, &gpio);
  GPIO_SetBits(SYSTEM_POWER_ON_OFF__PORT, SYSTEM_POWER_ON_OFF__PIN);

#if defined(DISPLAY_860C_V12) || defined(DISPLAY_860C_V13)
  gpio.GPIO_Pin = SYSTEM_POWER_2_ON_OFF__PIN;
  GPIO_Init(SYSTEM_POWER_2_ON_OFF__PORT, &gpio);
  GPIO_SetBits(SYSTEM_POWER_2_ON_OFF__PORT, SYSTEM_POWER_2_ON_OFF__PIN);
#endif

  gpio.GPIO_Pin = USB_CHARGE__PIN;
  GPIO_Init(USB_CHARGE__PORT, &gpio);
  GPIO_SetBits(USB_CHARGE__PORT, USB_CHARGE__PIN);

  /* backlight full-on as a plain output (app drives it via timer PWM) */
  gpio.GPIO_Pin = LCD_BACKLIGHT__PIN;
  GPIO_Init(LCD_BACKLIGHT__PORT, &gpio);
  GPIO_SetBits(LCD_BACKLIGHT__PORT, LCD_BACKLIGHT__PIN);
}

static void board_power_off(void)
{
  GPIO_ResetBits(LCD_BACKLIGHT__PORT, LCD_BACKLIGHT__PIN);
  GPIO_ResetBits(USB_CHARGE__PORT, USB_CHARGE__PIN);
#if defined(DISPLAY_860C_V12) || defined(DISPLAY_860C_V13)
  GPIO_ResetBits(SYSTEM_POWER_2_ON_OFF__PORT, SYSTEM_POWER_2_ON_OFF__PIN);
#endif
  GPIO_ResetBits(SYSTEM_POWER_ON_OFF__PORT, SYSTEM_POWER_ON_OFF__PIN);  /* board dies here */
}

/* --- USART1 TX (PA9) ------------------------------------------------------- */
static void usart1_init(void)
{
  GPIO_InitTypeDef  gpio;
  USART_InitTypeDef usart;

  RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1, ENABLE);   /* GPIOA already on */

  gpio.GPIO_Pin   = USART1_TX__PIN;     /* PA9 */
  gpio.GPIO_Speed = GPIO_Speed_50MHz;
  gpio.GPIO_Mode  = GPIO_Mode_AF_PP;
  GPIO_Init(USART1__PORT, &gpio);

  USART_DeInit(USART1);
  usart.USART_BaudRate            = DUMP_BAUD;
  usart.USART_WordLength          = USART_WordLength_8b;
  usart.USART_StopBits            = USART_StopBits_1;
  usart.USART_Parity              = USART_Parity_No;
  usart.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
  usart.USART_Mode                = USART_Mode_Tx;
  USART_Init(USART1, &usart);
  USART_Cmd(USART1, ENABLE);
}

static void tx(uint8_t b)
{
  while (USART_GetFlagStatus(USART1, USART_FLAG_TXE) == RESET) { }
  USART_SendData(USART1, b);
}

/* --- Intel HEX emitter ----------------------------------------------------- */
static void emit_nibble(uint8_t n) { tx(n < 10 ? (uint8_t)('0' + n) : (uint8_t)('A' + n - 10)); }
static void emit_byte(uint8_t b)   { emit_nibble(b >> 4); emit_nibble(b & 0x0F); }
static void emit_eol(void)         { tx('\r'); tx('\n'); delay_ms(LINE_DELAY_MS); }

/* type 04: set bits 16..31 of the address */
static void ihex_ext(uint16_t upper)
{
  uint8_t sum = 0x02 + 0x04 + (uint8_t)(upper >> 8) + (uint8_t)(upper & 0xFF);
  tx(':');
  emit_byte(0x02); emit_byte(0x00); emit_byte(0x00); emit_byte(0x04);
  emit_byte(upper >> 8); emit_byte(upper & 0xFF);
  emit_byte((uint8_t)(0u - sum));
  emit_eol();
}

/* type 00: 'len' data bytes at lower-16 'addr' */
static void ihex_data(uint16_t addr, const uint8_t *data, uint8_t len)
{
  uint8_t sum = len + (uint8_t)(addr >> 8) + (uint8_t)(addr & 0xFF) /* + type 0x00 */;
  uint8_t i;
  tx(':');
  emit_byte(len);
  emit_byte(addr >> 8); emit_byte(addr & 0xFF);
  emit_byte(0x00);
  for (i = 0; i < len; i++) { emit_byte(data[i]); sum += data[i]; }
  emit_byte((uint8_t)(0u - sum));
  emit_eol();
}

static void ihex_eof(void)
{
  tx(':'); emit_byte(0x00); emit_byte(0x00); emit_byte(0x00);
  emit_byte(0x01); emit_byte(0xFF);
  emit_eol();
}

int main(void)
{
  uint32_t off;
  uint16_t cur_upper = 0xFFFF;          /* force a leading type-04 record */

  clock_init();
  board_power_on();                     /* latch power + backlight on */
  usart1_init();

  for (off = 0; off < DUMP_LEN; off += 16)
  {
    uint32_t addr  = FLASH_BASE_ADDR + off;
    uint16_t upper = (uint16_t)(addr >> 16);
    if (upper != cur_upper) { ihex_ext(upper); cur_upper = upper; }
    ihex_data((uint16_t)(addr & 0xFFFF), (const uint8_t *)addr, 16);
  }
  ihex_eof();

  /* let the last byte fully shift out, then power the board off */
  while (USART_GetFlagStatus(USART1, USART_FLAG_TC) == RESET) { }
  delay_ms(50);
  board_power_off();

  while (1) { }                         /* (only reached if power is externally held) */
}
