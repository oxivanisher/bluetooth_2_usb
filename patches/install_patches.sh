#!/bin/bash
# Install patches for quax-circuitpython-hid library

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Find the site-packages directory in the venv
SITE_PACKAGES=$(find "$PROJECT_ROOT/venv/lib" -type d -name "site-packages" 2>/dev/null | head -n 1)

if [ -z "$SITE_PACKAGES" ]; then
    echo "Error: Could not find site-packages directory in venv"
    exit 1
fi

echo "Found site-packages: $SITE_PACKAGES"

# Backup original file if it exists and hasn't been backed up yet
if [ -f "$SITE_PACKAGES/usb_hid.py" ] && [ ! -f "$SITE_PACKAGES/usb_hid.py.orig" ]; then
    echo "Backing up original usb_hid.py to usb_hid.py.orig"
    cp "$SITE_PACKAGES/usb_hid.py" "$SITE_PACKAGES/usb_hid.py.orig"
fi

# Apply the patch
echo "Applying usb_hid.py patch..."
cp "$SCRIPT_DIR/usb_hid.py" "$SITE_PACKAGES/usb_hid.py"

echo "Patch applied successfully!"
echo ""
echo "Changes:"
echo "- Fixed BOOT_KEYBOARD report_id conflict (0x0 -> 0x1)"
echo "- BOOT_MOUSE will use /dev/hidg0"
echo "- BOOT_KEYBOARD will use /dev/hidg1"
echo ""
echo "Restart the bluetooth_2_usb service for changes to take effect:"
echo "  sudo systemctl restart bluetooth_2_usb"
