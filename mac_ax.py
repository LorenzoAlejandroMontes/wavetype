"""
mac_ax.py — caret, testo prima del caret, selezione e app in primo piano su macOS (AX + AppKit).

Stesso contratto di caret.py e context.py su Windows, cosi' live_panel.py e wavetype.py non
cambiano: rettangoli in pixel (punti * backingScaleFactor), origine in alto a sinistra dello
schermo principale (vedi mac_geom.py). Il "bersaglio" (hwnd su Windows) qui e' il pid dell'app
in primo piano.

Catena del caret (budget ~150 ms, AXUIElementSetMessagingTimeout):
  elemento con focus dell'app -> kAXSelectedTextRangeAttribute -> AXBoundsForRange
  (con un range lungo 0 molte app tornano un rettangolo vuoto: si allarga di un carattere).
  Se l'elemento non risponde: None, e la card va in basso al centro dello schermo attivo.

Chrome ed Electron accendono il loro albero AX solo se qualcuno lo chiede: si imposta
AXManualAccessibility sull'app (una volta per pid), come fanno gli altri dettatori.

Tutto in try/except: un errore qui non deve mai far cadere la dettatura.
"""
import time

import mac_geom

BUDGET_S = 0.150
LOOKBACK = 120

_LOG = [print]
_ax = {"mod": None, "tried": False, "err": None, "manual": set()}
LAST = {"source": None, "screen": None}     # ultimo gradino che ha risposto, ultimo schermo usato


def set_log(fn):
    _LOG[0] = fn


def log(msg):
    try:
        _LOG[0](msg)
    except Exception:
        pass


def _AX():
    """Il modulo ApplicationServices (PyObjC), importato una volta. None se manca."""
    if _ax["mod"] is not None or _ax["tried"]:
        return _ax["mod"]
    _ax["tried"] = True
    try:
        import ApplicationServices as AS
        _ax["mod"] = AS
    except Exception as e:
        _ax["err"] = e
        log(f"   [ax] ApplicationServices non disponibile: {e}")
    return _ax["mod"]


def _attr(el, name):
    """Valore di un attributo AX, o None (errore, attributo assente, app che non risponde)."""
    AS = _AX()
    if AS is None or el is None:
        return None
    try:
        err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
        return val if err == AS.kAXErrorSuccess else None
    except Exception:
        return None


def _vtype(AS, kind):
    """Tipo di AXValue: il nome nuovo (kAXValueTypeCFRange, quello dei test di PyObjC) o il
    vecchio (kAXValueCFRangeType), quale dei due c'e'."""
    return getattr(AS, "kAXValueType" + kind, None) or getattr(AS, "kAXValue" + kind + "Type")


def _range_of(axval):
    """AXValue di tipo CFRange -> (location, length), o None."""
    AS = _AX()
    try:
        ok, r = AS.AXValueGetValue(axval, _vtype(AS, "CFRange"), None)
        if not ok:
            return None
        try:
            return int(r.location), int(r.length)
        except AttributeError:
            return int(r[0]), int(r[1])
    except Exception:
        return None


def _rect_of(axval):
    """AXValue di tipo CGRect -> (x, y, w, h) in punti (origine in alto), o None."""
    AS = _AX()
    try:
        ok, r = AS.AXValueGetValue(axval, _vtype(AS, "CGRect"), None)
        if not ok:
            return None
        try:
            return (float(r.origin.x), float(r.origin.y), float(r.size.width), float(r.size.height))
        except AttributeError:
            (x, y), (w, h) = r
            return (float(x), float(y), float(w), float(h))
    except Exception:
        return None


# ------------------------------------------------------------------ app e schermi
def frontmost_pid():
    """pid dell'app in primo piano (il "bersaglio" della dettatura), 0 se non si sa."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is not None:
            return int(app.processIdentifier())
    except Exception:
        pass
    AS = _AX()
    try:
        sysw = AS.AXUIElementCreateSystemWide()
        app = _attr(sysw, AS.kAXFocusedApplicationAttribute)
        if app is not None:
            err, pid = AS.AXUIElementGetPid(app, None)
            if err == AS.kAXErrorSuccess:
                return int(pid)
    except Exception:
        pass
    return 0


def app_name(pid):
    """Nome dell'app (per il log, come il titolo della finestra su Windows)."""
    try:
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app is not None:
            return str(app.localizedName() or "")
    except Exception:
        pass
    return ""


def screens():
    """[(frame, visibleFrame, scale)] da NSScreen, il principale per primo. [] se AppKit manca."""
    try:
        from AppKit import NSScreen
        out = []
        for s in NSScreen.screens() or []:
            f, v = s.frame(), s.visibleFrame()
            out.append(((f.origin.x, f.origin.y, f.size.width, f.size.height),
                        (v.origin.x, v.origin.y, v.size.width, v.size.height),
                        float(s.backingScaleFactor() or 1.0)))
        return out
    except Exception as e:
        log(f"   [ax] schermi: {e}")
        return []


def cursor_pos():
    """Posizione del mouse in pixel (contratto Wavetype). Quartz da' gia' l'origine in alto."""
    try:
        import Quartz
        p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        x, y, i = mac_geom.pt_to_px(p.x, p.y, screens())
        LAST["screen"] = i
        return int(x), int(y)
    except Exception:
        return 0, 0


def work_area(x, y):
    return mac_geom.work_area_px(x, y, screens(), LAST["screen"])


def dpi_for_point(x, y):
    return mac_geom.dpi_px(x, y, screens(), LAST["screen"])


