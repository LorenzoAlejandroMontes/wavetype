"""
Fogli di contatto della card "copied" (incolla saltato, testo negli appunti): la fase accanto a
"inserted" e alle fasi a cui somiglia, nei tre stili, senza finestra.

    .venv/Scripts/python.exe tests/copied_card_demo.py [cartella]

Scrive copied_all.png (tutto) e copied_<stile>.png (una colonna per volta).
La fase capita solo su Mac: il pie' e' reso con il tasto del Mac (Cmd+V) anche da Windows.
Serve a guardare il reso: una suite verde non dice niente su una card storta.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import card_styles as cs                                       # noqa: E402
import live_panel as LP                                        # noqa: E402
from edit_card_demo import panel, run, sheet                   # noqa: E402

TEXT = "Hello Sarah, the meeting moved to Thursday at 3. Can you bring the slides?"
RAW = "hello sarah the meeting moved to thursday at 3 can you bring the slides"
LONG = (TEXT + " I also sent the budget to Mark this morning, he will confirm the numbers "
        "before lunch and then we can lock the agenda for the whole week.")
MAC_FOOT = ("On clipboard", "Cmd+V", "to paste")


def dictated(style, scale=1.5, width=None, text=TEXT, raw=RAW):
    """Card di una dettatura arrivata al testo finale (come dopo live_show_final)."""
    p, c = panel(style, scale, width)
    p.open()
    run(p, c, 0.4, level=0.4)
    p.update({"committed": raw, "tentative": "", "rev": 1})
    run(p, c, 0.8, level=0.6)
    p.set_phase("formatting")
    run(p, c, 0.5)
    p.update({"committed": text, "tentative": "", "rev": -1})
    return p, c


def shots(style, scale=1.5):
    out = []

    def add(name, p, c, secs=0.6):
        run(p, c, secs)
        out.append((name, p.render_frame()))
        p.destroy()

    # 01 il confronto: incolla riuscito (pillola al cursore)
    p, c = dictated(style, scale)
    p.set_phase("inserted")
    add("01 inserted", p, c, 0.4)
    # 02 incolla saltato: testo intero, pie' col tasto
    p, c = dictated(style, scale)
    p.set_phase("copied")
    add("02 copied", p, c, 0.9)
    # 03 dopo 3,5 s e' ancora li' uguale (si chiude a 4)
    p, c = dictated(style, scale)
    p.set_phase("copied")
    add("03 copied at 3.5 s", p, c, 3.5)
    # 04 testo lungo: le righe vecchie salgono, il pie' resta
    p, c = dictated(style, scale, text=LONG)
    p.set_phase("copied")
    add("04 copied long text", p, c, 1.2)
    # 05 una parola
    p, c = dictated(style, scale, text="Thanks.", raw="thanks")
    p.set_phase("copied")
    add("05 copied one word", p, c, 0.9)
    # 06 al 100%
    p, c = dictated(style, 1.0)
    p.set_phase("copied")
    add("06 copied at 100%", p, c, 0.9)
    # 07 card stretta (schermo piccolo)
    p, c = dictated(style, 1.0, width=520)
    p.set_phase("copied")
    add("07 copied narrow", p, c, 0.9)
    # 08 recupero non incollato
    p, c = panel(style, scale)
    p.open_recover(None, 38.4)
    run(p, c, 0.6)
    p.update({"committed": TEXT, "tentative": "", "rev": 1})
    run(p, c, 0.5)
    p.set_phase("copied")
    add("08 recover copied", p, c, 0.9)
    # 09 Edit non incollato: niente pillola "GRAMMAR FIXED"
    p, c = panel(style, scale)
    p.open_edit(None, 26)
    run(p, c, 0.3)
    p.set_chip("1")
    p.set_result(24)
    p.set_phase("done")
    p.set_phase("copied")
    add("09 edit copied", p, c, 0.9)
    # 10-11 le fasi a cui somiglia, per il confronto: recovered (corpo), unrecovered (pie')
    p, c = panel(style, scale)
    p.open_recover(None, 38.4)
    run(p, c, 0.6)
    p.update({"committed": TEXT, "tentative": "", "rev": 1})
    run(p, c, 0.5)
    p.set_phase("recovered")
    add("10 (ref) recovered", p, c, 0.5)
    p, c = panel(style, scale)
    p.open_recover(None, 38.4)
    run(p, c, 0.3)
    p.set_phase("unrecovered")
    add("11 (ref) unrecovered", p, c, 0.6)
    return out


def main(dst):
    os.makedirs(dst, exist_ok=True)
    saved = (cs.R_FOOTERS["copied"], cs.E_FOOTERS["e_copied"])
    cs.R_FOOTERS["copied"] = cs.E_FOOTERS["e_copied"] = MAC_FOOT     # il tasto che vede chi la vede
    try:
        per = {st: shots(st) for st in LP.STYLES}
    finally:
        cs.R_FOOTERS["copied"], cs.E_FOOTERS["e_copied"] = saved
    names = [n for n, _ in per[LP.STYLES[0]]]
    rows = [(n, [per[st][i][1] for st in LP.STYLES]) for i, n in enumerate(names)]
    p = os.path.join(dst, "copied_all.png")
    sheet(rows, "Wavetype · copied (150%)").save(p, optimize=True)
    print(p)
    for st in LP.STYLES:
        rows1 = [(n, [im]) for n, im in per[st]]
        p = os.path.join(dst, f"copied_{st}.png")
        sheet(rows1, f"Wavetype · copied · {st} (150%)").save(p, optimize=True)
        print(p)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tests", "panel_out", "copied"))
