"""keys.py — synthetic hotkeys for the macOS CI run (Quartz CGEventPost).

Wavetype reads its keys through a CGEventTap: modifier-only chords arrive as flagsChanged events,
so that is what we post here, at the HID level, exactly like a real keyboard would.

    python keys.py ctrl-option       # quick press + release (< 0.4 s): toggle start/stop
    python keys.py fn                # Fn (Globe) quick press + release
    python keys.py fn-hold 6         # Fn held for 6 s (push-to-talk)
    python keys.py quit              # Ctrl+Option+Q
    python keys.py esc
    python keys.py preflight         # what this process is allowed to do (JSON)
"""
import json
import sys
import time

import Quartz

KC_CONTROL, KC_OPTION, KC_FN, KC_Q, KC_ESC = 59, 58, 63, 12, 53
CTRL = Quartz.kCGEventFlagMaskControl
OPT = Quartz.kCGEventFlagMaskAlternate
FN = Quartz.kCGEventFlagMaskSecondaryFn


def _post(e):
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def flags(keycode, mask):
    """One flagsChanged event: `keycode` is the modifier that moved, `mask` the flags after it."""
    e = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    Quartz.CGEventSetType(e, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(e, mask)
    _post(e)


def key(keycode, mask=0):
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventSetFlags(e, mask)
        _post(e)
        time.sleep(0.03)


def ctrl_option(hold=0.12):
    flags(KC_CONTROL, CTRL)
    time.sleep(0.03)
    flags(KC_OPTION, CTRL | OPT)
    time.sleep(hold)
    flags(KC_OPTION, CTRL)
    time.sleep(0.03)
    flags(KC_CONTROL, 0)


def fn(hold=0.12):
    flags(KC_FN, FN)
    time.sleep(hold)
    flags(KC_FN, 0)


def quit_chord():
    flags(KC_CONTROL, CTRL)
    time.sleep(0.03)
    flags(KC_OPTION, CTRL | OPT)
    time.sleep(0.05)
    key(KC_Q, CTRL | OPT)
    time.sleep(0.05)
    flags(KC_OPTION, CTRL)
    time.sleep(0.03)
    flags(KC_CONTROL, 0)


def esc():
    key(KC_ESC)


def preflight():
    """Permissions of THIS process (the driver), for the results: posting needs PostEvent."""
    out = {}
    for name in ("CGPreflightPostEventAccess", "CGPreflightListenEventAccess",
                 "CGPreflightScreenCaptureAccess"):
        f = getattr(Quartz, name, None)
        try:
            out[name] = bool(f()) if f else "n/a"
        except Exception as e:
            out[name] = f"error: {e}"
    try:
        from ApplicationServices import AXIsProcessTrusted
        out["AXIsProcessTrusted"] = bool(AXIsProcessTrusted())
    except Exception as e:
        out["AXIsProcessTrusted"] = f"error: {e}"
    return out


def press(name, hold=None):
    if name == "ctrl-option":
        ctrl_option(hold or 0.12)
    elif name == "fn":
        fn(hold or 0.12)
    elif name == "fn-hold":
        fn(hold or 6.0)
    elif name == "quit":
        quit_chord()
    elif name == "esc":
        esc()
    else:
        raise ValueError(name)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "preflight"
    if what == "preflight":
        print(json.dumps(preflight()))
    else:
        press(what, float(sys.argv[2]) if len(sys.argv) > 2 else None)
