#!/usr/bin/env python3
"""
TSDZ2/TSDZ8 motor emulator for the 850C/860C color display.

Pretends to be the motor controller so a display connected over a USB-serial
adapter completes its boot handshake and shows live (faked) telemetry. Lets you
test a display with no motor on the bench.

Wiring / port
-------------
The display's UART (TX/RX, GND) must reach this PC through a 3.3V USB-serial
adapter. The display's TX -> adapter RX, adapter TX -> display RX, common GND.
Default port is /dev/ttyACM2 (override with argv[1]). 19200 baud, 8N1.

Protocol (reverse-engineered from firmware/common/src/state.c +
firmware/860C_850C/src/usart1.c)
--------------------------------------------------------------------------
Every frame:  [START][len][type][payload...][crc_lo][crc_hi]
  START = 0x43 for motor->display, 0x59 for display->motor
  len   = number of bytes from START through the last payload byte
          (i.e. header+type+payload). Total wire bytes = len + 2.
  CRC   = CRC-16/MODBUS (poly 0xA001, init 0xFFFF) over bytes [0 .. len-1],
          appended low byte first.

Frame types (frame_type_t):
  0 ALIVE   1 STATUS   2 PERIODIC   3 CONFIGURATIONS   4 FIRMWARE_VERSION

Handshake (display-driven). The display:
  1. waits for an unprompted ALIVE frame from the motor
  2. requests FIRMWARE_VERSION  -> we reply FIRMWARE_VERSION (must match the
     motor version the display was built for: major.minor / patch>=43)
  3. sends CONFIGURATIONS, then polls STATUS -> we reply STATUS=INIT_OK(2)
  4. enters READY and streams PERIODIC requests -> we reply PERIODIC telemetry

Rule of thumb: reply to each received 0x59 frame with a 0x43 frame of the SAME
type. The only unprompted frame we send is ALIVE, and only until the display
starts talking back.
"""

import sys
import time
import serial   # pyserial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM2"
BAUD = 19200

START_MOTOR = 0x43   # motor -> display
START_DISP = 0x59    # display -> motor

FRAME_ALIVE = 0
FRAME_STATUS = 1
FRAME_PERIODIC = 2
FRAME_CONFIGURATIONS = 3
FRAME_FIRMWARE_VERSION = 4

FRAME_NAME = {
    0: "ALIVE", 1: "STATUS", 2: "PERIODIC",
    3: "CONFIGURATIONS", 4: "FIRMWARE_VERSION",
}

# Motor firmware version the display expects. Must match
# TSDZ2_FIRMWARE_MAJOR/MINOR in firmware/common/Makefile.common and patch>=43.
FW_MAJOR, FW_MINOR, FW_PATCH = 0, 21, 52

MOTOR_INIT_STATUS_INIT_OK = 2   # tells display "config accepted, ready"

# ADC step: volts = adc * 866 / 10000  (ADC_BATTERY_VOLTAGE_PER_ADC_STEP_X10000)
def volts_to_adc(volts):
    return int(round(volts * 10000 / 866)) & 0x3FF


