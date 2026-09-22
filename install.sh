#!/usr/bin/env bash
# Linux installer: udev rule for sudo-free access, systemd service for boot and
# resume, and a symlink so 'loq-kbd' works from any directory.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo:  sudo $HERE/install.sh" >&2
    exit 1
fi

USER_NAME="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6 || true)"

if [ "$USER_NAME" = "root" ] || [ -z "$USER_HOME" ]; then
    echo "Could not work out which user to install for." >&2
    echo "Run it as yourself through sudo, not as root directly:" >&2
    echo "    sudo $HERE/install.sh" >&2
    exit 1
fi

echo "== 1/6  udev rule"
# Sweep away the 99- version if an older install left one: it ran after
# 73-seat-late.rules, so the ACL was never granted.
rm -f /etc/udev/rules.d/99-loq-keyboard.rules
install -m 0644 "$HERE/60-loq-keyboard.rules" /etc/udev/rules.d/60-loq-keyboard.rules
udevadm control --reload-rules
# The uaccess ACL is applied when the device is (re)announced, not when rules
# are reloaded, so the subsystem has to be retriggered.
udevadm trigger --subsystem-match=hidraw --action=add
udevadm settle

echo "== 2/6  loq-kbd on PATH"
install -d -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/.local/bin"
ln -sfn "$HERE/loq-kbd" "$USER_HOME/.local/bin/loq-kbd"
echo "   $USER_HOME/.local/bin/loq-kbd -> $HERE/loq-kbd"

echo "== 3/6  systemd service"
sed "s|__INSTALL_DIR__|$HERE|g" "$HERE/loq-kbd.service" \
    > /etc/systemd/system/loq-kbd.service
chmod 0644 /etc/systemd/system/loq-kbd.service
systemctl daemon-reload
systemctl enable loq-kbd.service >/dev/null
echo "   enabled for boot and for resume from suspend"

echo "== 4/6  your saved colour"
# Never clobber a colour the user already chose.
if [ ! -f "$HERE/color.conf" ]; then
    install -m 0644 -o "$USER_NAME" -g "$USER_NAME" \
        "$HERE/color.conf.example" "$HERE/color.conf"
    echo "   created color.conf (default: blue)"
else
    echo "   keeping the color.conf you already have"
fi

echo "== 5/6  applying it"
systemctl restart loq-kbd.service --no-block || true
sleep 1

echo "== 6/6  check"
# Ask the tool which node it found rather than repeating the VID:PID here. That
# keeps one source of truth, so changing PRODUCT_ID in loq_kbd.py for a related
# model does not leave this check hunting for the wrong device.
NODE="$("$HERE/loq-kbd" info 2>/dev/null | awk '$1 == "device" { print $2 }')"

if [ -z "$NODE" ]; then
    echo "   !! could not reach the keyboard lighting. Details:"
    "$HERE/loq-kbd" info 2>&1 | sed 's/^/      /' || true
    exit 1
fi
echo "   device: $NODE"

printf '   sudo-free access for %s: ' "$USER_NAME"
if ! command -v getfacl >/dev/null 2>&1; then
    echo "cannot tell (install the 'acl' package to check)"
elif getfacl -p "$NODE" 2>/dev/null | grep -q "^user:${USER_NAME}:.*rw"; then
    echo "yes"
else
    echo "not yet"
    echo "      The uaccess ACL is granted to the active seat session. If this"
    echo "      stays 'not yet', log out and back in once; the rule is in place."
fi

echo
echo "Done. Try:"
echo "   loq-kbd red          loq-kbd morado 60       loq-kbd ff8800"
echo "   loq-kbd mode3        loq-kbd modes           loq-kbd colours"
echo "   loq-kbd save mode3 50     # remember it for boot and resume"
echo
echo "If 'loq-kbd' is not found, your shell has not picked up ~/.local/bin yet:"
echo "   source ~/.bashrc"