class dpi_scope:
    """Su Mac non serve (le coordinate sono gia' quelle del contratto): resta per le chiamate
    condivise con Windows."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------------------ elemento con focus
def _focused(pid, budget):
    AS = _AX()
    if AS is None:
        return None
    try:
        root = AS.AXUIElementCreateApplication(int(pid)) if pid else AS.AXUIElementCreateSystemWide()
        try:
            AS.AXUIElementSetMessagingTimeout(root, float(max(0.05, budget)))
        except Exception:
            pass
        if pid and pid not in _ax["manual"]:
            _ax["manual"].add(pid)
            try:                         # Chrome/Electron: accende l'albero AX (ignorato dagli altri)
                AS.AXUIElementSetAttributeValue(root, "AXManualAccessibility", True)
            except Exception:
                pass
        el = _attr(root, AS.kAXFocusedUIElementAttribute)
        if el is not None:
            try:
                AS.AXUIElementSetMessagingTimeout(el, float(max(0.05, budget)))
            except Exception:
                pass
        return el
    except Exception as e:
        log(f"   [ax] focus: {e}")
        return None


def _bounds(el, loc, length):
    AS = _AX()
    try:
        rng = AS.AXValueCreate(_vtype(AS, "CFRange"), (int(loc), int(length)))
        err, val = AS.AXUIElementCopyParameterizedAttributeValue(
            el, AS.kAXBoundsForRangeParameterizedAttribute, rng, None)
        if err != AS.kAXErrorSuccess or val is None:
            return None
        return _rect_of(val)
    except Exception:
        return None


def get_caret_rect(hwnd=None, budget=BUDGET_S):
    """(x, y, w, h) del caret in pixel schermo, oppure None. hwnd = pid dell'app bersaglio."""
    rect, src, _ms = get_caret_rect_verbose(hwnd, budget)
    return rect


def get_caret_rect_verbose(hwnd=None, budget=BUDGET_S):
    t0 = time.perf_counter()
    rect, src = None, None
    try:
        pid = int(hwnd or 0) or frontmost_pid()
        el = _focused(pid, budget)
        AS = _AX()
        if el is not None and AS is not None:
            sel = _attr(el, AS.kAXSelectedTextRangeAttribute)
            rg = _range_of(sel) if sel is not None else None
            if rg is not None:
                loc, ln = rg
                r, src = _bounds(el, loc, ln), "ax-bounds"
                if (r is None or r[3] <= 0) and time.perf_counter() - t0 < budget:
                    r, src = _bounds(el, loc, 1), "ax-bounds+1"     # caret degenere: un carattere
                    if (r is None or r[3] <= 0) and loc > 0:
                        r, src = _bounds(el, loc - 1, 1), "ax-bounds-1"   # fine testo: quello prima
                        if r is not None:
                            r = (r[0] + r[2], r[1], 1.0, r[3])
                if r is not None and r[3] > 0:
                    (x, y, w, h), i = mac_geom.rect_pt_to_px(r, screens())
                    LAST["screen"] = i
                    rect = (int(x), int(y), max(1, int(w)), int(h))
    except Exception as e:
        log(f"   [caret] ax: {e}")
        rect = None
    ms = (time.perf_counter() - t0) * 1000
    LAST["source"] = src if rect else None
    if rect:
        log(f"   [caret] {src} -> {rect} in {ms:.0f} ms")
    else:
        log(f"   [caret] AX non ha risposto ({ms:.0f} ms): card in basso al centro")
    return rect, (src if rect else None), ms


def prewarm():
    ok = _AX() is not None
    log(f"   [caret] prewarm ax={ok}")
    return ok


# ------------------------------------------------------------------ testo (context.py, Edit)
def _utf16_slice(s, start, end):
    """Gli indici AX sono unita' UTF-16 (NSString), quelli di Python sono code point."""
    u = s.encode("utf-16-le")
    return u[max(0, start) * 2:max(0, end) * 2].decode("utf-16-le", errors="ignore")


def before_caret(hwnd=None, lookback=LOOKBACK, budget=1.5):
    """Testo che precede il cursore (al massimo `lookback` caratteri), '' se il campo e' vuoto,
    None se non si sa."""
    AS = _AX()
    if AS is None:
        return None
    try:
        pid = int(hwnd or 0) or frontmost_pid()
        if hwnd and frontmost_pid() not in (0, pid):
            return None                   # la finestra bersaglio non e' piu' davanti
        el = _focused(pid, budget)
        if el is None:
            return None
        val = _attr(el, AS.kAXValueAttribute)
        if not isinstance(val, str):      # NSString arriva come sottoclasse di str; un numero,
            return None                   # un booleano o niente: non e' un campo di testo
        s = str(val)
        sel = _attr(el, AS.kAXSelectedTextRangeAttribute)
        rg = _range_of(sel) if sel is not None else None
        if rg is None:
            return None
        loc = rg[0]
        return _utf16_slice(s, loc - lookback, loc)
    except Exception as e:
        log(f"   [ctx] ax: {e}")
        return None


def selected_text(hwnd=None, budget=0.3):
    """La selezione dell'elemento con focus: testo, '' (niente selezionato) o None (l'app non
    risponde via AX: allora chi chiama ripiega su Cmd+C)."""
    AS = _AX()
    if AS is None:
        return None
    try:
        el = _focused(int(hwnd or 0) or frontmost_pid(), budget)
        if el is None:
            return None
        err, val = AS.AXUIElementCopyAttributeValue(el, AS.kAXSelectedTextAttribute, None)
        if err != AS.kAXErrorSuccess or val is None:
            return None
        return str(val)
    except Exception:
        return None
