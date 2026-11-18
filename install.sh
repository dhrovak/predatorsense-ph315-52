#!/bin/bash
# One-time installer for PredatorSense on the Acer Predator PH315-52.
# Usage: sudo ./install.sh
set -euo pipefail

FACER_REPO="https://github.com/JafarAkhondali/acer-predator-turbo-and-rgb-keyboard-linux-module"
FACER_COMMIT="e97bb6354e4366c0d97121bedec19d536323108d"
FACER_VERSION="git.e97bb63-ps2"
APP_DIR="/opt/predator-sense"
GROUP="predatorsense"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KERNEL="$(uname -r)"

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

detect_package_manager() {
    local pm
    for pm in apt-get dnf pacman; do
        if command -v "$pm" >/dev/null; then
            echo "$pm"
            return
        fi
    done
    echo none
}

install_packages() {
    case "$1" in
        apt-get)
            apt-get update -qq
            DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pyqt6 dkms git gcc make
            ;;
        dnf)
            dnf install -y python3 python3-pyqt6 dkms git gcc make
            ;;
        pacman)
            # No -y: a partial upgrade is unsafe on Arch; a stale database just makes this fail.
            pacman -S --needed --noconfirm python python-pyqt6 dkms git gcc make \
                || die "pacman could not install the packages. Run 'pacman -Syu', reboot, then re-run this installer."
            ;;
        *)
            warn "No supported package manager (apt, dnf or pacman) found."
            warn "Install these yourself: python3, PyQt6, dkms, git, gcc, make and the headers for kernel $KERNEL."
            ;;
    esac
}

# Headers for the running kernel, plus the meta package that keeps them in step with kernel updates.
install_kernel_headers() {
    local pkgs=() flavour kernel_pkg
    case "$1" in
        apt-get)
            pkgs=("linux-headers-$KERNEL")
            for flavour in "$(dpkg --print-architecture)" generic; do
                if [[ "$(dpkg-query -W -f='${Status}' "linux-image-$flavour" 2>/dev/null || true)" == *"ok installed" ]]; then
                    pkgs+=("linux-headers-$flavour")
                fi
            done
            DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
            ;;
        dnf)
            dnf install -y "kernel-devel-$KERNEL"
            ;;
        pacman)
            kernel_pkg="$(pacman -Qqo "/usr/lib/modules/$KERNEL/vmlinuz" 2>/dev/null || echo linux)"
            pacman -S --needed --noconfirm "$kernel_pkg-headers"
            ;;
        *)
            return 0
            ;;
    esac
}

regenerate_initramfs() {
    if command -v update-initramfs >/dev/null; then
        update-initramfs -u
    elif command -v dracut >/dev/null; then
        dracut -f --kver "$KERNEL"
    elif command -v mkinitcpio >/dev/null; then
        mkinitcpio -P
    else
        return 0
    fi
}

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo: sudo ./install.sh"
    exit 1
fi

model="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
if [[ "$model" != "Predator PH315-52" ]]; then
    echo "This installer is only for the Acer Predator PH315-52 (this laptop reports: ${model:-unknown})."
    exit 1
fi

if [[ ! -d /run/systemd/system ]]; then
    die "PredatorSense needs systemd to run its background service."
fi

if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; then
    warn "Secure Boot is on: the facer module will only load if DKMS signs it with a key enrolled via mokutil."
fi

pkg_manager="$(detect_package_manager)"
step "Installing required packages (${pkg_manager/apt-get/apt})"
install_packages "$pkg_manager"
install_kernel_headers "$pkg_manager" || warn "could not install the headers for kernel $KERNEL"

step "Checking requirements"
missing=()
for cmd in python3 dkms git gcc make; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
done
if ! /usr/bin/python3 -c 'import PyQt6.QtWidgets, PyQt6.QtNetwork' 2>/dev/null; then
    missing+=("PyQt6 for /usr/bin/python3")
fi
if [[ ! -d "/lib/modules/$KERNEL/build" ]]; then
    missing+=("kernel headers for $KERNEL (if you just updated the kernel, reboot first)")
