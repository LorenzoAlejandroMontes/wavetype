"""
Fogli di contatto della card di Edit Mode: ogni stato x i tre stili, senza finestra.

    .venv/Scripts/python.exe tests/edit_card_demo.py [cartella]

Scrive edit_card_all.png (tutto) ed edit_card_<stile>.png (una colonna per volta).
Serve a guardare il reso e confrontarlo con la lavagna approvata: una suite verde non dice
niente su una card storta.
"""
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import card_styles as cs                                       # noqa: E402
import live_panel as LP                                        # noqa: E402

BG = (0xE9, 0xE7, 0xE2)
STAGE = (0xFA, 0xFA, 0xF8)
PAD = 16
LAB_W = 190
GUT = 24


class Clock:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


def panel(style, scale=1.5, width=None):
    c = Clock()
    p = LP.LivePanel(log=lambda m: None, clock=c, scale=scale, style=style)
    p.anchor_override = (620, 900, 30)
    p.work_override = (0, 0, width or 1920, 1080)
    return p, c


def run(p, c, secs, dt=1 / 30.0, level=0.0):
    import math
    for _ in range(max(1, int(secs / dt))):
        c.t += dt
        if level:
            p.set_level(level * (0.45 + 0.55 * abs(math.sin(c.t * 7.3) * math.sin(c.t * 2.1))))
        p.render_frame()


def feed(p, c, committed, tentative, secs=1.2):
    """Scrive l'istruzione a pezzi, come farebbe il motore, e lascia posare l'animazione."""
    words = committed.split()
    for i in range(1, len(words) + 1):
        p.update({"committed": " ".join(words[:i]), "tentative": tentative, "rev": i})
        run(p, c, 0.06, level=0.85)
    run(p, c, secs, level=0.85)


LONG = ("rendilo molto piu' corto e diretto, togli i convenevoli e lascia solo i numeri "
        "e la data di scadenza")


def shots(style, scale=1.5):
    """[(nome, immagine)] di tutti gli stati, nell'ordine della lavagna."""
    out = []

    def add(name, p, c, secs=0.6, level=0.0):
        run(p, c, secs, level=level)
        out.append((name, p.render_frame()))

    # 01 in ascolto
    p, c = panel(style, scale)
    p.open_edit(None, 26)
    add("01 listening", p, c, 1.0, level=0.3)
    # 02 istruzione = chip
    feed(p, c, "correggi la", "grammatica")
    add("02 chip recognized", p, c, 0.1)
    # 03 riscrivo con il chip acceso
    p.update({"committed": "correggi la grammatica", "tentative": "", "rev": 99})
    run(p, c, 0.3)
    p.set_phase("rewriting")
    add("03 rewriting chip", p, c, 1.4)
    p.destroy()

    # 02b istruzione libera
    p, c = panel(style, scale)
    p.open_edit(None, 26)
    run(p, c, 0.4)
    feed(p, c, "rendilo piu' corto e", "piu' diretto")
    add("02b free instruction", p, c, 0.1)
    # 03b riscrivo senza chip (istruzione libera): niente riga dei comandi
    p.set_phase("rewriting")
    add("03b rewriting free", p, c, 1.4)
    p.destroy()

    # 04 pillole di esito (una per chip + libera)
    for chip, res in (("1", 24), ("2", 23), ("3", 22), (None, 21)):
        p, c = panel(style, scale)
        p.open_edit(None, 26)
        run(p, c, 0.3)
        if chip:
            p.set_chip(chip)
        p.set_result(res)
        p.set_phase("done")
        add(f"04 done {p.edit_done_text().lower()}", p, c, 0.4)
        p.destroy()

    # 05 annullato · 06 rete giu' · 07 senza selezione
    for name, ph, chip in (("05 cancelled", "cancelled", None),
                           ("06 offline", "offline", "1"),
                           ("07 nothing selected", "nothing", None)):
        p, c = panel(style, scale)
        p.open_edit(None, 26 if ph != "nothing" else 0)
        run(p, c, 0.3)
        if chip:
            p.set_chip(chip)
        p.set_phase(ph)
        add(name, p, c, 0.5)
        p.destroy()

    # 08 istruzione lunga (va a capo, la card non si rompe)
    p, c = panel(style, scale)
    p.open_edit(None, 26)
    run(p, c, 0.3)
    feed(p, c, LONG, "e la data", 0.6)
    add("08 long instruction", p, c, 0.1)
    p.destroy()

    # 09 card stretta: la parola sparisce, resta il numero
    p, c = panel(style, scale, width=520)
    p.open_edit(None, 26)
    add("09 narrow card", p, c, 0.5)
    p.destroy()

    # 10 al 100%: stesse misure, niente tagliato
    p, c = panel(style, 1.0)
    p.open_edit(None, 26)
    run(p, c, 0.3)
    feed(p, c, "correggi la", "grammatica", 0.3)
    add("10 at 100%", p, c, 0.1)
    p.destroy()

    # 11 al 100%, card stretta
    p, c = panel(style, 1.0, width=360)
    p.open_edit(None, 26)
    add("11 at 100% narrow", p, c, 0.5)
    p.destroy()
    return out


def sheet(rows, title):
    """rows = [(nome, [img per stile]) ...] -> un foglio di contatto."""
    n = len(rows[0][1])
    col_w = max(max(im.width for im in imgs) for _, imgs in rows) + GUT
    heights = [max(PAD + max(i.height for i in imgs) + PAD, 96) for _, imgs in rows]
    W = LAB_W + n * col_w + PAD
    H = 46 + sum(heights)
    out = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(out)
    f = cs.font("jbmono", 13, wght=700)
    fs = cs.font("jbmono", 12, wght=500)
    d.text((PAD, 16), title, font=f, fill=(20, 20, 20))
    for i in range(n):
        d.text((LAB_W + i * col_w + PAD, 16), LP.STYLES[i].upper() if n == 3 else "", font=fs,
               fill=(0x8a, 0x86, 0x7d))
    y = 46
    for (name, imgs), h in zip(rows, heights):
        d.rectangle([LAB_W - 6, y + 4, W - PAD, y + h - 6], fill=STAGE)
        d.text((PAD, y + 14), name, font=fs, fill=(20, 20, 20))
        for i, im in enumerate(imgs):
            x = LAB_W + i * col_w + (col_w - im.width) // 2
            out.paste(im, (max(LAB_W, x), y + (h - im.height) // 2), im)
        y += h
        d.line([(PAD, y), (W - PAD, y)], fill=(0xcf, 0xcc, 0xc5))
    return out


def main(dst):
    os.makedirs(dst, exist_ok=True)
    per = {st: shots(st) for st in LP.STYLES}
    names = [n for n, _ in per[LP.STYLES[0]]]
    rows = [(n, [per[st][i][1] for st in LP.STYLES]) for i, n in enumerate(names)]
    p = os.path.join(dst, "edit_card_all.png")
    sheet(rows, "Wavetype · Edit Mode · card resa (150%)").save(p, optimize=True)
    print(p)
    for st in LP.STYLES:
        rows1 = [(n, [im]) for n, im in per[st]]
        p = os.path.join(dst, f"edit_card_{st}.png")
        sheet(rows1, f"Wavetype · Edit Mode · {st} (150%)").save(p, optimize=True)
        print(p)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tests", "panel_out", "edit"))
