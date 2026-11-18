"""PH315-52 sensors and fan control.

Sensors come from the firmware's gaming WMI interface when it answers, with the
EC registers and hwmon as fallback. Fans are controlled through the firmware's
own gaming WMI methods (the ones PredatorSense uses on Windows), exposed by the
patched facer module; nothing is ever written to the EC.
"""

import glob
import os

SUPPORTED_MODELS = ("Predator PH315-52",)
DMI_PRODUCT_NAME = "/sys/class/dmi/id/product_name"
DMI_BIOS_VERSION = "/sys/class/dmi/id/bios_version"
AC_ONLINE = "/sys/class/power_supply/AC/online"
FAN_CONTROL_PARAM = "/sys/module/facer/parameters/fan_control"
SENSORS_PARAM = "/sys/module/facer/parameters/sensors"
FACER_VERSION = "/sys/module/facer/version"

# Measured on PH315-52 BIOS V1.12 with `predator-sense discover` (CPU load test).
CPU_RPM = 0x70  # little-endian word
GPU_RPM = 0x5C  # little-endian word
CPU_TEMP = 0xDB

MAX_TEMP = 120
MAX_RPM = 10000

FANS = ("cpu", "gpu")


def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def detect_model():
    return _read_text(DMI_PRODUCT_NAME) or ""


def fingerprint():
    """What the hardware check depends on; if any of it changes the check must be redone."""
    return {"model": detect_model(), "bios": _read_text(DMI_BIOS_VERSION), "facer": _read_text(FACER_VERSION)}


def is_supported_model(model=None):
    return (model if model is not None else detect_model()) in SUPPORTED_MODELS


def fan_info(setting):
    if isinstance(setting, int):
        return {"mode": "manual", "speed": setting}
    return {"mode": setting, "speed": None}


def read_word(data, addr):
    return data[addr] | (data[addr + 1] << 8)


def parse_fan_setting(value):
    """Return 'auto', 'max' or an int percentage 0-100; raise ValueError otherwise."""
    if isinstance(value, bool):
        raise ValueError("Invalid fan setting")
    if isinstance(value, str):
        text = value.strip().lower().rstrip("%")
        if text in ("auto", "max"):
            return text
        if not text.isdigit():
            raise ValueError(f"Invalid fan setting: {value!r} (use auto, max or 0-100)")
        value = int(text)
    if not isinstance(value, int):
        raise ValueError(f"Invalid fan setting: {value!r}")
    if not 0 <= value <= 100:
        raise ValueError("Fan speed must be between 0 and 100")
    return value


def read_hwmon_temp(name, hwmon_root="/sys/class/hwmon"):
    """temp1 of the first hwmon device called `name`, in °C, or None."""
    for name_file in sorted(glob.glob(os.path.join(hwmon_root, "hwmon*", "name"))):
        try:
            with open(name_file) as f:
                if f.read().strip() != name:
                    continue
            with open(os.path.join(os.path.dirname(name_file), "temp1_input")) as f:
                return round(int(f.read().strip()) / 1000)
        except (OSError, ValueError):
            continue
    return None


def read_cpu_temp_hwmon(hwmon_root="/sys/class/hwmon"):
    return read_hwmon_temp("coretemp", hwmon_root)


def read_on_ac(path=AC_ONLINE):
    try:
        with open(path) as f:
            return f.read().strip() == "1"
    except OSError:
        return None


class WmiSensors:
    """Firmware sensor readings from the patched facer module's sensors parameter."""

    KEYS = ("cpu_temp", "gpu_temp", "cpu_rpm", "gpu_rpm")

    def __init__(self, path=SENSORS_PARAM):
        self.path = path

    def read(self):
        """Plausible readings by key; None for anything missing, unsupported or out of range."""
        values = dict.fromkeys(self.KEYS)
        text = _read_text(self.path)
        if text is None:
            return values
        for item in text.split():
            key, _, raw = item.partition("=")
            if key not in values or not raw.isdigit():
                continue
            value = int(raw)
            low, high = (1, MAX_TEMP) if key.endswith("_temp") else (0, MAX_RPM)
            if low <= value <= high:
                values[key] = value
        return values


