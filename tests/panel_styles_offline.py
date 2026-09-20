"""
Provino offline dei tre stili (niente finestre): ogni stile x ogni fase, reso con render_frame()
su uno sfondo chiaro e uno scuro, in un foglio PNG per stile. Serve a iterare in fretta sul
disegno; la verifica vera e' tests/panel_styles_demo.py (finestra reale + screenshot).

Uso: .venv/Scripts/python.exe tests/panel_styles_offline.py [cartella_uscita]
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import live_panel                                            # noqa: E402
from live_panel import LivePanel                             # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


DT = 1 / 30.0
LIVE = "Ciao Marco, ti scrivo per il preventivo di giovedì: abbiamo rivisto i numeri con"
LONG = ("abbiamo rivisto i numeri con Martina e il totale scende a 4.200 euro, consegna entro fine mese "
        "se confermi entro venerdì. Per la parte grafica ci sentiamo lunedì con Giulia, così chiudiamo "
        "anche il calendario delle consegne")


def run(p, clock, secs, level=0.6, seed=[0]):
    import math
    for _ in range(int(secs / DT)):
        clock.advance(DT)
        seed[0] += 1
        p.set_level(level * (0.35 + 0.65 * abs(math.sin(seed[0] * 0.37) * math.sin(seed[0] * 0.11 + 1))))
        p.render_frame()


def scenes(p, clock):
    """Genera (nome, immagine) per ogni fase."""
    p.open()
    run(p, clock, 1.2, 0.3)
    yield "listening", p.render_frame()
    p.update({"committed": "", "tentative": "Ciao Marco,", "rev": 1})
    run(p, clock, 0.6)
    yield "live-first", p.render_frame()
    p.update({"committed": LIVE + " Martino", "tentative": "", "rev": 2})
    run(p, clock, 1.6)
    p.update({"committed": LIVE + " Martina", "tentative": "e il totale scende", "rev": 3})
    run(p, clock, 0.25)
    yield "live-strike", p.render_frame()
    p.update({"committed": LIVE + " Martina e il totale scende a 4.200 euro,", "tentative": "", "rev": 4})
    run(p, clock, 1.0, 0.0)
    p.set_phase("paused")
    run(p, clock, 0.6, 0.0)
    yield "paused", p.render_frame()
    p.set_phase("live")
    p.update({"committed": LIVE + " Martina e il totale scende a 4.200 euro, " + LONG, "tentative": "e la data", "rev": 5})
    run(p, clock, 1.0)
    yield "live-long", p.render_frame()
    p.set_phase("offline")
    run(p, clock, 0.6)
    yield "offline", p.render_frame()
    p.set_phase("formatting")
    p.update({"committed": LIVE + " Martina e il totale scende a 4.200 euro, consegna entro fine mese.",
              "tentative": "", "rev": 6})
    run(p, clock, 0.9, 0.0)
    yield "formatting", p.render_frame()
    p.set_phase("inserted")
    run(p, clock, 0.35, 0.0)
    yield "inserted", p.render_frame()
    run(p, clock, 1.2, 0.0)
    p.open()
    run(p, clock, 0.3)
    p.update({"committed": LIVE + " Martina e il totale scende a 4.200 euro,", "tentative": "", "rev": 1})
    run(p, clock, 1.0)
    p.set_phase("cancelled")
    run(p, clock, 0.4, 0.0)
    yield "cancelled", p.render_frame()
    run(p, clock, 1.5)


def bg(w, h, dark):
    img = Image.new("RGB", (w, h), (23, 24, 27) if dark else (250, 250, 248))
    d = ImageDraw.Draw(img)
    for i in range(3):
        d.rounded_rectangle([30, 30 + i * 33, w - 30, 45 + i * 33], radius=7,
                            fill=(40, 41, 45) if dark else (235, 235, 233))
    return img


def main(out):
    os.makedirs(out, exist_ok=True)
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 26)
    except Exception:
        f = ImageFont.load_default()
    for style in live_panel.STYLES:
        clock = FakeClock()
        p = LivePanel(log=lambda m: None, clock=clock, scale=1.5, style=style)
        p.anchor_override = (400, 900, 30)
        p.work_override = (0, 0, 1920, 1080)
        cells = []
        for name, img in scenes(p, clock):
            cells.append((name, img))
        cw = max(i.width for _, i in cells) + 40
        chh = max(i.height for _, i in cells) + 60
        cols = 3
        rows = (len(cells) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cw * 2, rows * chh), (233, 231, 226))
        d = ImageDraw.Draw(sheet)
        for i, (name, img) in enumerate(cells):
            r, c = divmod(i, cols)
            for j, dark in enumerate((False, True)):
                x0, y0 = (c * 2 + j) * cw, r * chh
                b = bg(cw - 10, chh - 50, dark).convert("RGBA")
                b.alpha_composite(img, (20, (chh - 50 - img.height) // 2))
                sheet.paste(b.convert("RGB"), (x0 + 5, y0 + 45))
                d.text((x0 + 12, y0 + 8), f"{style} · {name}", font=f, fill=(20, 20, 20))
        path = os.path.join(out, f"offline_{style}.png")
        sheet.save(path)
        print(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tests", "panel_out"))
