"""
caret.py — dove sta il cursore di testo (caret) dell'app attiva, in pixel schermo.

Serve al pannello live: deve comparire accanto al punto in cui l'utente sta scrivendo,
senza mai coprire la riga che sta scrivendo e senza mai bloccare la dettatura.

Catena di ripiego (si ferma al primo che risponde, budget totale ~150 ms):
  1. UI Automation (comtypes): elemento con focus -> TextPattern2.GetCaretRange
     -> GetBoundingRectangles; se non c'e', TextPattern.GetSelection.
  2. MSAA: AccessibleObjectFromWindow(hwnd, OBJID_CARET) -> accLocation.
  3. GetGUIThreadInfo().rcCaret + ClientToScreen (il caret "classico" di Win32).
  4. None: chi chiama si piazza in basso al centro dell'area di lavoro del monitor.

Tutto in try/except: un errore qui non deve mai far cadere la dettatura.
Il COM va inizializzato sul thread che chiama (lo facciamo da soli, e' idempotente).

NOTA DPI: le coordinate cambiano se il processo e' "DPI unaware" (Windows le scala).
Usa `dpi_scope()` attorno alle chiamate: alza la consapevolezza DPI SOLO su questo
thread e la rimette com'era, cosi' le coordinate sono pixel fisici veri.
"""
import ctypes
import time
from ctypes import wintypes

_user = ctypes.windll.user32
_shcore = None
try:
    _shcore = ctypes.windll.shcore
except Exception:
    pass

# --- budget: se sforiamo, si smette di provare e si torna None ---
BUDGET_S = 0.150

_LOG = [print]


def set_log(fn):
    """Il chiamante puo' passare il log() di wavetype.py."""
    _LOG[0] = fn


def log(msg):
    try:
        _LOG[0](msg)
    except Exception:
        pass


# ---------------------------------------------------------------- DPI
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

try:
    _user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    _user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _HAS_THREAD_DPI = True
except Exception:
    _HAS_THREAD_DPI = False


class dpi_scope:
    """Per la durata del blocco questo thread ragiona in pixel fisici (per-monitor v2).

    Serve perche' wavetype.py gira DPI-unaware: senza questo, su un monitor al 150%
    le coordinate arrivano divise per 1,5 e la finestra viene stirata da Windows.
    Alla fine rimette il contesto di prima: l'HUD-cane non se ne accorge.
    """

    def __enter__(self):
        self.prev = None
        if _HAS_THREAD_DPI:
            try:
                self.prev = _user.SetThreadDpiAwarenessContext(
                    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            except Exception:
                self.prev = None
        return self

    def __exit__(self, *exc):
        if self.prev:
            try:
                _user.SetThreadDpiAwarenessContext(ctypes.c_void_p(self.prev))
            except Exception:
                pass
        return False


def dpi_for_point(x, y):
    """DPI del monitor che contiene il punto (96 = 100%). Va chiamato dentro dpi_scope()."""
    try:
        mon = _user.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)   # NEAREST
        dx, dy = wintypes.UINT(), wintypes.UINT()
        if _shcore and _shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return int(dx.value) or 96
    except Exception:
        pass
    try:
        return int(_user.GetDpiForSystem()) or 96
    except Exception:
        return 96


def work_area(x, y):
    """Area di lavoro (senza barra delle applicazioni) del monitor sotto al punto."""
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    try:
        mon = _user.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if _user.GetMonitorInfoW(mon, ctypes.byref(mi)):
            r = mi.rcWork
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return (0, 0, _user.GetSystemMetrics(0), _user.GetSystemMetrics(1))


# ---------------------------------------------------------------- COM
def _co_init():
    """COM sul thread corrente. S_FALSE e RPC_E_CHANGED_MODE vanno bene lo stesso."""
    try:
        hr = ctypes.windll.ole32.CoInitializeEx(None, 2)   # APARTMENTTHREADED
        return hr in (0, 1, -2147417850)
    except Exception:
        return False


# ---------------------------------------------------------------- rung 1: UI Automation
_uia = {"mod": None, "obj": None, "tried": False, "err": None}


