# Patches for quax-circuitpython-hid Library

## usb_hid.py - Fix BOOT_KEYBOARD and BOOT_MOUSE Conflict

### Problem
Both `Device.BOOT_KEYBOARD` and `Device.BOOT_MOUSE` in the quax-circuitpython-hid library (version 6.1.5) have `report_ids=[0]`. This causes them to conflict when both are enabled simultaneously:

1. Both devices try to create the same USB function directory: `/sys/kernel/config/usb_gadget/adafruit-blinka/functions/hid.usb0`
2. The second device skips creation when the directory already exists
3. Both devices end up using the same `/dev/hidg0` device file
4. Only one device works at a time

### Solution
Changed `Device.BOOT_KEYBOARD` to use `report_ids=[0x1]` instead of `report_ids=[0x0]`. This ensures:
- BOOT_MOUSE gets `/dev/hidg0` (report_id=0)
- BOOT_KEYBOARD gets `/dev/hidg1` (report_id=1)
- Both devices can work simultaneously

### How to Apply
Run the install script from the project root:
```bash
./patches/install_patches.sh
```

Or manually copy the patched file:
```bash
cp patches/usb_hid.py ~/bluetooth_2_usb/venv/lib/python*/site-packages/usb_hid.py
```

### Note
This fix may need to be reapplied if the `quax-circuitpython-hid` package is reinstalled or upgraded.
