# Flash dumper build. Standalone; reuses the SPL, startup and linker script
# from ../src. Built WITH the bootloader offset so it can be flashed via the
# manufacturer bootloader (preserving it) rather than ST-Link.
#
#   make            -> dumper.bin (+ dumper.hex), flash this via the bootloader
#   make clean
#
# Toolchain: same arm-none-eabi-gcc as the main firmware (Cortex-M3 thumb;
# runs unmodified on the Cortex-M4 GD32F303 of the 860C V1.5).

SRCDIR = ../src

TCPREFIX = arm-none-eabi-
CC  = $(TCPREFIX)gcc
AS  = $(TCPREFIX)as
LD  = $(TCPREFIX)gcc
CP  = $(TCPREFIX)objcopy
OD  = $(TCPREFIX)objdump
SIZE = $(TCPREFIX)size

OPT = 2

INCLUDES = -I$(SRCDIR) -I$(SRCDIR)/spl/CMSIS -I$(SRCDIR)/spl/CMSIS/inc -I$(SRCDIR)/spl/inc

# BOARD selects the version-specific pinout (power latch, backlight) from
# pins.h. V13 = the 860C V1.3 / V1.5 board. Override on the command line for
# other boards, e.g. make BOARD=DISPLAY_860C_V12
# (named BOARD, not DISPLAY, to avoid clashing with the X11 DISPLAY env var)
BOARD ?= DISPLAY_860C_V13

CFLAGS  = $(INCLUDES) -D$(BOARD) -DSTM32F10X_MD -DUSE_STDPERIPH_DRIVER -DUSE_WITH_BOOTLOADER
CFLAGS += -c -fno-common -O$(OPT) -g -mcpu=cortex-m3 -mthumb
CFLAGS += -ffunction-sections -fdata-sections -fno-builtin
CFLAGS += -std=c99 --specs=nano.specs --specs=nosys.specs -Wall

AFLAGS = -mcpu=cortex-m3 -mthumb -g

LFLAGS  = -T$(SRCDIR)/stm32_flash.ld -Xlinker --defsym=USE_WITH_BOOTLOADER=1
LFLAGS += -L/usr/lib/gcc/arm-none-eabi/4.9.3/armv7-m -lgcc -lm
LFLAGS += -nostartfiles -lnosys -mcpu=cortex-m3 -mthumb -Wl,--gc-sections
LFLAGS += --specs=nano.specs --specs=nosys.specs -Xlinker -Map=dumper.map

OBJDIR = _build

# Only the SPL units we touch (gc-sections drops anything unused anyway), plus
# the CMSIS system file for SystemInit/SystemCoreClockUpdate.
SPL_SOURCES = \
  $(SRCDIR)/spl/CMSIS/system_stm32f10x.c \
  $(SRCDIR)/spl/src/stm32f10x_rcc.c \
  $(SRCDIR)/spl/src/stm32f10x_gpio.c \
  $(SRCDIR)/spl/src/stm32f10x_usart.c \
  $(SRCDIR)/spl/src/stm32f10x_flash.c \
  $(SRCDIR)/spl/src/misc.c

SOURCES = main.c $(SPL_SOURCES)
OBJECTS = $(foreach x, $(basename $(SOURCES)), $(OBJDIR)/$(x).o)

all: dumper.bin size

dumper.bin: dumper.elf
	$(CP) -Obinary dumper.elf dumper.bin
	$(CP) -Oihex   dumper.elf dumper.hex
	$(OD) -S dumper.elf > dumper.lst

dumper.elf: $(OBJECTS) $(OBJDIR)/startup.o
	$(LD) $^ $(LFLAGS) -o $@

$(OBJDIR)/startup.o: $(SRCDIR)/startup_stm32f10x_md.s
	@mkdir -p $(dir $@)
	$(AS) $(AFLAGS) $< -o $@

$(OBJDIR)/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $< -o $@

size:
	@echo "Size:"
	$(SIZE) dumper.elf

clean:
	rm -f dumper.elf dumper.bin dumper.hex dumper.lst dumper.map
	rm -rf $(OBJDIR)