def _uia_ready():
    """Crea (una volta sola) il wrapper del typelib e l'oggetto CUIAutomation.

    La generazione del wrapper e' lenta (secondi) la prima volta: si chiama
    da `prewarm()` all'avvio, mai dentro il budget di una dettatura.
    """
    if _uia["obj"] is not None:
        return True
    if _uia["tried"] and _uia["obj"] is None:
        return False
    _uia["tried"] = True
    try:
        _co_init()
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        _uia["mod"] = U
        _uia["obj"] = comtypes.client.CreateObject(
            U.CUIAutomation, interface=U.IUIAutomation)
        return True
    except Exception as e:
        _uia["err"] = e
        log(f"   [caret] UIA non disponibile: {e}")
        return False


def _rects_of(rng):
    """GetBoundingRectangles -> lista di (l, t, w, h). Il SAFEARRAY e' piatto a 4 a 4."""
    try:
        raw = rng.GetBoundingRectangles()
    except Exception:
        return []
    try:
        vals = [float(v) for v in raw]
    except Exception:
        return []
    return [tuple(vals[i:i + 4]) for i in range(0, len(vals) - 3, 4)]


def _try_uia(deadline, hwnd=None):
    if time.perf_counter() > deadline or not _uia_ready():
        return None
    # GetFocusedElement e' globale: se la finestra bersaglio non e' quella col focus,
    # risponderebbe con il caret di un'ALTRA app. Meglio saltare il gradino.
    if hwnd:
        try:
            if _user.GetForegroundWindow() != hwnd:
                return None
        except Exception:
            pass
    U = _uia["mod"]
    try:
        el = _uia["obj"].GetFocusedElement()
    except Exception as e:
        log(f"   [caret] UIA focus: {e}")
        return None
    if not el:
        return None

    # 1a. TextPattern2.GetCaretRange: il caret vero, anche a meta' riga.
    try:
        pat = el.GetCurrentPattern(U.UIA_TextPattern2Id)
        if pat:
            tp2 = pat.QueryInterface(U.IUIAutomationTextPattern2)
            res = tp2.GetCaretRange()
            rng = res[-1] if isinstance(res, tuple) else res
            if rng is not None:
                rects = _rects_of(rng)
                if not rects:                       # caret degenere: allarghiamo di un carattere
                    try:
                        c = rng.Clone()
                        c.ExpandToEnclosingUnit(U.TextUnit_Character)
                        rects = _rects_of(c)
                    except Exception:
                        pass
                if rects:
                    l, t, w, h = rects[0]
                    if h > 0:
                        return ("uia-textpattern2", (int(l), int(t), max(1, int(w)), int(h)))
    except Exception as e:
        log(f"   [caret] UIA TextPattern2: {e}")

    # 1b. TextPattern.GetSelection: il punto di inserimento e' una selezione vuota.
    try:
        pat = el.GetCurrentPattern(U.UIA_TextPatternId)
        if pat:
            tp = pat.QueryInterface(U.IUIAutomationTextPattern)
            sel = tp.GetSelection()
            if sel and sel.Length > 0:
                rng = sel.GetElement(0)
                rects = _rects_of(rng)
                if not rects:
                    try:
                        c = rng.Clone()
                        c.ExpandToEnclosingUnit(U.TextUnit_Character)
                        rects = _rects_of(c)
                    except Exception:
                        pass
                if rects:
                    l, t, w, h = rects[-1]
                    if h > 0:
                        return ("uia-selection", (int(l), int(t), max(1, int(w)), int(h)))
    except Exception as e:
        log(f"   [caret] UIA TextPattern: {e}")
    return None


# ---------------------------------------------------------------- rung 2: MSAA OBJID_CARET
_msaa = {"iface": None, "tried": False}
OBJID_CARET = 0xFFFFFFF8


def _msaa_ready():
    if _msaa["iface"] is not None:
        return True
    if _msaa["tried"]:
        return False
    _msaa["tried"] = True
    try:
        _co_init()
        import comtypes.client
        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen.Accessibility import IAccessible
        _msaa["iface"] = IAccessible
        return True
    except Exception as e:
        log(f"   [caret] MSAA non disponibile: {e}")
        return False


