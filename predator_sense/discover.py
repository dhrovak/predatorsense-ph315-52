"""Read-only EC recorder used to find the real register map of this laptop.

The fans are driven by a short CPU load instead of the Turbo key, so this works
even when the firmware sends no Turbo events.
"""

import datetime
import multiprocessing
import os
import platform
import time

from . import hardware as hw
from .ec import EC, ECError

REPORT_NAME = "ec-report.txt"
IDLE_SECONDS = 10
LOAD_SECONDS = 45
COOL_SECONDS = 20
# Registers suggested by other projects for this laptop family (NBFC-Linux and Linux-PredatorSense).
CANDIDATE_BYTES = (0x21, 0x22, 0x37, 0x3A, 0x5B, 0xB0, 0xB3, 0xB7, 0xF5)
CANDIDATE_WORDS = (0x13, 0x15, 0x5C, 0x70, 0xDB, 0xDD)


def hexdump(data):
    lines = ["     " + " ".join(f"{i:02x}" for i in range(16))]
    for row in range(0, len(data), 16):
        lines.append(f"{row:02x}:  " + " ".join(f"{b:02x}" for b in data[row:row + 16]))
    return "\n".join(lines)


def word(data, addr):
    return data[addr] | (data[addr + 1] << 8)


def volatile_registers(samples):
    return [i for i in range(len(samples[0])) if len({s[i] for s in samples}) > 1]


def stable_changes(before, after):
    """Registers constant within each phase but different between them (mode/flag candidates)."""
    changes = []
    for i in range(len(before[0])):
        a, b = {s[i] for s in before}, {s[i] for s in after}
        if len(a) == 1 and len(b) == 1 and a != b:
            changes.append((i, a.pop(), b.pop()))
    return changes


def temperature_candidates(samples, temps, tolerance=4):
    """Byte registers that track the coretemp CPU temperature."""
    hits = []
    for i in range(len(samples[0])):
        if all(t is not None and abs(s[i] - t) <= tolerance for s, t in zip(samples, temps)):
            hits.append(i)
    return hits


def rpm_candidates(idle, loaded, min_rise=800, max_rpm=8000):
    """16-bit little-endian words that rise a lot when the fans spin up."""
    hits = []
    for i in range(len(idle[0]) - 1):
        before = max(word(s, i) for s in idle)
        after = max(word(s, i) for s in loaded)
        if after <= max_rpm and after - before >= min_rise:
            hits.append((i, before, after))
    return hits


def _env_info(ec_path):
    def read(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError as e:
            return f"<{e.strerror}>"

    modules = read("/proc/modules")
    loaded = [m for m in ("facer", "acer_wmi", "linuwu_sense", "acpi_ec") if f"\n{m} " in "\n" + modules]
    ec_dirs = sorted(os.listdir("/sys/kernel/debug/ec")) if os.path.isdir("/sys/kernel/debug/ec") else []
    return [
        f"date: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"model: {hw.detect_model()}",
        f"bios: {read('/sys/class/dmi/id/bios_version')} ({read('/sys/class/dmi/id/bios_date')})",
        f"kernel: {platform.release()}",
        f"ec path: {ec_path}   ec dirs: {ec_dirs}",
        f"ec_sys write_support: {read('/sys/module/ec_sys/parameters/write_support')}",
        f"modules loaded: {', '.join(loaded) or 'none of facer/acer_wmi/linuwu_sense/acpi_ec'}",
    ]


def _record(ec, seconds, label, out):
    samples, temps = [], []
    for n in range(seconds):
        if n:
            time.sleep(1)
        data = ec.read_all()
        temp = hw.read_cpu_temp_hwmon()
        samples.append(data)
        temps.append(temp)
        out.append(f"\n=== {label} {n + 1}/{seconds}s  (coretemp CPU {temp}°C) ===")
        out.append(hexdump(data))
        print(f"  {label}: {n + 1}/{seconds}s  CPU {temp}°C   ", end="\r", flush=True)
    print()
    return samples, temps


def _burn(stop_at):
    while time.monotonic() < stop_at:
        pass


def _write_report(path, lines):
    # Runs as root in a user-chosen directory, so never follow a planted symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if uid and gid and uid.isdigit() and gid.isdigit():
            os.fchown(f.fileno(), int(uid), int(gid))
        f.write("\n".join(lines) + "\n")


def run():
    if os.geteuid() != 0:
        print("Run this as root: sudo python3 -m predator_sense discover")
        return 1
    try:
        ec = EC()
        ec.read_all()
    except (ECError, OSError) as e:
        print(f"Cannot read the embedded controller: {e}")
        return 1

    report_path = os.path.join(os.getcwd(), REPORT_NAME)
    out = _env_info(ec.path)
    total = IDLE_SECONDS + LOAD_SECONDS + COOL_SECONDS
    print("PredatorSense register discovery (read-only, nothing is written to the EC)")
    print(f"This takes about {total} seconds and puts the CPU under full load for {LOAD_SECONDS} seconds")
    print("so the fans speed up. Keep the laptop plugged in and leave it alone until it finishes.\n")
    try:
        input("Press Enter to start... ")
    except EOFError:
        print("\nNo input available; run this from a terminal.")
        return 1

    idle, idle_temps = _record(ec, IDLE_SECONDS, "idle", out)
    stop_at = time.monotonic() + LOAD_SECONDS
    workers = [multiprocessing.Process(target=_burn, args=(stop_at,), daemon=True)
               for _ in range(os.cpu_count() or 4)]
    for w in workers:
        w.start()
    try:
        loaded, loaded_temps = _record(ec, LOAD_SECONDS, "load", out)
    finally:
        for w in workers:
            w.terminate()
            w.join()
    cool, cool_temps = _record(ec, COOL_SECONDS, "cool down", out)

    out.append("\n=== analysis ===")
    out.append(f"coretemp: idle {idle_temps[-1]}°C, peak {max(t or 0 for t in loaded_temps)}°C, after {cool_temps[-1]}°C")
    out.append("registers changing while idle: " + " ".join(f"{i:02x}" for i in volatile_registers(idle)))
    out.append("stable idle -> load changes: " + ", ".join(
        f"{i:02x}:{a:02x}->{b:02x}" for i, a, b in stable_changes(idle[-3:], loaded[-3:])))
    all_samples = idle + loaded + cool
    all_temps = idle_temps + loaded_temps + cool_temps
    out.append("CPU temp candidates: " + " ".join(f"{i:02x}" for i in temperature_candidates(all_samples, all_temps)))
    out.append("RPM candidates (LE word, idle max -> load max): "
               + ", ".join(f"{i:02x}:{a}->{b}" for i, a, b in rpm_candidates(idle, loaded)))
    for label, s in (("idle", idle[-1]), ("load", loaded[-1]), ("cool", cool[-1])):
        out.append(f"{label} known bytes: " + " ".join(f"{a:02x}={s[a]:02x}" for a in CANDIDATE_BYTES)
                   + " | words: " + " ".join(f"{a:02x}={word(s, a)}" for a in CANDIDATE_WORDS))

    _write_report(report_path, out)
    print("\n" + "\n".join(out[out.index("\n=== analysis ==="):]))
    print(f"\nFull report saved to {report_path}")
    return 0
