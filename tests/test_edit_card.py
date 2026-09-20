"""
Contratto della card di Edit Mode (19/09): chip, riconoscimento dell'istruzione, fasi, reso.
Niente schermo: orologio finto e render_frame(). La verifica visiva e' tests/edit_card_demo.py.

Uso: .venv/Scripts/python.exe tests/test_edit_card.py
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import card_styles as cs                                       # noqa: E402
import edit_chips as ec                                        # noqa: E402
import live_panel as LP                                        # noqa: E402


class Clock:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


def panel(style="signal", scale=1.5, width=1920):
    c = Clock()
    p = LP.LivePanel(log=lambda m: None, clock=c, scale=scale, style=style)
    p.anchor_override = (620, 900, 30)
    p.work_override = (0, 0, width, 1080)
    return p, c


def run(p, c, secs, dt=1 / 30.0):
    for _ in range(max(1, int(secs / dt))):
        c.t += dt
        p.render_frame()


def bbox(im):
    """Rettangolo della card dentro il bitmap (il margine e' ombra trasparente)."""
    a = np.asarray(im.getchannel("A"))
    ys, xs = np.where(a > 200)
    return (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1) if len(xs) else (0, 0, 0, 0)


# ------------------------------------------------------------------ chip: definizioni
def test_tre_chip_tasti_1_2_3():
    assert len(ec.CHIPS) == 3
    assert [c.key for c in ec.CHIPS] == ["1", "2", "3"]
    assert [c.label for c in ec.CHIPS] == ["Grammar", "English", "Slack"]
    assert [c.n for c in ec.CHIPS] == [1, 2, 3]
    for k, cid in (("1", "grammar"), (2, "english"), ("3", "slack")):
        assert ec.by_key(k).id == cid
    assert ec.by_key("9") is None and ec.by_key("") is None


def test_testi_istruzione():
    """Ogni chip ha un'istruzione vera da passare come `instr` a EDIT_PROMPT (wavetype.py)."""
    for c in ec.CHIPS:
        assert ec.instruction(c.id) == c.instr
        assert len(c.instr) > 15 and c.instr[0].isupper()
    assert ec.instruction(None) is None                     # istruzione libera: nessun testo nostro
    g = ec.instruction("grammar").lower()
    for must in ("grammatica", "sintassi", "lingua", "elenco"):
        assert must in g, f"Grammar non dice '{must}'"
    s = ec.instruction("slack").lower()
    for must in ("slack", "emoji", "informazioni"):
        assert must in s, f"Slack non dice '{must}'"


def test_pillole_di_esito():
    assert [ec.done_text(c.id) for c in ec.CHIPS] == ["GRAMMAR FIXED", "NOW IN ENGLISH",
                                                      "READY FOR SLACK"]
    assert ec.done_text(None) == "REWRITTEN"                # istruzione libera


# ------------------------------------------------------------------ chip: riconoscimento
CHIP_YES = {
    "grammar": ["correggi la grammatica", "Correggi la grammatica.", "sistema la grammatica",
                "Sistemala grammaticalmente.", "sistema la grammatica e la sintassi",
                "sistema la grammatica di questa frase", "grammatica", "grammar",
                "fix the grammar", "proofread", "corrige la gramatica",
                "ok, correggi la grammatica per favore"],
    "english": ["in inglese", "traduci in inglese", "Traducilo in inglese.", "in English",
                "translate it to english", "en ingles", "me lo puoi fare in inglese?",
                "mettilo in inglese"],
    "slack": ["tono slack", "in tono slack", "mettilo in tono slack", "per slack",
              "slack", "slack tone", "make it slack style"],
}

CHIP_NO = [
    "traduci in inglese e accorcialo",
    "traduci in italiano",
    "correggi la grammatica e mettilo in inglese",
    "sistemala grammaticalmente, deve dire scusa se non ti ho avvisato in tempo",
    "formatta senza asterischi, deve essere per Slack",
    "aggiusta la formattazione",
    "rendilo piu' corto e piu' diretto",
    "sistema tutto questo testo, aggiustalo",
    "accorcialo un po' e fallo in inglese naturale",
    "",
    "   ",
]


def test_riconoscimento_chip():
    for cid, phrases in CHIP_YES.items():
        for t in phrases:
            assert ec.match(t) == cid, f"{t!r} -> {ec.match(t)!r}, atteso {cid}"


def test_istruzione_con_contenuto_in_piu_resta_libera():
    for t in CHIP_NO:
        assert ec.match(t) is None, f"{t!r} acceso come {ec.match(t)!r}: falso positivo"


def test_match_non_solleva_mai():
    for bad in (None, 123, object(), "\x00\x01", "ok " * 200):
        assert ec.match(bad) is None or isinstance(ec.match(bad), str)


def test_tetto_di_parole():
    """Oltre MAX_WORDS non e' piu' "solo il comando", per quanto sembri."""
    long = "correggi la grammatica " + "e la sintassi e la forma e il resto"
    assert ec.match(long) is None


# ------------------------------------------------------------------ card: apertura e fasi
def test_apertura_edit():
    p, c = panel()
    p.open_edit(None, 26)
    assert p.edit and p.is_open() and p.holds_hud()
    assert p.mode == "e_listening" and p.sel_words == 26
    assert p.chip is None and p.instruction() == ""
    assert p.edit_sub() == "26 words"
    run(p, c, 0.3)
    p.destroy()


def test_tutte_le_fasi_rendono_in_tre_stili():
    for style in LP.STYLES:
        for ph in LP.EDIT_PHASES:
            p, c = panel(style)
            p.open_edit(None, 26)
            run(p, c, 0.2)
            p.set_chip("1")
            p.set_phase(ph)
            run(p, c, 0.3)
            im = p.render_frame()
            x0, y0, x1, y1 = bbox(im)
            assert x1 - x0 > 60 and y1 - y0 > 20, f"{style}/{ph}: card vuota {im.size}"
            assert x0 >= 0 and x1 <= im.width and y1 <= im.height
            p.destroy()


def test_fase_sconosciuta_non_solleva():
    p, c = panel("stamp")
    p.open_edit(None, 26)
    p.set_phase("boh")
    assert p.mode == "e_listening"
    p.set_phase("formatting")                  # fase della dettatura: qui non esiste
    assert p.mode == "e_listening"
    p.destroy()


def test_istruzione_accende_il_chip():
    p, c = panel("stamp")
    p.open_edit(None, 26)
    p.update({"committed": "correggi la", "tentative": "grammatica", "rev": 1})
    run(p, c, 0.2)
    assert p.chip == "grammar" and p.has_words()
    chips, lit, dim, only = p.edit_row()
    assert lit == "grammar" and not dim and len(chips) == 3
    p.update({"committed": "correggi la grammatica e", "tentative": "accorcialo", "rev": 2})
    assert p.chip is None                      # contenuto in piu': torna istruzione libera
    assert p.edit_row()[2] is True             # tutti spenti
    p.destroy()


def test_tasto_vince_sulla_voce():
    p, c = panel("glyph")
    p.open_edit(None, 26)
    assert p.set_chip("2") == "english"
    p.update({"committed": "correggi la grammatica", "tentative": "", "rev": 1})
    assert p.chip == "english", "l'istruzione detta ha sovrascritto il tasto premuto"
    assert p.set_chip("7") is None and p.chip == "english"
    p.set_chip(None)                           # torna al riconoscimento automatico
    p.update({"committed": "correggi la grammatica", "tentative": "", "rev": 2})
    assert p.chip == "grammar"
    p.destroy()


def test_pillola_nomina_il_chip():
    p, c = panel("signal")
    p.open_edit(None, 26)
    p.set_chip("3")
    p.set_result(19)
    p.set_phase("done")
    assert p.edit_done_text() == "READY FOR SLACK" and p.edit_words_text() == "19 words"
    run(p, c, 0.3)
    assert bbox(p.render_frame())[2] > 60
    p.destroy()


def test_pillola_istruzione_libera():
    p, c = panel("stamp")
    p.open_edit(None, 26)
    p.set_result(1)
    p.set_phase("done")
    assert p.edit_done_text() == "REWRITTEN" and p.edit_words_text() == "1 word"
    p.destroy()


# ------------------------------------------------------------------ card: misure
def test_riga_chip_allineata_al_testo():
    """I chip partono dalla stessa colonna dell'etichetta YOU; il testo rientra di YOU + stacco."""
    for style in LP.STYLES:
        p, _ = panel(style)
        p.open_edit(None, 26)
        st = p.style
        inset = st.edit_inset * p.scale
        you = cs.label_width(cs.E_YOU.upper(), st.edit_you_font(), st.edit_you_tr())
        assert abs(p.text_x - (inset + you + st.edit_you_gap * p.scale)) < 0.01
        assert p.text_x > inset
        p.destroy()


def test_card_stretta_tiene_il_numero_e_perde_la_parola():
    """Sotto la larghezza in cui l'etichetta piu' lunga ci sta, resta solo il numero."""
    for style in LP.STYLES:
        p, _ = panel(style)
        p.open_edit(None, 26)
        st = p.style
        wide = p.card_w_max - 2 * st.edit_inset * p.scale
        assert st.chip_labels_fit(LP.CHIPS, st.chip_seg(wide)), f"{style}: parole via a card piena"
        need = max(st.chip_w(c, True) for c in LP.CHIPS)
        seg = need - 1                                   # un pelo sotto la soglia misurata
        assert not st.chip_labels_fit(LP.CHIPS, seg), f"{style}: parole tenute oltre la misura"
        p.destroy()


def test_card_stretta_vera():
    """Con poco spazio a schermo la card si stringe e i chip restano dentro."""
    for style in LP.STYLES:
        p, c = panel(style, width=520)
        p.open_edit(None, 26)
        run(p, c, 0.4)
        im = p.render_frame()
        x0, _y0, x1, _y1 = bbox(im)
        assert 0 <= x0 and x1 <= im.width                # niente tagliato fuori dal bitmap
        assert p.card_w_max < p.style.card_w * p.scale   # si e' davvero stretta
        assert x1 - x0 <= p.card_w_max + p.margin
        p.destroy()


def test_istruzione_lunga_non_rompe_la_card():
    long = ("rendilo molto piu' corto e diretto togli i convenevoli e lascia solo i numeri "
            "e la data di scadenza e anche il nome del cliente e il riferimento dell'offerta")
    for style in LP.STYLES:
        p, c = panel(style)
        p.open_edit(None, 26)
        p.update({"committed": long, "tentative": "per favore", "rev": 1})
        run(p, c, 1.5)
        im = p.render_frame()
        x0, y0, x1, y1 = bbox(im)
        top = int(round(p._max_h() * p.scale)) + p.margin   # + ombra/anello dello stile
        assert y1 - y0 <= top, f"{style}: card alta {y1 - y0} contro un tetto di {top}"
        assert x1 - x0 <= p.card_w_max + p.margin
        assert p._n_lines >= 3 and p._scroll > 0, f"{style}: l'istruzione lunga non scorre"
        p.destroy()


def test_parola_lunghissima_non_esce():
    p, c = panel("glyph")
    p.open_edit(None, 26)
    p.update({"committed": "https://" + "x" * 300, "tentative": "", "rev": 1})
    run(p, c, 0.5)
    im = p.render_frame()
    x0, _y0, x1, _y1 = bbox(im)
    assert x1 - x0 <= p.card_w_max + p.margin
    p.destroy()


def test_due_scale():
    """Al 100% e al 150% la card resta dentro al suo bitmap in ogni fase."""
    for scale in (1.0, 1.5):
        for style in LP.STYLES:
            p, c = panel(style, scale=scale)
            p.open_edit(None, 26)
            p.update({"committed": "correggi la", "tentative": "grammatica", "rev": 1})
            for ph in LP.EDIT_PHASES:
                p.set_phase(ph)
                run(p, c, 0.3)
                im = p.render_frame()
                x0, y0, x1, y1 = bbox(im)
                assert 0 <= x0 and x1 <= im.width and 0 <= y0 and y1 <= im.height
            p.destroy()


# ------------------------------------------------------------------ la dettatura non cambia
def test_dettatura_dopo_edit():
    """Lo stesso pannello, usato prima per Edit e poi per una dettatura, torna com'era."""
    p, c = panel("stamp")
    p.open_edit(None, 26)
    p.set_chip("1")
    run(p, c, 0.3)
    p.close(pasted=False)
    run(p, c, 0.5)
    p.open()
    assert not p.edit and p.mode == "listening" and p.chip is None
    assert p._maxl == LP.MAX_LINES and p._compact()
    assert abs(p.line_h - p.style.line_h * p.scale) < 0.01
    assert abs(p.text_x - p.style.text_inset * p.scale) < 0.01
    p.update({"committed": "Ciao Marco", "tentative": "ti scrivo", "rev": 1})
    assert p.mode == "live"
    for ph in ("paused", "formatting", "inserted"):
        p.set_phase(ph)
        run(p, c, 0.2)
        assert p.mode == ph
    p.destroy()


def test_fasi_dettatura_non_toccate_da_edit():
    assert LP.PHASES == ("listening", "live", "paused", "formatting", "inserted", "cancelled",
                         "offline", "recovering", "recovered", "no_audio", "unrecovered")
    assert all(not m.startswith("e_") for m in LP.PHASES)
    assert all(m.startswith("e_") for m in cs.EDIT_MODES)


# ---------- card del recupero (Win+Ctrl+R) ----------
def test_recupero_card_compatta_con_la_durata():
    """"recovering": pillola compatta, e a destra la durata dell'audio, non un cronometro."""
    for st in LP.STYLES:
        p, c = panel(st)
        p.open_recover(None, 38.4)
        run(p, c, 1.0)
        assert p.recover and p.mode == "recovering"
        assert p.timer_text() == "0:38", (st, p.timer_text())
        c.t += 30.0                                  # il tempo passa: la durata NON cambia
        run(p, c, 0.2)
        assert p.timer_text() == "0:38", st
        assert p._compact(), st
        h = p.render_frame().height
        assert h < p.style.max_height() * p.scale, (st, h)
        p.destroy()


def test_recupero_testo_apre_la_card_e_poi_recovered():
    for st in LP.STYLES:
        p, c = panel(st)
        p.open_recover(None, 38.4)
        run(p, c, 0.4)
        compatta = p.render_frame().height
        p.update({"committed": "Ciao Marco, il preventivo e' pronto.", "tentative": "", "rev": 1})
        run(p, c, 0.6)
        assert not p._compact(), st
        assert p.render_frame().height > compatta, st
        p.set_phase("recovered")
        run(p, c, 0.3)
        assert p.mode == "recovered" and p.progress() == 1.0, st
        p.close(pasted=True)                          # un esito non si taglia a meta'
        assert p.phase != "closing", st
        run(p, c, LP.T_RECOVERED + 0.2)               # ... ma si chiude da sola
        assert p.phase in ("closing", "closed"), (st, p.phase)
        p.destroy()


def test_recupero_a_vuoto_ha_il_pie_e_niente_corpo():
    """"no_audio" e "unrecovered": testata e pie' di pagina, nessuna riga vuota in mezzo."""
    for ph in ("no_audio", "unrecovered"):
        assert ph in cs.R_FOOTERS and cs.Style.has_foot(cs.make("signal"), ph)
        for st in LP.STYLES:
            p, c = panel(st)
            p.open_recover(None, 38.4 if ph == "unrecovered" else 0.0)
            run(p, c, 0.3)
            p.set_phase(ph)
            run(p, c, 0.6)
            top, pt, pb, bot = p._zones(1.0)
            assert abs(p._card_h - (top + bot)) < 1.5, (ph, st, p._card_h, top + bot)
            p.destroy()


def test_recupero_copy_senza_caratteri_difensivi():
    """Il pie' dice cosa fare, non cosa ci manca (niente "no", "not", "can't", "failed")."""
    for mode, (left, key, right) in cs.R_FOOTERS.items():
        testo = f"{left} {right}".lower()
        assert key.startswith("Win+Ctrl"), mode
        for brutta in ("no ", "not ", "n't", "fail", "error", "unable", "sorry"):
            assert brutta not in testo, (mode, brutta, testo)


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
