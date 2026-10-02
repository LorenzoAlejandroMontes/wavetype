"""
mac_geom.py — conti di coordinate per macOS, senza PyObjC (si prova su qualunque sistema).

Il resto di Wavetype ragiona come su Windows: pixel fisici, origine in alto a sinistra.
Su Mac il contratto del layer di piattaforma e':
  pixel = punti * backingScaleFactor dello schermo che contiene il punto,
  origine in alto a sinistra dello schermo PRINCIPALE (come restituisce l'accessibilita', AX).
Cocoa invece mette l'origine in basso a sinistra dello schermo principale, con la y che sale:
NSScreen.frame e la frame di una NSWindow sono in quel sistema. Qui stanno le conversioni.

Uno "schermo" e' una tupla (frame, visible, scale) presa da NSScreen:
  frame   = (x, y, w, h) in punti, origine in basso a sinistra (NSScreen.frame)
  visible = (x, y, w, h) idem, senza menu bar e Dock (NSScreen.visibleFrame)
  scale   = backingScaleFactor (1.0 o 2.0)
Il primo della lista e' lo schermo principale (NSScreen.screens()[0], origine 0,0).

Schermi misti (uno Retina e uno no) fanno sovrapporre i rettangoli in pixel: un pixel puo'
cadere in due schermi. Si risolve con un "suggerimento" (lo schermo dell'ultimo punto
convertito, cioe' quello del caret): vedi screen_for_px.
"""


def main_height(screens):
    """Altezza in punti dello schermo principale: serve a ribaltare la y."""
    if not screens:
        return 0.0
    return float(screens[0][0][3])


def rect_tl(rect_bl, h_main):
    """(x, y, w, h) con origine in basso a sinistra -> (x, top, w, h) con origine in alto."""
    x, y, w, h = rect_bl
    return (float(x), float(h_main - (y + h)), float(w), float(h))


def rect_bl(rect_tl_, h_main):
    """L'inverso di rect_tl: da origine in alto a origine in basso (per NSWindow)."""
    x, t, w, h = rect_tl_
    return (float(x), float(h_main - (t + h)), float(w), float(h))


def _contains(r, x, y):
    rx, ry, rw, rh = r
    return rx <= x < rx + rw and ry <= y < ry + rh


def _dist2(r, x, y):
    rx, ry, rw, rh = r
    dx = max(rx - x, 0.0, x - (rx + rw))
    dy = max(ry - y, 0.0, y - (ry + rh))
    return dx * dx + dy * dy


def screen_for_pt(x, y, screens):
    """Indice dello schermo che contiene il punto (punti, origine in alto); se nessuno lo
    contiene (punto fuori da tutti), il piu' vicino. None senza schermi."""
    if not screens:
        return None
    hm = main_height(screens)
    tls = [rect_tl(s[0], hm) for s in screens]
    for i, r in enumerate(tls):
        if _contains(r, x, y):
            return i
    return min(range(len(tls)), key=lambda i: _dist2(tls[i], x, y))


def screen_for_px(x, y, screens, hint=None):
    """Indice dello schermo a cui appartiene un punto in pixel (contratto Wavetype).
    Un pixel appartiene allo schermo i se (x, y) / scale_i cade nella sua frame. Con schermi
    misti piu' di uno puo' rispondere: vince `hint` se e' fra quelli, poi il primo."""
    if not screens:
        return None
    hm = main_height(screens)
    hits = []
    for i, s in enumerate(screens):
        sc = float(s[2]) or 1.0
        if _contains(rect_tl(s[0], hm), x / sc, y / sc):
            hits.append(i)
    if hits:
        return hint if hint in hits else hits[0]
    if hint is not None and 0 <= hint < len(screens):
        return hint
    return min(range(len(screens)),
               key=lambda i: _dist2(rect_tl(screens[i][0], hm), x / (float(screens[i][2]) or 1.0),
                                    y / (float(screens[i][2]) or 1.0)))


def pt_to_px(x, y, screens):
    """Punto in punti (origine in alto, come AX) -> (x_px, y_px, indice schermo)."""
    i = screen_for_pt(x, y, screens)
    if i is None:
        return float(x), float(y), None
    sc = float(screens[i][2]) or 1.0
    return float(x) * sc, float(y) * sc, i


def rect_pt_to_px(rect, screens):
    """Rettangolo AX (x, y, w, h) in punti -> (x, y, w, h) in pixel, con la scala dello schermo
    che contiene il suo angolo in alto a sinistra. Torna anche l'indice dello schermo."""
    x, y, w, h = rect
    px, py, i = pt_to_px(x, y, screens)
    sc = float(screens[i][2]) if i is not None else 1.0
    return (px, py, float(w) * sc, float(h) * sc), i


def px_to_pt(x, y, screens, hint=None):
    """Pixel (contratto Wavetype) -> punti con origine in alto. Torna anche lo schermo."""
    i = screen_for_px(x, y, screens, hint)
    if i is None:
        return float(x), float(y), None
    sc = float(screens[i][2]) or 1.0
    return float(x) / sc, float(y) / sc, i


def work_area_px(x, y, screens, hint=None):
    """(left, top, right, bottom) in pixel della visibleFrame dello schermo che contiene il
    punto in pixel: lo stesso contratto di caret.work_area su Windows."""
    i = screen_for_px(x, y, screens, hint)
    if i is None:
        return (0, 0, 1920, 1080)
    s = screens[i]
    sc = float(s[2]) or 1.0
    vx, vt, vw, vh = rect_tl(s[1], main_height(screens))
    return (int(round(vx * sc)), int(round(vt * sc)),
            int(round((vx + vw) * sc)), int(round((vt + vh) * sc)))


def dpi_px(x, y, screens, hint=None):
    """96 * backingScaleFactor: cosi' la scala della card (dpi / 96) funziona come su Windows."""
    i = screen_for_px(x, y, screens, hint)
    if i is None:
        return 96
    return int(round(96 * (float(screens[i][2]) or 1.0)))


def window_frame(x, y, w, h, screens, idx):
    """Posizione e misura di una finestra in pixel (origine in alto) -> frame di NSWindow in
    punti (origine in basso), con la scala dello schermo `idx` (quello su cui e' stata
    pianificata la card: misura e posizione vanno divise per la STESSA scala)."""
    sc = float(screens[idx][2]) if (screens and idx is not None) else 1.0
    sc = sc or 1.0
    return rect_bl((x / sc, y / sc, w / sc, h / sc), main_height(screens))
