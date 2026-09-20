"""
Banco a schermo di context.py: il modulo legge davvero cio' che precede il cursore?

I test unitari (tests/test_context.py) provano la regola; qui si prova la LETTURA, che
dipende dall'app. Le finestre sono nostre: nessuna app dell'utente viene toccata e non
viene premuto nessun tasto.

Uso:
  .venv/Scripts/python.exe tests/bench/context/probe.py --win32    # campo Edit classico
  .venv/Scripts/python.exe tests/bench/context/probe.py --chrome   # Chrome (= Electron: Slack, VS Code)

Misurato il 2026-09-20 su questa macchina:
  Chrome     TextPattern, paragrafo indietro: giusto nei 5 casi; 6-10 ms, ~290 ms il primo colpo.
  Edit Win32 niente TextPattern: risponde il ripiego ValuePattern + EM_GETSEL, ~4 ms.
"""
import os
import subprocess
import sys
import threading
import time

import win32con
import win32gui

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import context as C                                              # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EM_SETSEL = 0x00B1
TESTO = "Ciao a tutti, questa e' una frase gia' scritta a meta"
CASI_WIN32 = [
    # (etichetta, testo del campo, posizione del cursore, ci si aspetta "frase nuova"?)
    ("meta' frase", TESTO, 30, False),
    ("dopo il punto", "Ho finito il lavoro. ", 21, True),
    ("campo vuoto", "", 0, True),
    ("dopo la virgola", "pane, latte,", 12, False),
]


def _finestra(campi):
    """Finestra nostra con un campo Edit; torna (hwnd finestra, hwnd campo)."""
    res = {}

    def gui():
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "WavetypeCtxProbe"
        def _quit(*a):
            win32gui.PostQuitMessage(0)
            return 0
        wc.lpfnWndProc = {win32con.WM_DESTROY: _quit, win32con.WM_CLOSE: _quit}
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass
        h = win32gui.CreateWindow("WavetypeCtxProbe", "SONDA CONTESTO (win32)",
                                  win32con.WS_OVERLAPPEDWINDOW, 200, 200, 660, 200,
                                  0, 0, 0, None)
        e = win32gui.CreateWindow("EDIT", "", win32con.WS_CHILD | win32con.WS_VISIBLE |
                                  win32con.WS_BORDER | win32con.ES_AUTOHSCROLL,
                                  20, 40, 600, 28, h, 0, 0, None)
        res["h"], res["e"] = h, e
        win32gui.ShowWindow(h, win32con.SW_SHOW)
        win32gui.SetFocus(e)
        win32gui.PumpMessages()

    threading.Thread(target=gui, daemon=True).start()
    for _ in range(40):
        time.sleep(0.1)
        if "e" in res:
            break
    return res.get("h"), res.get("e")


def win32():
    h, e = _finestra(None)
    if not h:
        print("finestra non creata"); return 1
    win32gui.SetForegroundWindow(h)
    time.sleep(0.5)
    C.before_caret()                       # primo colpo: sveglia l'albero
    bad = 0
    for label, testo, pos, attesa in CASI_WIN32:
        win32gui.SetWindowText(e, testo)
        win32gui.SendMessage(e, EM_SETSEL, pos, pos)
        time.sleep(0.25)
        t0 = time.perf_counter()
        prev = C.before_caret(h)
        ms = (time.perf_counter() - t0) * 1000
        got = C.starts_sentence(prev)
        ok = "ok  " if got == attesa else "FAIL"
        if got != attesa:
            bad += 1
        print(f"{ok} {label:16s} letto={prev!r:42s} frase-nuova={got} (atteso {attesa}) {ms:.1f} ms")
    win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
    time.sleep(0.3)
    print("FALLITI:", bad)
    return bad


def chrome():
    exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not os.path.exists(exe):
        exe = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    page = os.path.join(HERE, "probe.html")
    prof = os.path.join(HERE, "chromeprof")       # profilo usa e getta: niente dati dell'utente
    subprocess.Popen([exe, "--user-data-dir=" + prof, "--no-first-run",
                      "--no-default-browser-check", "--new-window",
                      "file:///" + page.replace("\\", "/")])
    hwnd, titolo = None, ""
    casi = ("A-meta-frase", "B-campo-vuoto", "C-dopo-punto", "D-contenteditable", "E-ce-vuoto")
    for _ in range(60):
        time.sleep(0.4)
        trovate = []

        def cb(x, _):
            t = win32gui.GetWindowText(x)
            if win32gui.IsWindowVisible(x) and t.split(" - ")[0] in casi:
                trovate.append(x)
            return True
        win32gui.EnumWindows(cb, None)
        if trovate:
            hwnd = trovate[0]; break
    if not hwnd:
        print("finestra Chrome non trovata (pagina probe.html)"); return 1
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(1.0)
    C.before_caret()
    attese = {"A-meta-frase": False, "B-campo-vuoto": True, "C-dopo-punto": True,
              "D-contenteditable": False, "E-ce-vuoto": True}
    visti, bad = {}, 0
    fine = time.time() + 26
    while time.time() < fine and len(visti) < len(attese):
        caso = win32gui.GetWindowText(hwnd).split(" - ")[0]
        if caso in attese and caso not in visti:
            t0 = time.perf_counter()
            prev = C.before_caret(hwnd)
            ms = (time.perf_counter() - t0) * 1000
            got = C.starts_sentence(prev)
            visti[caso] = True
            ok = "ok  " if got == attese[caso] else "FAIL"
            if got != attese[caso]:
                bad += 1
            print(f"{ok} {caso:18s} letto={prev!r:46s} frase-nuova={got} "
                  f"(atteso {attese[caso]}) {ms:.1f} ms")
        time.sleep(0.3)
    for c in attese:
        if c not in visti:
            print(f"non visto: {c}"); bad += 1
    print("FALLITI:", bad, " (la finestra di Chrome resta aperta: chiudila tu)")
    return bad


if __name__ == "__main__":
    if "--chrome" in sys.argv:
        sys.exit(1 if chrome() else 0)
    sys.exit(1 if win32() else 0)