def _try_msaa(hwnd, deadline):
    if time.perf_counter() > deadline or not hwnd or not _msaa_ready():
        return None
    try:
        import comtypes
        IAccessible = _msaa["iface"]
        ptr = ctypes.POINTER(IAccessible)()
        iid = IAccessible._iid_
        hr = ctypes.oledll.oleacc.AccessibleObjectFromWindow(
            wintypes.HWND(hwnd), ctypes.c_ulong(OBJID_CARET),
            ctypes.byref(comtypes.GUID(str(iid))), ctypes.byref(ptr))
        if hr != 0 or not ptr:
            return None
        l, t, w, h = ptr.accLocation(0)
        if h > 0:
            return ("msaa-caret", (int(l), int(t), max(1, int(w)), int(h)))
    except Exception as e:
        log(f"   [caret] MSAA: {e}")
    return None


# ---------------------------------------------------------------- rung 3: GetGUIThreadInfo
class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def _try_guithread(hwnd, deadline):
    if time.perf_counter() > deadline:
        return None
    try:
        tid = _user.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not _user.GetGUIThreadInfo(tid, ctypes.byref(gti)):
            return None
        r = gti.rcCaret
        w, h = r.right - r.left, r.bottom - r.top
        if h <= 0 or not gti.hwndCaret:
            return None
        pt = wintypes.POINT(r.left, r.top)
        _user.ClientToScreen(gti.hwndCaret, ctypes.byref(pt))
        return ("guithreadinfo", (int(pt.x), int(pt.y), max(1, int(w)), int(h)))
    except Exception as e:
        log(f"   [caret] GetGUIThreadInfo: {e}")
    return None


# ---------------------------------------------------------------- API
def prewarm():
    """Paga in anticipo i costi lenti (typelib COM). Da chiamare all'avvio dell'app."""
    t0 = time.perf_counter()
    ok_uia = _uia_ready()
    ok_msaa = _msaa_ready()
    log(f"   [caret] prewarm uia={ok_uia} msaa={ok_msaa} in {(time.perf_counter()-t0)*1000:.0f} ms")
    return ok_uia or ok_msaa


def get_caret_rect(hwnd=None, budget=BUDGET_S):
    """(x, y, w, h) del caret in pixel schermo, oppure None.

    hwnd: finestra di destinazione; se None usa quella in primo piano.
    Non solleva mai. Non blocca mai piu' del budget (salvo una singola chiamata COM lenta).
    """
    with dpi_scope():
        return _get_caret_rect_inner(hwnd, budget)


def _get_caret_rect_inner(hwnd, budget):
    t0 = time.perf_counter()
    deadline = t0 + budget
    try:
        if not hwnd:
            hwnd = _user.GetForegroundWindow()
    except Exception:
        hwnd = None
    for fn in (lambda: _try_uia(deadline, hwnd),
               lambda: _try_msaa(hwnd, deadline),
               lambda: _try_guithread(hwnd, deadline)):
        try:
            got = fn()
        except Exception as e:
            log(f"   [caret] gradino fallito: {e}")
            got = None
        if got:
            rung, rect = got
            log(f"   [caret] {rung} -> {rect} in {(time.perf_counter()-t0)*1000:.0f} ms")
            return rect
    log(f"   [caret] nessun gradino ha risposto ({(time.perf_counter()-t0)*1000:.0f} ms)")
    return None


def get_caret_rect_verbose(hwnd=None, budget=BUDGET_S):
    """Come get_caret_rect ma dice anche QUALE gradino ha risposto e quanto ha messo.
    Serve alla sonda di prova: (rect|None, nome_gradino|None, ms)."""
    with dpi_scope():
        t0 = time.perf_counter()
        deadline = t0 + budget
        try:
            if not hwnd:
                hwnd = _user.GetForegroundWindow()
        except Exception:
            hwnd = None
        for fn in (lambda: _try_uia(deadline, hwnd),
                   lambda: _try_msaa(hwnd, deadline),
                   lambda: _try_guithread(hwnd, deadline)):
            try:
                got = fn()
            except Exception:
                got = None
            if got:
                return got[1], got[0], (time.perf_counter() - t0) * 1000
        return None, None, (time.perf_counter() - t0) * 1000
