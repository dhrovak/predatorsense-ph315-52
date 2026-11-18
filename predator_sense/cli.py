"""Command line interface. Running with no arguments opens the GUI."""

import argparse
import sys

from . import rgb
from .client import ServiceError, request


def _print_status(data):
    print(f"Model:       {data['model']}")
    s = data.get("sensors")
    if s:
        print(f"CPU:         {_temp_text(s['cpu_temp'])}, fan {_rpm_text(s['cpu_rpm'])} ({_fan_text(s['cpu_fan'])})")
        print(f"GPU:         {_temp_text(s['gpu_temp'])}, fan {_rpm_text(s['gpu_rpm'])} ({_fan_text(s['gpu_fan'])})")
        print(f"Sensors:     {'firmware' if s['sensor_source'] == 'firmware' else 'EC registers'}")
        print(f"Turbo:       {'on' if s['turbo'] else 'off'}")
        print(f"Power:       {'unknown' if s['on_ac'] is None else 'AC' if s['on_ac'] else 'battery'}")
    if data.get("error"):
        print(f"Error:       {data['error']}")
    if data["fan_control"]:
        print("Fan control: enabled")
    else:
        print(f"Fan control: locked. {data['fan_control_problem']}")
    k = data["rgb"]
    print(f"Keyboard:    {k['effect']}, brightness {k['brightness']}%"
          + ("" if data["keyboard"] else " (RGB device not found)"))
    print(f"Backlight:   {'off after 30 s idle' if data['kb_timeout'] else 'always on'}"
          + ("" if data["kb_timeout_control"] else " (cannot be changed: reinstall needed)"))
    if data.get("notice"):
        print(f"Notice:      {data['notice']}")


def _fan_text(fan):
    if fan["mode"] is None:
        return "firmware controlled"
    return f"manual {fan['speed']}%" if fan["mode"] == "manual" else fan["mode"]


def _temp_text(t):
    return "—" if t is None else f"{t}°C"


def _rpm_text(rpm):
    return "— rpm" if rpm is None else f"{rpm} rpm"


def _build_parser():
    p = argparse.ArgumentParser(prog="predator-sense", description="PredatorSense for Acer Predator PH315-52.")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("gui", help="open the window (default)")
    sub.add_parser("status", help="show temperatures, fans and keyboard")

    fan = sub.add_parser("fan", help="set fan speed: auto, max or 0-100")
    fan.add_argument("value", nargs="?", help="setting for both fans")
    fan.add_argument("--cpu", help="setting for the CPU fan")
    fan.add_argument("--gpu", help="setting for the GPU fan")

    turbo = sub.add_parser("turbo", help="turn turbo (both fans at max) on or off")
    turbo.add_argument("state", choices=("on", "off"))

    kb = sub.add_parser("rgb", help="set keyboard lighting")
    kb.add_argument("effect", choices=tuple(rgb.EFFECTS))
    kb.add_argument("--color", help="#RRGGBB for breath/shifting/zoom, or all zones in static")
    kb.add_argument("--zones", nargs=4, metavar="#RRGGBB", help="static colors, left to right")
    kb.add_argument("--brightness", type=int, help="0-100")
    kb.add_argument("--speed", type=int, help="0-9")
    kb.add_argument("--direction", choices=tuple(rgb.DIRECTIONS))

    timeout = sub.add_parser("kb-timeout", help="turn the keyboard backlight off after 30 s idle (on) or keep it lit (off)")
    timeout.add_argument("state", choices=("on", "off"))

    sub.add_parser("verify", help="(root) check the hardware and unlock fan control").add_argument(
        "--if-needed", action="store_true", help="skip if the check already passed for this BIOS and module")
    sub.add_parser("shortcut", help="make the PredatorSense key open this app (GNOME)").add_argument(
        "--remove", action="store_true", help="remove the shortcut instead")
    sub.add_parser("discover", help="(root) record EC registers to find this laptop's register map")
    sub.add_parser("daemon", help="(root) run the background service")
    sub.add_parser("reset", help="(root) return fans to Auto")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    cmd = args.command or "gui"

    if cmd == "gui":
        from .gui import main as gui_main
        return gui_main()
    if cmd == "daemon":
        from .daemon import run
        return run()
    if cmd == "reset":
        from .daemon import reset_fans
        return reset_fans()
    if cmd == "verify":
        from .probe import run
        return run(if_needed=args.if_needed)
    if cmd == "discover":
        from .discover import run
        return run()
    if cmd == "shortcut":
        from .shortcut import run
        return run(remove_shortcut=args.remove)

    try:
        if cmd == "status":
            _print_status(request("status"))
        elif cmd == "fan":
            if args.value is None and args.cpu is None and args.gpu is None:
                print("Give a setting, e.g. 'predator-sense fan auto' or 'predator-sense fan --cpu 60 --gpu 80'")
                return 2
            fans = {fan: getattr(args, fan) if getattr(args, fan) is not None else args.value
                    for fan in ("cpu", "gpu")}
            request("set_fans", **{fan: value for fan, value in fans.items() if value is not None})
        elif cmd == "turbo":
            request("set_turbo", on=args.state == "on")
        elif cmd == "rgb":
            settings = {"effect": args.effect}
            for key in ("brightness", "speed", "direction"):
                if getattr(args, key) is not None:
                    settings[key] = getattr(args, key)
            if args.zones:
                settings["zones"] = args.zones
            if args.color:
                settings["color"] = args.color
                if args.effect == "static" and not args.zones:
                    settings["zones"] = [args.color] * rgb.ZONES
            request("set_rgb", settings=settings)
        elif cmd == "kb-timeout":
            request("set_kb_timeout", on=args.state == "on")
    except ServiceError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0
