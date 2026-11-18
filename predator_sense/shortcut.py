"""GNOME keyboard shortcut that opens PredatorSense with the PredatorSense key (left of Num Lock)."""

import ast
import os
import subprocess

KEY = "XF86Presentation"  # the PredatorSense key sends evdev KEY_PRESENTATION
COMMAND = "/usr/local/bin/predator-sense"
SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
BINDING_SCHEMA = SCHEMA + ".custom-keybinding"
BINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/predator-sense/"


class ShortcutError(Exception):
    pass


def _gsettings(*args):
    try:
        result = subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ShortcutError(f"Cannot run gsettings: {e}") from e
    if result.returncode != 0:
        raise ShortcutError(result.stderr.strip() or f"gsettings {args[0]} failed")
    return result.stdout.strip()


def _bindings():
    text = _gsettings("get", SCHEMA, "custom-keybindings")
    text = text.removeprefix("@as ").strip()
    try:
        paths = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ShortcutError(f"Unexpected custom-keybindings value: {text!r}") from e
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ShortcutError(f"Unexpected custom-keybindings value: {text!r}")
    return paths


def _set_bindings(paths):
    _gsettings("set", SCHEMA, "custom-keybindings", "[" + ", ".join(f"'{p}'" for p in paths) + "]")


def install():
    paths = _bindings()
    relocatable = f"{BINDING_SCHEMA}:{BINDING_PATH}"
    _gsettings("set", relocatable, "name", "PredatorSense")
    _gsettings("set", relocatable, "command", COMMAND)
    _gsettings("set", relocatable, "binding", KEY)
    if BINDING_PATH not in paths:
        _set_bindings(paths + [BINDING_PATH])


def remove():
    paths = _bindings()
    if BINDING_PATH in paths:
        _set_bindings([p for p in paths if p != BINDING_PATH])
    relocatable = f"{BINDING_SCHEMA}:{BINDING_PATH}"
    for key in ("name", "command", "binding"):
        _gsettings("reset", relocatable, key)


def run(remove_shortcut=False):
    if os.geteuid() == 0:
        print("Run this as your normal user (not with sudo): predator-sense shortcut")
        return 1
    try:
        if remove_shortcut:
            remove()
            print("The PredatorSense key shortcut was removed.")
        else:
            install()
            print("The PredatorSense key now opens PredatorSense.")
    except ShortcutError as e:
        print(f"Could not change the GNOME shortcut: {e}")
        return 1
    return 0
