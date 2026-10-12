"""
Banco dell'incolla e della copia della selezione, senza toccare gli appunti veri ne' la tastiera.

Gli appunti di Windows, la tastiera e la finestra davanti sono sostituiti da un mondo finto con
un'app LENTA: esegue Ctrl+V e Ctrl+C dopo un ritardo scelto dal test, come fa un'app vera quando
il PC e' carico. Si guida il vero codice di wavetype.py: insert_text, clip_restore, copy_selection.

Il difetto del 12/10: gli appunti di prima tornavano 0,2 s dopo Ctrl+V; un'app che leggeva gli
appunti piu' tardi incollava quello che c'era prima, non il dettato. `old_insert_text` qui sotto e'
quel codice, tenuto per mostrare che il banco lo vede.

Uso:
  .venv/Scripts/python.exe tests/test_clip.py
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
import wavetype                                               # noqa: E402

LOGS = []
wavetype.log = lambda m: LOGS.append(str(m))
wavetype.beep = lambda *a, **k: None


class World:
    """Appunti finti + un'app che reagisce ai tasti dopo `delay` secondi."""
    CF_UNICODETEXT = 13

    def __init__(self, clip="PRIMA", delay=0.0, selection="", busy=0, locked=False):
        self.clip, self.seq = clip, 100
        self.delay, self.selection = delay, selection
        self.busy, self.locked = busy, locked     # busy = aperture che falliscono; locked = sempre
        self.pasted, self.sent, self.fg = [], [], 1
        self.down = lambda vk: False
        self.lock = threading.Lock()

    # --- win32clipboard
    def OpenClipboard(self):
        if self.locked or self.busy > 0:
            self.busy -= 1
            raise OSError("OpenClipboard: accesso negato")

    def CloseClipboard(self):
        pass

    def EmptyClipboard(self):
        with self.lock:
            self.clip, self.seq = None, self.seq + 1

    def SetClipboardData(self, fmt, text):
        with self.lock:
            self.clip, self.seq = text, self.seq + 1

    def GetClipboardData(self, fmt):
        if self.clip is None:
            raise TypeError("nessun testo")
        return self.clip

    def GetClipboardSequenceNumber(self):
        return self.seq

    # --- win32gui
    def SetForegroundWindow(self, hwnd):
        self.fg = hwnd

    def GetForegroundWindow(self):
        return self.fg

    # --- keyboard
    def send(self, combo):
        self.sent.append(combo)
        threading.Timer(self.delay, self._react, args=(combo,)).start()

    def _react(self, combo):
        if combo == "ctrl+v":
            self.pasted.append(self.clip)
        elif combo == "ctrl+c" and self.selection:
            self.SetClipboardData(self.CF_UNICODETEXT, self.selection)

    def user_copies(self, text):
        self.SetClipboardData(self.CF_UNICODETEXT, text)


def world(**kw):
    w = World(**kw)
    wavetype.win32clipboard = w
    wavetype.win32gui = w
    wavetype.keyboard = w
    wavetype._down = lambda vk: w.down(vk)
    wavetype.clip_hold.update(active=False, saved=None, seq=0, due=0.0, last=0.0)
    del LOGS[:]
    return w


def old_insert_text(hwnd, text):
    """Il codice di prima del 12/10, riga per riga."""
    saved = wavetype.get_clipboard_text()
    wavetype.win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    wavetype.set_clipboard_text(text)
    time.sleep(0.03)
    wavetype.keyboard.send("ctrl+v")
    time.sleep(0.20)
    if saved is not None:
        wavetype.set_clipboard_text(saved)


# ---------- incolla ----------
def test_il_codice_di_prima_incollava_gli_appunti_vecchi_con_l_app_lenta():
    w = world(delay=0.6)
    old_insert_text(7, "DETTATO")
    time.sleep(0.7)
    assert w.pasted == ["PRIMA"], w.pasted      # il difetto: incollato quello che c'era prima


def test_app_lenta_riceve_il_dettato():
    for delay in (0.0, 0.6, 2.0):
        w = world(delay=delay)
        assert wavetype.insert_text(7, "DETTATO") is True
        wavetype.clip_restore()                    # il loop principale passa subito: non e' ancora ora
        time.sleep(delay + 0.1)
        assert w.pasted == ["DETTATO"], (delay, w.pasted)
        assert w.fg == 7


