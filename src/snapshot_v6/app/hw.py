"""Hardware access: shared I2C bus (PCA9685 0x40, SSD1306 0x3C) and GPIO (buttons, LEDs).

PCA9685 rules (技术方案.md 4.3): init MODE2=0x04, MODE1=0x30, PRESCALE=121, wait 5 ms,
MODE1=0x20, wait 5 ms, MODE1=0xA0; afterwards only the 4-byte LEDn registers of CH4/CH8 are
written. The chip is never put back to sleep. ALL_LED registers are never used.
"""
from __future__ import annotations

import threading
import time

import config as C
import pointing

try:
    import smbus2
except ImportError:  # allows importing on a dev PC
    smbus2 = None
try:
    import lgpio
except ImportError:
    lgpio = None

i2c_lock = threading.Lock()


def scan_i2c(bus_no=C.I2C_BUS) -> list:
    found = []
    with i2c_lock:
        b = smbus2.SMBus(bus_no)
        try:
            for addr in range(0x03, 0x78):
                try:
                    b.write_quick(addr)
                    found.append(addr)
                except OSError:
                    pass
        finally:
            b.close()
    return found


class Pca9685:
    MODE1, MODE2, PRESCALE, LED0 = 0x00, 0x01, 0xFE, 0x06
    ALLOWED = (C.PAN_CH, C.TILT_CH)

    def __init__(self, bus, addr=C.PCA9685_ADDR):
        self.bus, self.addr = bus, addr
        self.ticks = {}
        self.us = {}
        self.inits = 0
        self.init_running()

    def init_running(self):
        with i2c_lock:
            self.bus.write_byte_data(self.addr, self.MODE2, 0x04)
            self.bus.write_byte_data(self.addr, self.MODE1, 0x30)
            self.bus.write_byte_data(self.addr, self.PRESCALE, C.PCA_PRESCALE)
            time.sleep(0.005)
            self.bus.write_byte_data(self.addr, self.MODE1, 0x20)
            time.sleep(0.005)
            self.bus.write_byte_data(self.addr, self.MODE1, 0xA0)
            time.sleep(0.005)
        self.inits += 1
        self.ticks.clear()          # force rewrite of the channels after (re)init

    def check_awake(self) -> bool:
        """Re-init only if the chip reports SLEEP (e.g. lost logic power). Returns True if re-inited."""
        with i2c_lock:
            mode1 = self.bus.read_byte_data(self.addr, self.MODE1)
        if mode1 & 0x10:
            self.init_running()
            return True
        return False

    def write_us(self, channel: int, micros: float) -> int:
        """Write one channel; returns the integer us actually represented by the 12-bit count."""
        if channel not in self.ALLOWED:
            raise ValueError(f"only CH{self.ALLOWED} may be written")
        micros = pointing.clamp(micros, *pointing.channel_limits_us(channel))
        ticks = max(1, min(4095, pointing.pulse_to_ticks(micros)))
        if self.ticks.get(channel) == ticks:
            return self.us[channel]
        reg = self.LED0 + 4 * channel
        with i2c_lock:
            self.bus.write_i2c_block_data(self.addr, reg, [0, 0, ticks & 0xFF, (ticks >> 8) & 0x0F])
        self.ticks[channel] = ticks
        self.us[channel] = int(round(pointing.ticks_to_us(ticks)))
        return self.us[channel]


    def readback(self, channel: int) -> dict:
        """Read the LEDn ON/OFF registers back from the chip (checks the write really landed)."""
        reg = self.LED0 + 4 * channel
        with i2c_lock:
            d = self.bus.read_i2c_block_data(self.addr, reg, 4)
            mode1 = self.bus.read_byte_data(self.addr, self.MODE1)
        off = d[2] | ((d[3] & 0x0F) << 8)
        return {"on": d[0] | ((d[1] & 0x0F) << 8), "off": off, "us": round(pointing.ticks_to_us(off), 1),
                "mode1": f"0x{mode1:02x}"}


