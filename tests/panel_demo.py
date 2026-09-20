"""
tests/panel_demo.py — banco di prova del pannello live.

Due modi:
  offline   python tests/panel_demo.py            -> niente finestre: rende i fotogrammi
                                                     chiave in PNG + un provino, e misura
                                                     quanto costa un frame.
  a schermo python tests/panel_demo.py --onscreen -> apre la finestra vera accanto al
                                                     cursore e la fotografa con ImageGrab,
                                                     su sfondo chiaro (Blocco note) e scuro.

Il copione e' una dettatura italiana vera: intercalari tolti ("allora", "ehm"), una
autocorrezione parlata ("alle 5, anzi no, alle 6"), un ripensamento del ricognitore
("cliente nuovo" -> "cliente Rossi") e un traboccamento oltre le 3 righe.
"""
import argparse
import ctypes
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# La sonda dev'essere consapevole del DPI: altrimenti ImageGrab fotografa uno schermo
# rimpicciolito da Windows e non si giudica la nitidezza vera.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

from PIL import Image, ImageDraw, ImageFont, ImageGrab   # noqa: E402

import live_panel                                        # noqa: E402
from live_panel import LivePanel                         # noqa: E402

OUT = os.path.join("tests", "panel_out")
os.makedirs(OUT, exist_ok=True)

C1 = "Ci vediamo domani alle 6 davanti al bar di piazza Garibaldi,"
C2 = C1 + " così finiamo il preventivo per il cliente Rossi"
C3 = C2 + " e poi mando il riepilogo a Martina, perché lo aspetta da ieri"

# (secondi da tenere, committed, tentative, nome del fotogramma chiave o None,
#  quando fotografare in secondi dall'aggiornamento — serve per cogliere il barrato)
SCRIPT = [
    (0.40, "", "allora", "01_prima-parola", None),
    (0.35, "", "allora ci vediamo", None, None),
    (0.40, "", "allora ci vediamo domani", "02_coda-incerta", None),
    (1.00, "Ci vediamo domani", "alle", "03_intercalare-barrato", 0.22),
    (0.35, "Ci vediamo domani", "alle 5,", None, None),
    (0.40, "Ci vediamo domani", "alle 5, ehm", "04_ehm", None),
    (0.50, "Ci vediamo domani", "alle 5, ehm anzi no, alle 6", "05_autocorrezione", None),
    (1.10, "Ci vediamo domani alle 6", "", "06_barrato-tenuto", 0.45),
    (0.40, "Ci vediamo domani alle 6", "davanti al bar", "07_richiuso", None),
    (0.45, "Ci vediamo domani alle 6", "davanti al bar di piazza Garibaldi", "08_due-righe", None),
    (0.50, C1, "così finiamo il preventivo", "09_tre-righe", None),
    (0.45, C1, "così finiamo il preventivo per il cliente nuovo", None, None),
    (1.00, C1, "così finiamo il preventivo per il cliente Rossi", "10_cambio-idea", 0.20),
    (0.50, C2, "e poi mando", None, None),
    (0.60, C2, "e poi mando il riepilogo a Martina, perché lo aspetta", None, None),
    (0.80, C3, "ma io gli avevo già scritto lunedì", "11_scorrimento", None),
]

# scena a parte: accenti + una parola piu' lunga della riga (caso patologico: un indirizzo
# dettato). Non deve mai uscire dalla card: viene spezzata, ma resta UNA parola per il diff.
SCRIPT_LONG = [
    (0.40, "", "apri", None, None),
    (0.50, "", "apri la controindicazione dell'internazionalizzazione è però", "12_accenti", None),
    (0.80, "", "apri https://www.comune.example.it/servizi/anagrafe/"
               "certificati-di-residenza-online e poi", "13_parola-lunga", None),
]

FPS = 40.0
DT = 1.0 / FPS


