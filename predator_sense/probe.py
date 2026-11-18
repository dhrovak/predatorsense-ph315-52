"""Hardware check that must pass before fan control is unlocked."""

import json
import os
import statistics
import time

from . import hardware as hw
from .daemon import STATE_DIR, VERIFIED_FILE, verification_problem
from .ec import EC, ECError

SPIN_UP_SECONDS = 8
MIN_RPM_INCREASE = 500
MAX_RPM = 8000
# The firmware CPU temperature is smoothed: ~10°C above coretemp at idle, below it during spikes.
TEMP_TOLERANCE = 15
TEMP_SAMPLES = 6
TEMP_SAMPLE_INTERVAL = 0.5


def _ok(msg):
    print(f"  [ OK ] {msg}")


def _fail(msg):
    print(f"  [FAIL] {msg}")


def check_readings(sensors, firmware_cpu_temp, coretemp):
    """Return a list of problems with the sensor readings (empty if they look right)."""
    problems = []
    for fan in hw.FANS:
        rpm = sensors[fan + "_rpm"]
        if rpm is None:
            problems.append(f"{fan.upper()} fan speed cannot be read")
        elif not 0 <= rpm <= MAX_RPM:  # 0 is fine: fans may stop when cool
            problems.append(f"{fan.upper()} fan reads {rpm} rpm")
    if (coretemp is not None and firmware_cpu_temp is not None
            and abs(firmware_cpu_temp - coretemp) > TEMP_TOLERANCE):
        problems.append(f"Firmware CPU temperature {firmware_cpu_temp}°C differs from coretemp {coretemp}°C")
    return problems


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median_low(values) if values else None


def sample_cpu_temps(controller, samples=TEMP_SAMPLES, interval=TEMP_SAMPLE_INTERVAL):
    """Median firmware and coretemp CPU temperatures, so a momentary spike cannot fail the check."""
    firmware, coretemp = [], []
    for i in range(samples):
        if i:
            time.sleep(interval)
        firmware.append(controller.firmware_cpu_temp())
        coretemp.append(hw.read_cpu_temp_hwmon(controller.hwmon_root))
    return _median(firmware), _median(coretemp)


def run(if_needed=False):
    if os.geteuid() != 0:
        print("Run this as root: sudo predator-sense verify")
        return 1
    model = hw.detect_model()
    if not hw.is_supported_model(model):
        print(f"This tool only supports {', '.join(hw.SUPPORTED_MODELS)} (found: {model or 'unknown'}).")
        return 1
    if if_needed and verification_problem() is None:
        print("The hardware check already passed for this BIOS and facer module.")
        return 0

    print("PredatorSense hardware check\n")
    try:
        ec = EC()
    except ECError as e:
        print(f"  [INFO] {e} Using firmware sensors only.")
        ec = None
    controller = hw.Controller(ec)
    try:
        status = controller.status()
        firmware_cpu_temp, coretemp = sample_cpu_temps(controller)
    except OSError as e:
        _fail(f"Cannot read the sensors: {e}")
        return 1
    problems = check_readings(status, firmware_cpu_temp, coretemp)
    for p in problems:
        _fail(p)
    if problems:
        print("\nThe sensor readings do not match this laptop. Fan control stays locked.")
        return 1
    source = "firmware" if status["sensor_source"] == "firmware" else "EC registers"
    gpu_temp = "unavailable" if status["gpu_temp"] is None else f"{status['gpu_temp']}°C"
    _ok(f"Sensors read correctly from the {source}: CPU {status['cpu_temp']}°C, GPU {gpu_temp}, "
        f"CPU fan {status['cpu_rpm']} rpm, GPU fan {status['gpu_rpm']} rpm")

    if not controller.can_control_fans:
        print("\nThe PredatorSense build of the facer module is not loaded, so fan control stays off.")
        print("Re-run: sudo ./install.sh")
        return 1

    if status["cpu_fan"]["mode"] != "auto" or status["gpu_fan"]["mode"] != "auto":
        _fail(f"Fans are not in Auto mode ({status['cpu_fan']['mode']}, {status['gpu_fan']['mode']}).")
        return 1
    _ok("Both fans are in Auto mode")

    print("\nPredatorSense will briefly spin both fans to 100% (they will be loud).")
    try:
        answer = input("  Continue? [Y/n] ")
    except EOFError:
        answer = "n"
    if answer.strip().lower() not in ("", "y", "yes"):
        print("Cancelled. Fan control stays locked.")
        return 1

    try:
        before = controller.status()
        controller.set_both(100, 100)
        print(f"  Waiting {SPIN_UP_SECONDS} seconds for the fans to spin up...")
        time.sleep(SPIN_UP_SECONDS)
        after = controller.status()
    except (ECError, OSError) as e:
        _fail(f"Setting the fans failed: {e}")
        return 1
    finally:
        reset_errors = controller.reset()
    if reset_errors:
        _fail("Could not return fans to Auto: " + "; ".join(reset_errors) + ". Reboot to be safe.")
        return 1

    results = {}
    for fan in hw.FANS:
        b, a = before[fan + "_rpm"], after[fan + "_rpm"]
        responded = a is not None and b is not None and a - b >= MIN_RPM_INCREASE
        results[fan] = {"rpm_before": b, "rpm_at_100": a, "responded": responded}
        if responded:
            _ok(f"{fan.upper()} fan responded: {b} -> {a} rpm")
        else:
            _fail(f"{fan.upper()} fan did not speed up ({b} -> {a} rpm)")
    if not all(r["responded"] for r in results.values()):
        print("\nManual fan control did not work. Fan control stays locked.")
        return 1
    _ok("Fans returned to Auto")

    os.makedirs(STATE_DIR, exist_ok=True)
    record = dict(hw.fingerprint(), time=int(time.time()), sensor_source=status["sensor_source"],
                  results=results)
    tmp = VERIFIED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, VERIFIED_FILE)
    print("\nAll checks passed. Fan control is now enabled.")
    return 0
