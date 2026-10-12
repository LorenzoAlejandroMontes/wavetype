"""
Prova VERA dell'incolla: appunti di Windows veri, Ctrl+V vero, in una finestrella nostra.

Apre una finestra con una casella di testo, ci incolla con wavetype.insert_text e rilegge cosa e'
arrivato. La finestra legge gli appunti IN RITARDO (come un'app col PC carico): col codice di prima
del 12/10 riceveva quello che c'era prima negli appunti, non il dettato.
Tocca gli appunti per qualche secondo e li rimette com'erano. Dura ~8 s.

Uso:
  .venv/Scripts/python.exe tests/clip_smoke.py
"""
import os
import sys
import time
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
import wavetype                                               # noqa: E402

LOGS = []
wavetype.log = lambda m: LOGS.append(str(m))
wavetype.beep = lambda *a, **k: None
SLOW = 0.9                    # la finestra esegue l'incolla dopo tanto: piu' dei 0,2 s di una volta


def main():
    before = wavetype.get_clipboard_text()
    wavetype.set_clipboard_text("APPUNTI DI PRIMA")
    root = tk.Tk()
    root.title("Wavetype clip smoke")
    root.geometry("420x120+60+60")
    root.attributes("-topmost", True)
    box = tk.Text(root, height=4)
    box.pack(fill="both", expand=True)
    got = []

    def slow_paste(_e):
        root.after(int(SLOW * 1000), lambda: got.append(root.clipboard_get()))
        return "break"
    box.bind("<Control-v>", slow_paste)
    root.update()
    root.focus_force()
    box.focus_set()
    root.update()
    hwnd = wavetype.win32gui.GetParent(root.winfo_id()) or root.winfo_id()

    def pump(sec):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            root.update()
            wavetype.clip_restore()          # come il loop principale dell'app
            time.sleep(0.02)

    t0 = time.perf_counter()
    ok = wavetype.insert_text(hwnd, "DETTATO DI PROVA àèì")
    took = time.perf_counter() - t0
    pump(SLOW + 0.4)
    mid = wavetype.get_clipboard_text()
    pump(wavetype.PASTE_RESTORE_SEC)
    after = wavetype.get_clipboard_text()
    root.destroy()
    if before is not None:
        wavetype.set_clipboard_text(before)
    print(f"insert_text -> {ok} in {took:.2f}s")
    print(f"la finestra lenta ({SLOW}s) ha letto: {got}")
    print(f"appunti dopo {SLOW + 0.4:.1f}s: {mid!r}")
    print(f"appunti dopo {wavetype.PASTE_RESTORE_SEC + SLOW + 0.4:.1f}s: {after!r}")
    for m in LOGS:
        print("log:", m)
    good = ok and got == ["DETTATO DI PROVA àèì"] and after == "APPUNTI DI PRIMA"
    print("OK" if good else "FALLITO")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