class WmiFans:
    """Fan control through the patched facer module's fan_control parameter."""

    def __init__(self, path=FAN_CONTROL_PARAM):
        self.path = path

    def available(self):
        return os.path.exists(self.path)

    def get(self):
        with open(self.path) as f:
            parts = f.read().split()
        if len(parts) != 2:
            raise OSError(f"Unexpected fan_control value: {' '.join(parts)!r}")
        try:
            return dict(zip(FANS, (parse_fan_setting(p) for p in parts)))
        except ValueError as e:
            raise OSError(f"Unexpected fan_control value: {e}") from e

    def set(self, cpu, gpu):
        text = f"{parse_fan_setting(cpu)} {parse_fan_setting(gpu)}\n".encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, text)
        except OSError as e:
            raise OSError(f"The firmware rejected the fan setting ({e.strerror})") from e
        finally:
            os.close(fd)


class Controller:
    def __init__(self, ec, fans=None, sensors=None, hwmon_root="/sys/class/hwmon", ac_path=AC_ONLINE):
        self.ec = ec
        self.fans = fans if fans is not None else WmiFans()
        self.sensors = sensors if sensors is not None else WmiSensors()
        self.hwmon_root = hwmon_root
        self.ac_path = ac_path

    @property
    def can_control_fans(self):
        return self.fans.available()

    def _ec_byte(self, addr):
        return None if self.ec is None else self.ec.read(addr)

    def _ec_word(self, addr):
        if self.ec is None:
            return None
        high = self.ec.read(addr + 1)
        low = self.ec.read(addr)
        # The speed can change between the two byte reads; re-read the low byte if the high one moved.
        high_again = self.ec.read(addr + 1)
        if high_again != high:
            low = self.ec.read(addr)
        return low | (high_again << 8)

    def gpu_temp(self, firmware=None):
        firmware = firmware if firmware is not None else self.sensors.read()
        if firmware["gpu_temp"] is not None:
            return firmware["gpu_temp"]
        return read_hwmon_temp("nouveau", self.hwmon_root)

    def firmware_cpu_temp(self, firmware=None):
        """CPU temperature as the firmware sees it (WMI, else the EC register)."""
        firmware = firmware if firmware is not None else self.sensors.read()
        if firmware["cpu_temp"] is not None:
            return firmware["cpu_temp"]
        return self._ec_byte(CPU_TEMP)

    def status(self):
        # Only fall back to the EC for what the firmware did not report: each EC byte delays the keyboard.
        firmware = self.sensors.read()
        cpu_temp = read_cpu_temp_hwmon(self.hwmon_root)
        if cpu_temp is None:
            cpu_temp = self.firmware_cpu_temp(firmware)
        rpm = {}
        for fan, addr in (("cpu", CPU_RPM), ("gpu", GPU_RPM)):
            value = firmware[fan + "_rpm"]
            rpm[fan] = value if value is not None else self._ec_word(addr)
        settings = {fan: None for fan in FANS}
        if self.can_control_fans:
            try:
                settings = self.fans.get()
            except OSError:
                pass
        fans = {fan: fan_info(s) if s is not None else {"mode": None, "speed": None}
                for fan, s in settings.items()}
        return {
            "cpu_temp": cpu_temp,
            "gpu_temp": self.gpu_temp(firmware),
            "cpu_rpm": rpm["cpu"],
            "gpu_rpm": rpm["gpu"],
            "sensor_source": "firmware" if any(v is not None for v in firmware.values()) else "ec",
            "cpu_fan": fans["cpu"],
            "gpu_fan": fans["gpu"],
            "turbo": settings["cpu"] == "max" and settings["gpu"] == "max",
            "on_ac": read_on_ac(self.ac_path),
        }

    def _require(self):
        if not self.can_control_fans:
            raise OSError("Fan control needs the PredatorSense build of the facer module. Re-run: sudo ./install.sh")

    def set_fan(self, fan, setting):
        if fan not in FANS:
            raise ValueError(f"Unknown fan: {fan!r}")
        setting = parse_fan_setting(setting)
        self._require()
        current = self.fans.get()
        current[fan] = setting
        self.fans.set(current["cpu"], current["gpu"])

    def set_both(self, cpu, gpu):
        cpu, gpu = parse_fan_setting(cpu), parse_fan_setting(gpu)
        self._require()
        self.fans.set(cpu, gpu)

    def set_turbo(self, on):
        mode = "max" if on else "auto"
        self.set_both(mode, mode)

    def reset(self):
        """Hand fan control back to the firmware; never raises."""
        if not self.can_control_fans:
            return []
        try:
            self.fans.set("auto", "auto")
        except OSError as e:
            return [str(e)]
        return []
