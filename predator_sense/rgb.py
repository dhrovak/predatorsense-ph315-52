"""4-zone keyboard RGB through the facer kernel module's character devices."""

import os
import re

DYNAMIC_DEVICE = "/dev/acer-gkbbl-0"
STATIC_DEVICE = "/dev/acer-gkbbl-static-0"
TIMEOUT_PARAM = "/sys/module/facer/parameters/kb_timeout"

EFFECTS = {"static": 0, "breath": 1, "neon": 2, "wave": 3, "shifting": 4, "zoom": 5}
EFFECTS_WITH_COLOR = ("breath", "shifting", "zoom")
EFFECTS_WITH_DIRECTION = ("wave", "shifting")
DIRECTIONS = {"right-to-left": 1, "left-to-right": 2}
ZONES = 4

DEFAULT_SETTINGS = {
    "effect": "wave",
    "brightness": 100,
    "speed": 4,
    "direction": "right-to-left",
    "color": "#ff0000",
    "zones": ["#ff0000", "#ff0000", "#ff0000", "#ff0000"],
}

_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value):
    match = _HEX_COLOR.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ValueError(f"Invalid color {value!r}; use #RRGGBB")
    return "#" + match.group(1).lower()


def color_bytes(color):
    h = parse_color(color)[1:]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _int_in_range(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def normalize_settings(settings, base=None):
    """Merge `settings` over `base` (or defaults) and validate every field."""
    if not isinstance(settings, dict):
        raise ValueError("Keyboard settings must be an object")
    unknown = set(settings) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError(f"Unknown keyboard setting(s): {', '.join(sorted(unknown))}")
    merged = dict(base or DEFAULT_SETTINGS)
    merged.update(settings)
    if not isinstance(merged["effect"], str) or merged["effect"] not in EFFECTS:
        raise ValueError(f"Effect must be one of: {', '.join(EFFECTS)}")
    if not isinstance(merged["direction"], str) or merged["direction"] not in DIRECTIONS:
        raise ValueError(f"Direction must be one of: {', '.join(DIRECTIONS)}")
    zones = merged["zones"]
    if not isinstance(zones, list) or len(zones) != ZONES:
        raise ValueError(f"zones must be a list of {ZONES} colors")
    return {
        "effect": merged["effect"],
        "brightness": _int_in_range(merged["brightness"], "brightness", 0, 100),
        "speed": _int_in_range(merged["speed"], "speed", 0, 9),
        "direction": merged["direction"],
        "color": parse_color(merged["color"]),
        "zones": [parse_color(c) for c in zones],
    }


def effect_payload(settings):
    """16-byte payload for /dev/acer-gkbbl-0, same layout as facer_rgb.py."""
    payload = bytearray(16)
    effect = settings["effect"]
    if effect == "static":
        payload[2] = settings["brightness"]
        payload[9] = 1
        return bytes(payload)
    payload[0] = EFFECTS[effect]
    payload[1] = settings["speed"]
    payload[2] = settings["brightness"]
    payload[3] = 8 if effect == "wave" else 0
    payload[4] = DIRECTIONS[settings["direction"]]
    if effect in EFFECTS_WITH_COLOR:
        payload[5:8] = bytes(color_bytes(settings["color"]))
    payload[9] = 1
    return bytes(payload)


def zone_payload(zone_index, color):
    """4-byte payload for /dev/acer-gkbbl-static-0; zone_index is 0-3, left to right."""
    if not 0 <= zone_index < ZONES:
        raise ValueError("zone index out of range")
    return bytes([1 << zone_index, *color_bytes(color)])


class Keyboard:
    def __init__(self, dynamic_device=DYNAMIC_DEVICE, static_device=STATIC_DEVICE, timeout_param=TIMEOUT_PARAM):
        self.dynamic_device = dynamic_device
        self.static_device = static_device
        self.timeout_param = timeout_param

    def available(self):
        return os.path.exists(self.dynamic_device) and os.path.exists(self.static_device)

    def timeout_available(self):
        return os.path.exists(self.timeout_param)

    def set_timeout(self, on):
        """Turn the backlight's 30 second idle timeout on or off."""
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        if not self.timeout_available():
            raise OSError("The keyboard backlight timeout needs the PredatorSense build of the facer module. "
                          "Re-run: sudo ./install.sh")
        fd = os.open(self.timeout_param, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, b"1\n" if on else b"0\n")
        except OSError as e:
            raise OSError(f"The firmware rejected the keyboard timeout setting ({e.strerror})") from e
        finally:
            os.close(fd)

    @staticmethod
    def _write(path, payload):
        fd = os.open(path, os.O_WRONLY)
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError(f"Short write to {path}")
        finally:
            os.close(fd)

    def apply(self, settings):
        if not self.available():
            raise OSError("Keyboard RGB device not found (is the facer module loaded?)")
        if settings["effect"] == "static":
            for i, color in enumerate(settings["zones"]):
                self._write(self.static_device, zone_payload(i, color))
        self._write(self.dynamic_device, effect_payload(settings))
