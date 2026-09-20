"""
Contratto della card live con wavetype.py (19/09): stili, fasi, livello, anteprima.
Niente schermo: orologio finto e render_frame(). La verifica visiva e' panel_styles_demo.py.

Uso: .venv/Scripts/python.exe tests/test_panel_styles.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import live_panel as LP                                     # noqa: E402


class Clock:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


def panel(style="signal"):
    c = Clock()
    p = LP.LivePanel(log=lambda m: None, clock=c, scale=1.5, style=style)
    p.anchor_override = (500, 900, 30)
    p.work_override = (0, 0, 1920, 1080)
    return p, c


def run(p, c, secs, dt=1 / 30):
    for _ in range(int(secs / dt)):
        c.t += dt
        p.render_frame()


def test_load_save_style():
    old = LP.STYLE_FILE
    with tempfile.TemporaryDirectory() as d:
        LP.STYLE_FILE = os.path.join(d, "card_style.txt")
        try:
            assert LP.load_style() == "signal"              # file assente -> default
            LP.save_style("glyph")
            assert LP.load_style() == "glyph"
            with open(LP.STYLE_FILE, "w") as f:
                f.write("boh\n")
            assert LP.load_style() == "signal"              # sconosciuto -> default
            try:
                LP.save_style("boh")
                assert False, "save_style accetta uno stile sconosciuto"
            except ValueError:
                pass
        finally:
            LP.STYLE_FILE = old
    assert LP.STYLES == ("stamp", "glyph", "signal")


def test_fasi_e_hud():
    p, c = panel()
    p.open()
    assert p.mode == "listening" and p.holds_hud()
    run(p, c, 0.3)
    p.update({"committed": "", "tentative": "Ciao", "rev": 1})
    assert p.mode == "live"                                 # prima parola -> live
    for ph in ("paused", "live", "offline", "formatting"):
        p.set_phase(ph)
        run(p, c, 0.2)
        assert p.mode == ph and p.holds_hud()
    p.set_phase("boh")                                      # ignorata, niente eccezione
    assert p.mode == "formatting"
    p.set_phase("inserted")
    p.close(pasted=True)                                    # non taglia la pillola
    run(p, c, 0.5)
    assert p.is_open() and p.holds_hud()
    run(p, c, 0.8)
    assert not p.is_open() and not p.holds_hud()            # si chiude da sola (~0,9 s + uscita)
    p.destroy()


def test_annullato_si_chiude_da_solo():
    p, c = panel("stamp")
    p.open()
    p.update({"committed": "Ciao Marco", "tentative": "", "rev": 1})
    run(p, c, 0.4)
    p.set_phase("cancelled")
    p.close(pasted=False)
    run(p, c, 1.0)
    assert p.is_open()
    run(p, c, 0.6)
    assert not p.is_open()
    p.destroy()


def test_close_prima_poi_cancelled():
    """ESC: se close() arriva prima di set_phase('cancelled'), la card annullata si vede lo stesso."""
    p, c = panel("glyph")
    p.open()
    p.update({"committed": "Ciao", "tentative": "", "rev": 1})
    run(p, c, 0.3)
    p.close(pasted=False)
    p.set_phase("cancelled")
    run(p, c, 0.5)
    assert p.is_open() and p.mode == "cancelled"
    p.destroy()


def test_cambio_stile_a_card_aperta():
    p, c = panel("stamp")
    p.open()
    p.update({"committed": "Ciao Marco, ti scrivo", "tentative": "per", "rev": 1})
    run(p, c, 0.3)
    for st in ("glyph", "signal", "stamp"):
        p.set_style(st)
        run(p, c, 0.2)
        assert p.style_name == st and p.is_open()
    p.destroy()


def test_anteprima():
    p, c = panel("glyph")
    p.preview_style("stamp")
    assert p.style_name == "stamp" and p.preview_name == "Stamp" and p.mode == "live"
    run(p, c, 0.5)
    assert any(w.state == "gone" for w in p.words)           # la correzione barrata c'e'
    run(p, c, 1.4)
    assert not p.is_open()                                  # 1,6 s e via
    p.preview_style("signal")
    run(p, c, 0.3)
    p.open()                                                # una dettatura vera la interrompe
    assert p.preview_name == "" and p.mode == "listening"
    run(p, c, 2.0)
    assert p.is_open()                                      # e non si chiude a tempo
    p.destroy()


def test_set_level_costa_poco():
    p, c = panel()
    p.open()
    t = time.perf_counter()
    for i in range(20000):
        p.set_level((i % 100) / 100.0)
    us = (time.perf_counter() - t) / 20000 * 1e6
    assert us < 5, f"set_level {us:.2f} us"
    p.set_level("x")                                        # niente eccezione
    p.destroy()


def test_parole_fuori_latin():
    """Una parola con lettere non coperte dai font (sottoinsieme latin) usa il ripiego."""
    p, c = panel("signal")
    p.open()
    p.update({"committed": "Ciao Zażółć gęślą Ελληνικά", "tentative": "", "rev": 1})
    run(p, c, 0.4)
    fonts = {id(p._wfont(w)[0]) for w in p.words}
    assert len(fonts) == 2, "tutte le parole nello stesso font: il ripiego non scatta"
    p.destroy()


if __name__ == "__main__":
    n = ok = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            n += 1
            try:
                fn()
                ok += 1
                print(f"ok   {name}")
            except Exception as e:
                print(f"FAIL {name}: {e!r}")
    print(f"{ok}/{n} ok")
    sys.exit(0 if ok == n else 1)
