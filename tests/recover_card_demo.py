"""
Fogli di contatto della card del recupero (Win+Ctrl+R): ogni stato x i tre stili, senza finestra.

    .venv/Scripts/python.exe tests/recover_card_demo.py [cartella]

Scrive recover_card_all.png (tutto) e recover_card_<stile>.png (una colonna per volta).
Serve a guardare il reso: una suite verde non dice niente su una card storta.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import live_panel as LP                                        # noqa: E402
from edit_card_demo import panel, run, sheet                   # noqa: E402

TEXT = ("Ciao Marco, ti scrivo per il preventivo di giovedì: i numeri sono quelli "
        "dell'ultima volta, la scadenza è il 30.")


def shots(style, scale=1.5):
    """[(nome, immagine)] degli stati del recupero, nell'ordine in cui capitano."""
    out = []

    def add(name, p, c, secs=0.6):
        run(p, c, secs)
        out.append((name, p.render_frame()))

    # 01 sto recuperando: card compatta, durata dell'audio accanto al titolo
    p, c = panel(style, scale)
    p.open_recover(None, 38.4)
    add("01 recovering 0:38", p, c, 1.0)
    # 02 testo tornato e incollato: le parole compaiono tutte insieme
    p.update({"committed": TEXT, "tentative": "", "rev": 1})
    run(p, c, 0.5)
    p.set_phase("recovered")
    add("02 recovered", p, c, 0.5)
    p.destroy()

    # 03 audio lungo (il tempo a due cifre non stringe la card)
    p, c = panel(style, scale)
    p.open_recover(None, 214.0)
    add("03 recovering 3:34", p, c, 1.0)
    p.destroy()

    # 04 niente in archivio
    p, c = panel(style, scale)
    p.open_recover(None, 0.0)
    run(p, c, 0.3)
    p.set_phase("no_audio")
    add("04 no audio", p, c, 0.6)
    p.destroy()

    # 05 audio c'e', il testo non e' tornato (rete giu')
    p, c = panel(style, scale)
    p.open_recover(None, 38.4)
    run(p, c, 0.3)
    p.set_phase("unrecovered")
    add("05 unrecovered", p, c, 0.6)
    p.destroy()

    # 06 al 100%: stesse misure, niente tagliato
    p, c = panel(style, 1.0)
    p.open_recover(None, 38.4)
    add("06 at 100%", p, c, 1.0)
    p.destroy()

    # 07 card stretta col pie' di pagina (schermo piccolo)
    p, c = panel(style, 1.0, width=520)
    p.open_recover(None, 38.4)
    run(p, c, 0.3)
    p.set_phase("unrecovered")
    add("07 narrow unrecovered", p, c, 0.6)
    p.destroy()
    return out


def main(dst):
    os.makedirs(dst, exist_ok=True)
    per = {st: shots(st) for st in LP.STYLES}
    names = [n for n, _ in per[LP.STYLES[0]]]
    rows = [(n, [per[st][i][1] for st in LP.STYLES]) for i, n in enumerate(names)]
    p = os.path.join(dst, "recover_card_all.png")
    sheet(rows, "Wavetype · recupero Win+Ctrl+R · card resa (150%)").save(p, optimize=True)
    print(p)
    for st in LP.STYLES:
        rows1 = [(n, [im]) for n, im in per[st]]
        p = os.path.join(dst, f"recover_card_{st}.png")
        sheet(rows1, f"Wavetype · recupero · {st} (150%)").save(p, optimize=True)
        print(p)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tests", "panel_out", "recover"))