class FakeClock:
    """Tempo finto: i PNG devono venire uguali a ogni giro."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _bg(w, h, dark):
    """Sfondo finto tipo editor: il pannello deve leggersi su chiaro E su scuro."""
    base = (16, 18, 24) if dark else (247, 248, 250)
    ink = (120, 128, 145) if dark else (176, 182, 196)
    img = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 20)
    except Exception:
        f = ImageFont.load_default()
    for i in range(h // 34 + 1):
        d.text((26, 10 + i * 34), "Lorem ipsum dolor sit amet, consectetur adipiscing.",
               font=f, fill=ink)
    return img


def on_bgs(frame):
    """Il fotogramma su sfondo chiaro a sinistra e scuro a destra, in un solo PNG."""
    w, h = frame.size
    out = Image.new("RGB", (w * 2 + 12, h), (60, 60, 60))
    for i, dark in enumerate((False, True)):
        bg = _bg(w, h, dark).convert("RGBA")
        bg.alpha_composite(frame)
        out.paste(bg.convert("RGB"), (i * (w + 12), 0))
    return out


def run_offline(tag=""):
    clock = FakeClock()
    p = LivePanel(log=lambda m: None, clock=clock, scale=1.5)   # 150%: come lo schermo di qui
    p.open()
    keys, costs = [], []
    rev = 0
    for script in (SCRIPT, SCRIPT_LONG):
        if script is SCRIPT_LONG:                  # seconda scena: si riparte da zero
            p.close(pasted=False)
            for _ in range(int(0.30 / DT)):
                clock.advance(DT)
                p.render_frame()
            p.open()
        for hold, com, ten, name, at in script:
            rev += 1
            p.update({"committed": com, "tentative": ten, "rev": rev})
            n = max(1, int(hold / DT))
            shot_at = n - 1 if at is None else min(n - 1, max(0, int(at / DT)))
            for i in range(n):
                clock.advance(DT)
                t0 = time.perf_counter()
                img = p.render_frame()
                costs.append((time.perf_counter() - t0) * 1000)
                if name and i == shot_at:
                    keys.append((name, img.copy()))
    # uscita: il testo collassa verso il cursore
    p.close(pasted=True)
    for i in range(int(0.24 / DT)):
        clock.advance(DT)
        t0 = time.perf_counter()
        img = p.render_frame()
        costs.append((time.perf_counter() - t0) * 1000)
        if i in (2, 5):
            keys.append((f"14_chiusura-{i}", img.copy()))

    for name, img in keys:
        on_bgs(img).save(os.path.join(OUT, f"{tag}{name}.png"))
    sheet(keys, os.path.join(OUT, f"{tag}00_provino.png"))
    costs.sort()
    n = len(costs)
    print(f"fotogrammi resi: {n} | frame medio {sum(costs)/n:.2f} ms | "
          f"p95 {costs[int(n*0.95)]:.2f} ms | max {costs[-1]:.2f} ms")
    print("PNG in", os.path.abspath(OUT))
    return costs


def sheet(keys, path, cols=3):
    """Provino: tutti i fotogrammi chiave su sfondo scuro, con l'etichetta."""
    if not keys:
        return
    cw = max(i.width for _, i in keys)
    chh = max(i.height for _, i in keys) + 26
    rows = (len(keys) + cols - 1) // cols
    out = Image.new("RGB", (cols * cw, rows * chh), (24, 25, 30))
    d = ImageDraw.Draw(out)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 15)
    except Exception:
        f = ImageFont.load_default()
    for k, (name, img) in enumerate(keys):
        x, y = (k % cols) * cw, (k // cols) * chh
        cell = Image.new("RGBA", (cw, chh - 26), (38, 40, 48, 255))
        cell.alpha_composite(img, ((cw - img.width) // 2, 0))
        out.paste(cell.convert("RGB"), (x, y))
        d.text((x + 10, y + chh - 22), name, font=f, fill=(190, 195, 205))
    out.save(path)


# ------------------------------------------------------------------ a schermo
_user = ctypes.windll.user32


def _backdrop(dark=True):
    """Fondale scuro piccolo, in basso al centro (dove il pannello va senza caret).
    NON a tutto schermo: deve dare il minimo fastidio."""
    import win32gui
    import win32con
    import win32api
    hinst = win32api.GetModuleHandle(None)
    cls = "WavetypePanelBackdrop"
    try:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = cls
        wc.hInstance = hinst
        wc.lpfnWndProc = lambda h, m, w, l: win32gui.DefWindowProc(h, m, w, l)
        wc.hbrBackground = win32gui.CreateSolidBrush(win32api.RGB(16, 18, 24) if dark
                                                     else win32api.RGB(247, 248, 250))
        win32gui.RegisterClass(wc)
    except Exception:
        pass
    wl, wt, wr, wb = live_panel.caret_mod.work_area(10, 10)
    bw, bh = 1100, 420
    h = win32gui.CreateWindowEx(win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
                                cls, "backdrop", win32con.WS_POPUP,
                                (wl + wr) // 2 - bw // 2, wb - bh, bw, bh, 0, 0, hinst, None)
    win32gui.ShowWindow(h, win32con.SW_SHOWNOACTIVATE)
    return h


def play(p, grabs, label, keep_front=None):
    """Gioca il copione col tempo vero e fotografa lo schermo nei momenti indicati."""
    import win32gui
    rev = 0
    shots = []
    ticks = []
    t_start = time.perf_counter()
    for hold, com, ten, name, _at in SCRIPT:
        rev += 1
        p.update({"committed": com, "tentative": ten, "rev": rev})
        t_end = time.perf_counter() + hold * 0.55       # ~8 s in tutto
        if keep_front:
            try:    # SetForegroundWindow da un processo non attivo spesso non attecchisce:
                    # alziamo la finestra con SetWindowPos, che non richiede l'attivazione
                _user.SetWindowPos(keep_front, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                _user.SetForegroundWindow(keep_front)
            except Exception:
                pass
        while time.perf_counter() < t_end:
            win32gui.PumpWaitingMessages()
            p.tick()
            ticks.append(p.last_frame_ms)
            time.sleep(DT)
        if name in grabs:
            time.sleep(0.05)
            r = win32gui.GetWindowRect(p.hwnd)      # la finestra include gia' il margine ombra
            box = (max(0, r[0] - 10), max(0, r[1] - 10), r[2] + 10, r[3] + 10)
            shot = ImageGrab.grab(box, all_screens=True)
            path = os.path.join(OUT, f"live_{label}_{name}.png")
            shot.save(path)
            shots.append(path)
            print("  foto", path, shot.size, flush=True)
    p.close(pasted=True)
    for _ in range(int(0.4 / DT)):
        win32gui.PumpWaitingMessages()
        p.tick()
        time.sleep(DT)
    ticks.sort()
    n = max(1, len(ticks))
    print(f"  durata {time.perf_counter()-t_start:.1f} s | tick(): medio {sum(ticks)/n:.2f} ms, "
          f"p95 {ticks[int(n*0.95)-1]:.2f} ms, max {ticks[-1]:.2f} ms su {n} frame", flush=True)
    return shots


def run_onscreen():
    import win32gui
    grabs = {"05_autocorrezione", "06_barrato-tenuto", "09_tre-righe", "11_scorrimento"}

    # --- 1. sfondo CHIARO: Blocco note (una istanza mia, la chiudo alla fine) ---
    before = set()

    def tops():
        out = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, _):
            if _user.IsWindowVisible(h):
                b = ctypes.create_unicode_buffer(256)
                _user.GetClassNameW(h, b, 256)
                out.append((h, b.value))
            return True
        _user.EnumWindows(cb, 0)
        return out

    before = {h for h, _ in tops()}
    np_proc = subprocess.Popen(["notepad.exe"])
    hwnd_np = None
    t0 = time.time()
    while time.time() - t0 < 10 and not hwnd_np:
        for h, cls in tops():
            if h not in before and "Notepad" in cls:
                hwnd_np = h
        time.sleep(0.3)
    if hwnd_np:
        time.sleep(1.2)
        _user.SetForegroundWindow(hwnd_np)
        time.sleep(0.8)
        p = LivePanel(log=print)
        p.open(hwnd_np)
        print("[blocconote] pannello aperto sul caret vero di Blocco note", flush=True)
        play(p, grabs, "blocconote", keep_front=hwnd_np)
        p.destroy()
        win32gui.PostMessage(hwnd_np, 0x0010, 0, 0)
        time.sleep(0.8)
        try:
            np_proc.kill()
        except Exception:
            pass
    else:
        print("[blocconote] Blocco note non trovato", flush=True)

    # --- 2 e 3. fondale mio (chiaro e scuro): sfondo certo, nessuna app dell'utente toccata ---
    for dark, label in ((False, "chiaro"), (True, "scuro")):
        bd = _backdrop(dark=dark)
        time.sleep(0.5)
        p = LivePanel(log=print)
        p.open(bd)                   # nessun caret qui: si prova anche il gradino 4
        print(f"[{label}] pannello aperto su fondale", flush=True)
        play(p, grabs, label)
        p.destroy()
        win32gui.DestroyWindow(bd)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--onscreen", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.onscreen:
        run_onscreen()
    else:
        run_offline(a.tag)