def crc16(data):
    """CRC-16/MODBUS, identical to crc16() in firmware/common/src/utils.c."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def build_frame(frame_type, payload=b""):
    """Assemble a motor->display frame with header, length and CRC."""
    body = bytes([START_MOTOR, 0, frame_type]) + bytes(payload)
    length = len(body)                 # bytes [0..length-1] covered by CRC
    body = bytes([START_MOTOR, length, frame_type]) + bytes(payload)
    crc = crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def alive_frame():
    return build_frame(FRAME_ALIVE)


def firmware_frame():
    # payload: [error_states, major, minor, patch]
    return build_frame(FRAME_FIRMWARE_VERSION,
                       bytes([0, FW_MAJOR, FW_MINOR, FW_PATCH]))


def status_frame(status=MOTOR_INIT_STATUS_INIT_OK):
    # payload: [motor_init_status]
    return build_frame(FRAME_STATUS, bytes([status]))


def periodic_frame(state):
    """Faked telemetry. payload indices match p_rx_buffer[3..26] in state.c."""
    p = bytearray(24)   # indices 0..23 here == rx buffer [3..26]

    adc_v = volts_to_adc(state["voltage"])
    p[0] = adc_v & 0xFF                       # [3] battery voltage low 8 bits
    p[1] = ((adc_v >> 8) & 0x03) << 4         # [4] voltage high bits at 0x30
    p[2] = state["battery_current_x5"]        # [5] battery current x5

    wheel = state["wheel_speed_x10"] & 0x3FF
    p[3] = wheel & 0xFF                        # [6] wheel speed low
    p[4] = (wheel >> 8) & 0xFF                 # [7] wheel speed high (+torque hi bits)

    p[5] = 0x00                               # [8] flags: braking/hall/limits
    p[6] = state["throttle"]                  # [9] adc throttle
    p[7] = state["motor_temp"]                # [10] motor temp OR throttle map

    p[8] = state["torque_adc"] & 0xFF         # [11] pedal torque sensor low
    p[9] = state["torque_delta"] & 0xFF       # [12] torque delta low
    p[10] = (state["torque_delta"] >> 8) & 0xFF  # [13] torque delta high

    p[11] = state["cadence"]                  # [14] pedal cadence
    p[12] = state["duty_cycle"]               # [15] motor duty cycle

    erps = state["motor_erps"]
    p[13] = erps & 0xFF                        # [16] motor speed erps low
    p[14] = (erps >> 8) & 0xFF                 # [17] motor speed erps high

    p[15] = 0x00                              # [18] foc angle / field weakening
    p[16] = 0x00                              # [19] error states (0 = no error)
    p[17] = state["motor_current_x5"]         # [20] motor current x5

    ticks = state["wheel_ticks"] & 0xFFFFFF
    p[18] = ticks & 0xFF                       # [21] wheel speed tick counter
    p[19] = (ticks >> 8) & 0xFF                # [22]
    p[20] = (ticks >> 16) & 0xFF              # [23]

    p[21] = 0x00                              # [24] torque delta boost low
    p[22] = 0x00                              # [25] torque delta boost high
    p[23] = 0x00                              # [26] torque increment (patch>=52)

    return build_frame(FRAME_PERIODIC, bytes(p))


class FrameParser:
    """Incremental parser for display->motor (0x59) frames."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        """Append bytes, yield (frame_type, full_frame_bytes) for each frame."""
        self.buf.extend(data)
        out = []
        while True:
            # drop noise until a start byte
            start = self.buf.find(START_DISP)
            if start < 0:
                self.buf.clear()
                break
            if start > 0:
                del self.buf[:start]
            if len(self.buf) < 2:
                break
            length = self.buf[1]
            total = length + 2                 # payload + 2 CRC bytes
            if len(self.buf) < total:
                break
            frame = bytes(self.buf[:total])
            del self.buf[:total]
            expect = crc16(frame[:length])
            got = frame[length] | (frame[length + 1] << 8)
            if expect != got:
                # bad CRC: resync past this start byte and retry
                continue
            out.append((frame[2], frame))
        return out


def main():
    print(f"[motor-emu] opening {PORT} @ {BAUD} 8N1")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0)
    except serial.SerialException as e:
        print(f"[motor-emu] ERROR opening {PORT}: {e}")
        print("[motor-emu] check the port name and that you're in the "
              "'dialout' group (ls -l /dev/ttyACM*; groups).")
        sys.exit(1)

    # Mutable faked telemetry. Tweak these to drive the display's gauges.
    state = {
        "voltage": 40.0,            # battery volts
        "battery_current_x5": 25,   # 5.0 A
        "wheel_speed_x10": 255,     # 25.5 km/h
        "throttle": 0,
        "motor_temp": 30,
        "torque_adc": 160,
        "torque_delta": 0,
        "cadence": 70,              # rpm
        "duty_cycle": 120,
        "motor_erps": 100,
        "motor_current_x5": 30,     # 6.0 A
        "wheel_ticks": 5000,
    }

    parser = FrameParser()
    handshaking = True              # send unprompted ALIVE until display replies
    last_alive = 0.0
    last_rx = 0.0
    # If the display goes quiet this long, assume it rebooted (back to
    # WAIT_MOTOR_ALIVE, where it sends nothing) and resume unprompted ALIVE.
    SILENCE_REARM = 0.5
    print("[motor-emu] sending ALIVE until display responds. Ctrl-C to quit.")

    try:
        while True:
            now = time.time()

            # Re-arm the handshake if the display stopped streaming (reboot).
            if (not handshaking) and last_rx and (now - last_rx) > SILENCE_REARM:
                handshaking = True
                print("[motor-emu] display went silent -> resume ALIVE "
                      "(reboot?)")

            # 1) unprompted ALIVE during the wait-for-motor phase
            if handshaking and (now - last_alive) > 0.05:
                ser.write(alive_frame())
                last_alive = now

            # 2) react to whatever the display sends
            data = ser.read(256)
            if data:
                last_rx = now
                for ftype, frame in parser.feed(data):
                    if handshaking:
                        handshaking = False
                        print("[motor-emu] display is talking -> stop "
                              "unprompted ALIVE")
                    name = FRAME_NAME.get(ftype, f"0x{ftype:02x}")
                    if ftype == FRAME_FIRMWARE_VERSION:
                        ser.write(firmware_frame())
                        print(f"[motor-emu] rx {name} -> tx FIRMWARE "
                              f"{FW_MAJOR}.{FW_MINOR}.{FW_PATCH}")
                    elif ftype == FRAME_STATUS:
                        ser.write(status_frame())
                        print(f"[motor-emu] rx {name} -> tx STATUS=INIT_OK")
                    elif ftype == FRAME_CONFIGURATIONS:
                        # display ignores non-STATUS here; just acknowledge log
                        print(f"[motor-emu] rx {name} ({len(frame)} bytes), "
                              "no reply needed")
                    elif ftype == FRAME_PERIODIC:
                        ser.write(periodic_frame(state))
                        # READY: this is the steady-state stream
                    else:
                        print(f"[motor-emu] rx {name} (unhandled)")

            time.sleep(0.005)
    except KeyboardInterrupt:
        print("\n[motor-emu] bye")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
