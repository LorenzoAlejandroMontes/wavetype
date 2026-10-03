"""gatekeeper_click.py — answers "Open" to Gatekeeper's first-launch question on the CI runner.

The question is a window of CoreServicesUIAgent. System Events could not walk it (runs 14 and 15:
"Can't make ... entire contents ... into type specifier", -1700), so this goes one level down:
the accessibility API directly, then a real mouse click on the button, then on the place where
the button sits in the window. Prints everything it saw, for gatekeeper.log.

    python gatekeeper_click.py            # exit 0 = the question is gone, 1 = still there, 2 = never seen
"""
import sys
import time

import Quartz

OWNER = "CoreServicesUIAgent"
LABELS = ("Open", "Apri")
# where "Open" sits in the question, measured on the run 15 screenshot (window 260x264 pt at
# 382,130; button centre at 571,363): 71 pt from the right edge, 31 pt from the bottom
FROM_RIGHT, FROM_BOTTOM = 71, 31


def windows():
    """On-screen windows of CoreServicesUIAgent big enough to be the question."""
    out = []
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerName") != OWNER:
            continue
        b = w.get("kCGWindowBounds") or {}
        if b.get("Width", 0) < 150 or b.get("Height", 0) < 100:
            continue
        out.append({"pid": int(w.get("kCGWindowOwnerPID")), "layer": int(w.get("kCGWindowLayer", 0)),
                    "x": float(b["X"]), "y": float(b["Y"]), "w": float(b["Width"]), "h": float(b["Height"])})
    return out


def click(x, y):
    pt = Quartz.CGPointMake(x, y)
    for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        e = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.15)


def gone(wait=4.0):
    end = time.time() + wait
    while time.time() < end:
        if not windows():
            return True
        time.sleep(0.5)
    return False


def ax_buttons(pid):
    """[(title, element, centre or None)] for every button under the app's windows."""
    import ApplicationServices as AS

    def attr(el, name):
        try:
            err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
        except Exception as e:
            print(f"ax {name}: {type(e).__name__}: {e}")
            return None
        return val if err == 0 else None

    def centre(el):
        pos, size = attr(el, "AXPosition"), attr(el, "AXSize")
        if pos is None or size is None:
            return None
        try:
            ok1, p = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
            ok2, s = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
            return (p.x + s.width / 2, p.y + s.height / 2) if ok1 and ok2 else None
        except Exception as e:
            print(f"ax geometry: {type(e).__name__}: {e}")
            return None

    found, todo, seen = [], [], 0
    app = AS.AXUIElementCreateApplication(pid)
    wins = attr(app, "AXWindows")
    print(f"ax windows: {len(wins) if wins is not None else 'none (error or not allowed)'}")
    todo.extend(wins or [])
    while todo and seen < 400:
        el = todo.pop(0)
        seen += 1
        role = attr(el, "AXRole")
        if role == "AXButton":
            title = str(attr(el, "AXTitle") or attr(el, "AXDescription") or "")
            found.append((title, el, centre(el)))
        todo.extend(attr(el, "AXChildren") or [])
    print(f"ax elements walked: {seen}; buttons: {[(t, c) for t, _, c in found]}")
    return found


def main():
    ws = []
    for _ in range(20):                      # the question may take a few seconds to come up
        ws = windows()
        if ws:
            break
        time.sleep(1)
    print(f"windows of {OWNER}: {ws}")
    if not ws:
        print("RESULT no Gatekeeper window")
        return 2
    w = ws[0]

    target = None
    try:
        import ApplicationServices as AS
        print(f"AXIsProcessTrusted: {bool(AS.AXIsProcessTrusted())}")
        for title, el, c in ax_buttons(w["pid"]):
            if any(l in title for l in LABELS):
                target = c
                err = AS.AXUIElementPerformAction(el, "AXPress")
                print(f"AXPress on [{title}]: err={err}")
                if gone():
                    print("RESULT clicked Open (AXPress)")
                    return 0
                break
    except Exception as e:
        print(f"accessibility path failed: {type(e).__name__}: {e}")

    print(f"CGPreflightPostEventAccess: {bool(Quartz.CGPreflightPostEventAccess())}")
    if target:
        print(f"mouse click on the button centre {target}")
        click(*target)
        if gone():
            print("RESULT clicked Open (mouse, button centre from accessibility)")
            return 0
    ws = windows() or [w]
    w = ws[0]
    x, y = w["x"] + w["w"] - FROM_RIGHT, w["y"] + w["h"] - FROM_BOTTOM
    print(f"mouse click at {x:.0f},{y:.0f} (window {w})")
    click(x, y)
    if gone():
        print("RESULT clicked Open (mouse, position in the window)")
        return 0
    print(f"RESULT the question is still there: {windows()}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
