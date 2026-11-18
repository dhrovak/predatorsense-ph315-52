#!/bin/bash
# Removes PredatorSense and restores the stock acer_wmi driver.
# Usage: sudo ./uninstall.sh
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo: sudo ./uninstall.sh"
    exit 1
fi

systemctl disable --now predator-sense.service 2>/dev/null
rm -f /etc/systemd/system/predator-sense.service
systemctl daemon-reload

if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" && -x /usr/local/bin/predator-sense ]]; then
    user_uid="$(id -u "$SUDO_USER")"
    if [[ -S "/run/user/$user_uid/bus" ]]; then
        runuser -u "$SUDO_USER" -- env XDG_RUNTIME_DIR="/run/user/$user_uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$user_uid/bus" \
            /usr/local/bin/predator-sense shortcut --remove
    fi
fi

for v in $(dkms status -m facer 2>/dev/null | sed -n 's#^facer/\([^,:]*\).*#\1#p' | sort -u); do
    dkms remove -m facer -v "$v" --all
    rm -rf "/usr/src/facer-$v"
done
rm -f /etc/modprobe.d/predator-sense.conf /etc/modules-load.d/predator-sense.conf
modprobe -r facer 2>/dev/null
modprobe acer_wmi 2>/dev/null
if command -v update-initramfs >/dev/null; then
    update-initramfs -u >/dev/null 2>&1
elif command -v dracut >/dev/null; then
    dracut -f --kver "$(uname -r)" >/dev/null 2>&1
elif command -v mkinitcpio >/dev/null; then
    mkinitcpio -P >/dev/null 2>&1
fi

rm -rf /opt/predator-sense /var/lib/predator-sense
rm -f /usr/local/bin/predator-sense /usr/share/applications/predator-sense.desktop
groupdel predatorsense 2>/dev/null

echo "PredatorSense has been removed."