fi
if (( ${#missing[@]} )); then
    printf -v missing_list '%s, ' "${missing[@]}"
    die "missing: ${missing_list%, }"
fi
echo "All requirements found."

step "Building the facer kernel module (keyboard RGB and fan control)"
facer_rebuilt=0
if dkms status -m facer -v "$FACER_VERSION" 2>/dev/null | grep -q installed; then
    echo "facer $FACER_VERSION is already installed."
else
    for old in $(dkms status -m facer 2>/dev/null | sed -n 's#^facer/\([^,:]*\).*#\1#p' | sort -u); do
        dkms remove -m facer -v "$old" --all || true
        rm -rf "/usr/src/facer-$old"
    done
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    git -C "$tmp" init -q
    git -C "$tmp" fetch -q --depth 1 "$FACER_REPO" "$FACER_COMMIT"
    git -C "$tmp" checkout -q FETCH_HEAD
    git -C "$tmp" apply "$SRC_DIR/packaging/facer/turbo-sync.patch"
    facer_src="/usr/src/facer-$FACER_VERSION"
    rm -rf "$facer_src"
    mkdir -p "$facer_src"
    cp "$tmp/src/facer.c" "$facer_src/"
    sed "s/#VERSION#/$FACER_VERSION/" "$SRC_DIR/packaging/facer/predator-sense-fan.c" >> "$facer_src/facer.c"
    cp "$SRC_DIR/packaging/facer/Makefile" "$facer_src/"
    sed "s/#VERSION#/$FACER_VERSION/" "$SRC_DIR/packaging/facer/dkms.conf" > "$facer_src/dkms.conf"
    dkms add -m facer -v "$FACER_VERSION"
    dkms install -m facer -v "$FACER_VERSION" -k "$KERNEL" \
        || die "building facer for kernel $KERNEL failed; see /var/lib/dkms/facer/$FACER_VERSION/build/make.log"
    facer_rebuilt=1
fi

step "Configuring kernel modules"
cat > /etc/modprobe.d/predator-sense.conf <<'EOF'
# facer is an extended copy of acer_wmi, so the stock driver must stay unloaded.
blacklist acer_wmi
EOF
# ec_sys is only a sensor fallback and not every kernel ships it.
if modinfo ec_sys >/dev/null 2>&1; then
    printf 'facer\nec_sys\n' > /etc/modules-load.d/predator-sense.conf
    modprobe ec_sys || warn "could not load ec_sys (only needed as a sensor fallback)"
else
    printf 'facer\n' > /etc/modules-load.d/predator-sense.conf
fi
# PredatorSense only reads the EC; turn off write access left on by older versions.
if [[ -w /sys/module/ec_sys/parameters/write_support ]]; then
    echo N > /sys/module/ec_sys/parameters/write_support || warn "could not turn off EC write support"
fi
if grep -q '^acer_wmi ' /proc/modules; then
    modprobe -r acer_wmi || warn "could not unload acer_wmi now; it will be gone after a reboot"
fi
if [[ $facer_rebuilt -eq 1 ]] && grep -q '^facer ' /proc/modules; then
    systemctl stop predator-sense.service 2>/dev/null || true
    modprobe -r facer || warn "could not unload the old facer module; reboot to use the new one"
fi
modprobe facer || warn "could not load facer now; it will load after a reboot"
regenerate_initramfs >/dev/null 2>&1 || warn "could not regenerate the initramfs; acer_wmi may still load early at boot"

step "Installing the PredatorSense app"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
cp -r "$SRC_DIR/predator_sense" "$APP_DIR/"
find "$APP_DIR" -name __pycache__ -prune -exec rm -rf {} +
install -m 644 "$SRC_DIR/packaging/predator-sense.svg" "$APP_DIR/"
chown -R root:root "$APP_DIR"
chmod -R u=rwX,go=rX "$APP_DIR"
cat > /usr/local/bin/predator-sense <<EOF
#!/bin/sh
PYTHONPATH=$APP_DIR exec /usr/bin/python3 -m predator_sense "\$@"
EOF
chmod 755 /usr/local/bin/predator-sense
install -m 644 "$SRC_DIR/packaging/predator-sense.desktop" /usr/share/applications/
install -m 644 "$SRC_DIR/packaging/predator-sense.service" /etc/systemd/system/
update-desktop-database -q /usr/share/applications 2>/dev/null || true

step "Allowing your user to control the hardware without a password"
groupadd -f "$GROUP"
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    usermod -aG "$GROUP" "$SUDO_USER"
    echo "Added $SUDO_USER to the $GROUP group (log out and back in if this is new)."
fi

step "Starting the background service"
systemctl daemon-reload
systemctl enable predator-sense.service >/dev/null
systemctl restart predator-sense.service

step "Hardware check (unlocks fan control; repeated when the BIOS or facer module changes)"
/usr/local/bin/predator-sense verify --if-needed \
    || warn "hardware check did not pass; fan control stays locked. Run 'sudo predator-sense verify' to retry."

step "Making the PredatorSense key (left of Num Lock) open the app"
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    user_uid="$(id -u "$SUDO_USER")"
    if [[ -S "/run/user/$user_uid/bus" ]]; then
        runuser -u "$SUDO_USER" -- env XDG_RUNTIME_DIR="/run/user/$user_uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$user_uid/bus" \
            /usr/local/bin/predator-sense shortcut \
            || warn "could not add the keyboard shortcut; run 'predator-sense shortcut' from your desktop session."
    else
        warn "no desktop session found for $SUDO_USER; run 'predator-sense shortcut' after logging in."
    fi
fi

cat <<'EOF'

PredatorSense is installed.
  Open it:   press the PredatorSense key, search for "PredatorSense" in your applications menu, or run: predator-sense
  Terminal:  predator-sense status | fan auto|max|0-100 | turbo on|off | rgb <effect>
EOF