class Ssd1306:
    """128x32 SSD1306 (same init as the bench page, verified on the real panel)."""
    WIDTH, HEIGHT = 128, 32

    def __init__(self, bus, addr=C.OLED_ADDR):
        self.bus, self.addr = bus, addr
        for cmd in ((0xAE,), (0xD5, 0x80), (0xA8, 0x1F), (0xD3, 0x00), (0x40,), (0x8D, 0x14),
                    (0x20, 0x00), (0xA1,), (0xC8,), (0xDA, 0x02), (0x81, 0x8F), (0xD9, 0xF1),
                    (0xDB, 0x40), (0xA4,), (0xA6,), (0x2E,), (0xAF,)):
            self._cmd(*cmd)
        self.last = None

    def _cmd(self, *values):
        with i2c_lock:
            self.bus.write_i2c_block_data(self.addr, 0x00, list(values))

    def show(self, lines):
        lines = ["".join(ch if 32 <= ord(ch) < 127 else "?" for ch in str(l))[:21] for l in lines[:2]]
        if lines == self.last:
            return
        from PIL import Image, ImageDraw
        img = Image.new("1", (self.WIDTH, self.HEIGHT), 0)
        d = ImageDraw.Draw(img)
        for i, line in enumerate(lines):
            d.text((0, i * 16), line, fill=1)
        px = img.load()
        buf = bytearray(self.WIDTH * self.HEIGHT // 8)
        idx = 0
        for page in range(self.HEIGHT // 8):
            for x in range(self.WIDTH):
                b = 0
                for bit in range(8):
                    if px[x, page * 8 + bit]:
                        b |= 1 << bit
                buf[idx] = b
                idx += 1
        self._cmd(0x21, 0x00, self.WIDTH - 1)
        self._cmd(0x22, 0x00, self.HEIGHT // 8 - 1)
        for off in range(0, len(buf), 16):
            with i2c_lock:
                self.bus.write_i2c_block_data(self.addr, 0x40, list(buf[off:off + 16]))
        self.last = lines

    def off(self):
        self._cmd(0xAE)


class Gpio:
    """Buttons (pull-up, active low, 30 ms debounce) and LEDs (active high), 技术方案.md 4.2."""
    BUTTONS = {"K1": C.BTN_ARM, "K2": C.BTN_PAGE, "K3": C.BTN_CENTER, "K4": C.BTN_SNAPSHOT}
    LEDS = {"green": C.LED_GREEN, "red": C.LED_RED, "yellow": C.LED_YELLOW}

    def __init__(self, on_press):
        self.on_press = on_press
        self.chip = lgpio.gpiochip_open(0)
        for g in self.BUTTONS.values():
            lgpio.gpio_claim_input(self.chip, g, lgpio.SET_PULL_UP)
        for g in self.LEDS.values():
            lgpio.gpio_claim_output(self.chip, g, 0)
        self.led_state = {k: False for k in self.LEDS}
        self.pressed = {k: False for k in self.BUTTONS}
        self._raw_since = {k: (False, time.monotonic()) for k in self.BUTTONS}
        self.running = True
        self.lock = threading.Lock()
        threading.Thread(target=self._poll, daemon=True, name="buttons").start()

    def _poll(self):
        while self.running:
            now = time.monotonic()
            for name, g in self.BUTTONS.items():
                try:
                    with self.lock:
                        raw = lgpio.gpio_read(self.chip, g) == 0
                except Exception:
                    continue
                last_raw, since = self._raw_since[name]
                if raw != last_raw:
                    self._raw_since[name] = (raw, now)
                elif now - since >= C.BTN_DEBOUNCE_S and self.pressed[name] != raw:
                    self.pressed[name] = raw
                    if raw:
                        try:
                            self.on_press(name)
                        except Exception as exc:
                            print("button handler error:", exc)
            time.sleep(0.01)

    def led(self, name, on):
        on = bool(on)
        if self.led_state.get(name) == on:
            return
        with self.lock:
            lgpio.gpio_write(self.chip, self.LEDS[name], 1 if on else 0)
        self.led_state[name] = on

    def close(self):
        self.running = False
        time.sleep(0.03)
        for n in self.LEDS:
            try:
                self.led(n, False)
            except Exception:
                pass
        try:
            lgpio.gpiochip_close(self.chip)
        except Exception:
            pass
