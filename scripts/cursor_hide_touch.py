#!/usr/bin/env python3
"""Hide the mouse pointer on the kiosk by injecting one synthetic touch tap.

The Pi's USB touch panel presents a mouse interface, so wlroots/labwc draws a
pointer cursor at startup. wlroots hides the pointer cursor as soon as *touch*
input is used (touch and pointer are tracked separately). WPE/cog does not
honour the empty XCURSOR theme or CSS `cursor:none` until a pointer event, so
the cursor otherwise stays visible until someone touches the screen.

This creates a virtual multitouch device via uinput and taps once in an empty
corner (no button there) after the compositor is up, which makes the pointer
cursor disappear. Must run as root (for /dev/uinput). stdlib only.
"""
import fcntl
import os
import struct
import sys
import time

# uinput / input ioctls (generic Linux _IOC encoding; same on arm64/x86)
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

WAYLAND_SOCK = "/tmp/frame-runtime/wayland-0"
# Tap here: an empty corner of the viewer, away from any button.
TAP_X, TAP_Y = 30, 600
MAX_X, MAX_Y = 1920, 1200


def wait_for_compositor(timeout=90):
    """Wait until the kiosk compositor socket exists, then a little longer so
    cog has mapped and grabbed input."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(WAYLAND_SOCK):
            time.sleep(6)
            return True
        time.sleep(2)
    return False


def main():
    if not wait_for_compositor():
        print("[cursor-hide] compositor socket never appeared; tapping anyway")

    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for axis in (ABS_X, ABS_Y, ABS_MT_SLOT,
                 ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID):
        fcntl.ioctl(fd, UI_SET_ABSBIT, axis)

    absmax = [0] * 64
    absmax[ABS_X] = MAX_X
    absmax[ABS_Y] = MAX_Y
    absmax[ABS_MT_SLOT] = 9
    absmax[ABS_MT_POSITION_X] = MAX_X
    absmax[ABS_MT_POSITION_Y] = MAX_Y
    absmax[ABS_MT_TRACKING_ID] = 65535
    zeros = [0] * 64

    name = b"mimir-touch-hide".ljust(80, b"\x00")
    # struct uinput_user_dev: name[80], input_id(4*u16), ff_effects_max(u32),
    # absmax[64], absmin[64], absfuzz[64], absflat[64]  (all s32)
    dev = (name
           + struct.pack("HHHH", 0x03, 0x1234, 0x9ABC, 1)
           + struct.pack("I", 0)
           + struct.pack("i" * 64, *absmax)
           + struct.pack("i" * 64, *zeros)
           + struct.pack("i" * 64, *zeros)
           + struct.pack("i" * 64, *zeros))
    os.write(fd, dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    # Give udev/libinput/wlroots time to pick up the new device.
    time.sleep(3)

    def emit(etype, code, value):
        os.write(fd, struct.pack("qqHHi", 0, 0, etype, code, value))

    # touch down
    emit(EV_ABS, ABS_MT_SLOT, 0)
    emit(EV_ABS, ABS_MT_TRACKING_ID, 1)
    emit(EV_ABS, ABS_MT_POSITION_X, TAP_X)
    emit(EV_ABS, ABS_MT_POSITION_Y, TAP_Y)
    emit(EV_ABS, ABS_X, TAP_X)
    emit(EV_ABS, ABS_Y, TAP_Y)
    emit(EV_KEY, BTN_TOUCH, 1)
    emit(EV_SYN, SYN_REPORT, 0)
    time.sleep(0.15)
    # touch up
    emit(EV_ABS, ABS_MT_SLOT, 0)
    emit(EV_ABS, ABS_MT_TRACKING_ID, -1)
    emit(EV_KEY, BTN_TOUCH, 0)
    emit(EV_SYN, SYN_REPORT, 0)
    time.sleep(1)

    fcntl.ioctl(fd, UI_DEV_DESTROY)
    os.close(fd)
    print("[cursor-hide] touch tap injected")

    # The tap can trigger labwc's (now empty) root menu popup; send Escape via a
    # short-lived virtual keyboard to dismiss it. The menu grabs the keyboard
    # while open, so Escape closes it and never reaches the web app.
    send_escape()


UI_SET_KEYBIT = 0x40045565  # (already defined above; harmless re-bind)
KEY_ESC = 1


def send_escape():
    try:
        kfd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(kfd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(kfd, UI_SET_KEYBIT, KEY_ESC)
        name = b"mimir-key-esc".ljust(80, b"\x00")
        dev = (name
               + struct.pack("HHHH", 0x03, 0x1234, 0x9ABD, 1)
               + struct.pack("I", 0)
               + struct.pack("i" * 64, *([0] * 64)) * 4)
        os.write(kfd, dev)
        fcntl.ioctl(kfd, UI_DEV_CREATE)
        time.sleep(2)

        def emit(etype, code, value):
            os.write(kfd, struct.pack("qqHHi", 0, 0, etype, code, value))

        emit(EV_KEY, KEY_ESC, 1)
        emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(0.05)
        emit(EV_KEY, KEY_ESC, 0)
        emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(0.5)
        fcntl.ioctl(kfd, UI_DEV_DESTROY)
        os.close(kfd)
        print("[cursor-hide] escape injected (menu dismissed)")
    except Exception as e:
        print("[cursor-hide] escape failed: %s" % e, file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the boot
        print("[cursor-hide] error: %s" % e, file=sys.stderr)
        sys.exit(0)