def test_gli_appunti_di_prima_tornano_quando_e_ora():
    w = world()
    wavetype.insert_text(7, "DETTATO")
    wavetype.clip_restore()
    assert w.clip == "DETTATO"                  # prima del tempo restano col dettato
    wavetype.clip_hold["due"] = 0.0
    wavetype.clip_restore()
    assert w.clip == "PRIMA" and wavetype.clip_hold["active"] is False


def test_se_copi_altro_nel_frattempo_non_viene_sovrascritto():
    w = world()
    wavetype.insert_text(7, "DETTATO")
    w.user_copies("COPIATO DA ME")
    wavetype.clip_hold["due"] = 0.0
    wavetype.clip_restore()
    assert w.clip == "COPIATO DA ME"


def test_due_incolla_di_fila_e_torna_l_originale():
    w = world(delay=0.1)
    wavetype.insert_text(7, "UNO")
    wavetype.insert_text(7, "DUE")                 # aspetta PASTE_GAP: il primo ha gia' letto
    time.sleep(0.2)
    assert w.pasted == ["UNO", "DUE"], w.pasted
    wavetype.clip_hold["due"] = 0.0
    wavetype.clip_restore()
    assert w.clip == "PRIMA"                    # non "UNO": l'originale e' quello di prima di tutto


def test_appunti_occupati_si_riprova():
    w = world(busy=4)
    assert wavetype.insert_text(7, "DETTATO") is True
    time.sleep(0.05)
    assert w.pasted == ["DETTATO"]


def test_appunti_bloccati_niente_ctrl_v_alla_cieca():
    w = world(locked=True)
    assert wavetype.insert_text(7, "DETTATO") is None
    assert "ctrl+v" not in w.sent               # mai un incolla con dentro il testo di qualcun altro
    assert any("NON incollo" in m for m in LOGS)


def test_win_ancora_premuto_ctrl_v_aspetta_il_rilascio():
    w = world()
    t0 = time.perf_counter()
    w.down = lambda vk: vk == wavetype.win32con.VK_LWIN and time.perf_counter() - t0 < 0.25
    wavetype.insert_text(7, "DETTATO")
    assert time.perf_counter() - t0 >= 0.25 and w.sent == ["ctrl+v"]


# ---------- copia della selezione ----------
def test_selezione_copiata_e_appunti_rimessi():
    w = world(delay=0.02, selection="testo selezionato")
    t0 = time.perf_counter()
    assert wavetype.copy_selection() == "testo selezionato"
    assert time.perf_counter() - t0 < wavetype.COPY_WAIT      # esce appena arriva, non a tempo fisso
    assert w.clip == "PRIMA"


def test_niente_selezione_torna_vuoto():
    w = world(delay=0.0, selection="")
    assert wavetype.copy_selection() == ""
    assert w.clip == "PRIMA"


def test_selezione_subito_dopo_un_incolla_non_perde_l_originale():
    w = world(delay=0.02, selection="testo selezionato")
    wavetype.insert_text(7, "DETTATO")
    time.sleep(0.05)
    assert wavetype.copy_selection() == "testo selezionato"
    assert w.clip == "PRIMA"                    # non "DETTATO"
    wavetype.clip_hold["due"] = 0.0
    wavetype.clip_restore()
    assert w.clip == "PRIMA"


def test_copia_in_ritardo_non_sporca_gli_appunti():
    w = world(delay=0.45, selection="testo selezionato")
    assert wavetype.copy_selection() == ""         # l'app non ha fatto in tempo: parte come dettatura
    time.sleep(0.7)
    assert w.clip == "PRIMA", w.clip            # la selezione arrivata dopo non resta negli appunti
    assert any("in ritardo" in m for m in LOGS)


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    bad = 0
    for n, f in tests:
        try:
            f()
            print(f"ok   {n}")
        except Exception as ex:
            bad += 1
            print(f"FAIL {n}: {ex!r}")
    print(f"{len(tests) - bad}/{len(tests)} ok" if not bad else f"{bad} falliti")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
