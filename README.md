# PredatorSense 2026 for the Acer Predator Helios 300 PH315-52 model on Linux

> **COMPLETED AND WORKING AS OF NOV 18TH 2026**

Fan control, turbo and 4-zone keyboard RGB for the **Acer Predator PH315-52**, with a Qt GUI, a CLI and a small root service.

> **Only tested on the Predator PH315-52 (BIOS V1.12) running Debian 13.** The installer refuses to run on other models.
> Use at your own risk: this software changes fan behaviour on your laptop.

## Features

- Live CPU/GPU temperature and fan speed, with a 60-second chart
- Fan modes: **Auto**, **Turbo** (both fans at max) or **Custom** (0-100% per fan)
- Keyboard lighting: static (4 zones), breath, neon, wave, shifting, zoom
- Keyboard backlight 30-second idle timeout on or off (always lit)
- Settings persist across reboots and suspend/resume
- Opens with the PredatorSense key (left of Num Lock) on GNOME

## Install

Requires a systemd-based distro, Python 3.9+ and internet access to fetch the facer module source.

| Distro family | Package manager | Status |
| --- | --- | --- |
| Debian | apt | Tested (Debian 13, kernel 6.12) |
| Ubuntu, Linux Mint, Pop!_OS | apt | Supported, untested |
| Fedora | dnf | Supported, untested |
| Arch, EndeavourOS, Manjaro | pacman | Supported, untested |
| Others (systemd) | — | Install the dependencies yourself, then run the installer |

On other distros, install PyQt6 (for `/usr/bin/python3`), `dkms`, `git`, `gcc`, `make` and the headers for your running kernel first; the installer checks for them and stops if anything is missing. On Arch, run `pacman -Syu` and reboot before installing so the headers match the running kernel.

```sh
git clone https://github.com/dhrovak/predatorsense-ph315-52.git
cd predatorsense-ph315-52
sudo ./install.sh
```

The installer:

1. installs PyQt6, `dkms`, `git`, a compiler and the kernel headers;
2. builds a patched [facer](https://github.com/JafarAkhondali/acer-predator-turbo-and-rgb-keyboard-linux-module) module with DKMS (it replaces `acer_wmi`, which is blacklisted);
3. installs the app to `/opt/predator-sense` and the `predator-sense` command;
4. creates the `predatorsense` group and adds your user to it (log out and back in afterwards);
5. starts `predator-sense.service` and runs the hardware check.

With Secure Boot enabled the module only loads if DKMS signs it with a key enrolled via `mokutil`.

To remove everything and restore `acer_wmi`:

```sh
sudo ./uninstall.sh
```

## Usage

```sh
predator-sense                         # open the window
predator-sense status
predator-sense fan auto                # or: max, 0-100
predator-sense fan --cpu 60 --gpu 80
predator-sense turbo on
predator-sense rgb wave --speed 5 --brightness 80
predator-sense rgb static --zones '#ff0000' '#00ff00' '#0000ff' '#ffffff'
predator-sense kb-timeout off          # keep the keyboard backlight on (on: off after 30 s idle)
sudo predator-sense verify             # re-run the hardware check
```

## Safety

- Fans are set through the firmware's own gaming WMI methods (the ones PredatorSense uses on Windows). Nothing is ever written to the embedded controller.
- Fan control stays locked until `sudo predator-sense verify` confirms the sensors read correctly and both fans respond. The check is repeated automatically when the BIOS or kernel module changes.
- If any temperature reaches 95°C while a fan is on a custom speed, both fans go back to Auto.
- The GPU fan cannot be set below 40% while the GPU temperature cannot be read.
- Fans go back to Auto when the service stops, even after a crash.

## Development

Tests use only the standard library:

```sh
PYTHONPATH=. python3 -m unittest discover -s tests
```

## Credits

- [acer-predator-turbo-and-rgb-keyboard-linux-module (facer)](https://github.com/JafarAkhondali/acer-predator-turbo-and-rgb-keyboard-linux-module) by Jafar Akhondali — keyboard RGB and the kernel module this project extends.
- [Linuwu-Sense](https://github.com/0x7375646F/Linuwu-Sense) — gaming WMI fan behaviour and speed methods.
- The mainline Linux `acer-wmi` driver — Predator Sense v4 sensor IDs.

## License

Copyright (C) 2026 Dhruv Bhat

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. See [LICENSE](LICENSE).

The kernel code in `packaging/facer/` is compiled into the facer module, which is itself licensed under the GPL.
