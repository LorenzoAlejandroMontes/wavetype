"""
Demo a schermo dei tre stili della card live: finestra VERA (layered, come in Wavetype), sopra a
una finta app con un campo di testo e un cursore finto vicino al fondo dello schermo.
Ogni stile passa per tutte le fasi; a ogni fase uno screenshot reale (ImageGrab, finestre
layered incluse) e alla fine un foglio per stile: styles_stamp.png, styles_glyph.png,
styles_signal.png. Misura anche i ms per frame di tick() (media, p95) per stile.

Non tocca Wavetype: processo suo, finestre sue (non rubano il focus), nessun file dell'app.

Uso: .venv/Scripts/python.exe tests/panel_styles_demo.py [cartella_uscita] [stile ...]
"""
import ctypes
import json
import math
import os
import sys
import time

ctypes.windll.shcore.SetProcessDpiAwareness(2)          # px fisici ovunque (grab compreso)

from PIL import Image, ImageDraw, ImageFont, ImageGrab   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import caret as caret_mod                                # noqa: E402
import live_panel                                        # noqa: E402
from live_panel import LivePanel                         # noqa: E402

FPS = 30
LIVE = "Ciao Marco, ti scrivo per il preventivo di giovedì: abbiamo rivisto i numeri con"
LONG = ("abbiamo rivisto i numeri con Martina e il totale scende a 4.200 euro, consegna entro fine mese "
        "se confermi entro venerdì. Per la parte grafica ci sentiamo lunedì con Giulia, così chiudiamo "
        "anche il calendario delle consegne")
FINAL = (LIVE + " Martina e il totale scende a 4.200 €, consegna entro fine mese se confermi entro venerdì.")
PASTED = "…4.200 €, consegna entro fine mese se confermi entro venerdì."

_user = ctypes.WinDLL("user32")


def ui_font(px, bold=False):
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    try:
        return ImageFont.truetype(os.path.join(r"C:\Windows\Fonts", name), px)
    except Exception:
        return ImageFont.load_default()


class Stage:
    """La finta app: una finestra layered (non attiva, non cliccabile) con righe di testo e un
    campo con il cursore. Usa lo stesso disegno della card (LivePanel._paint)."""

    def __init__(self, k):
        self.k = k
        self.win = LivePanel(log=lambda m: None)
        self.win._ensure_window()

    def show(self, x, y, w, h, dark=False, field_text="", caret_at_top=False):
        k = self.k
        bg = (23, 24, 27) if dark else (250, 250, 248)
        line = (40, 41, 45) if dark else (232, 232, 229)
        img = Image.new("RGBA", (w, h), bg + (255,))
        d = ImageDraw.Draw(img)
        fh = int(50 * k)
        fy = int(24 * k) if caret_at_top else h - fh - int(24 * k)
        ly0 = fy + fh + int(26 * k) if caret_at_top else int(26 * k)
        for i in range(2):
            d.rounded_rectangle([int(26 * k), ly0 + i * int(22 * k), w - int(26 * k), ly0 + i * int(22 * k) + int(10 * k)],
                                radius=int(5 * k), fill=line)
        d.rounded_rectangle([int(26 * k), fy, w - int(26 * k), fy + fh], radius=int(10 * k),
                            fill=(34, 35, 39) if dark else (255, 255, 255),
                            outline=(60, 62, 68) if dark else (205, 205, 205), width=max(1, int(k)))
        ink = (232, 232, 234) if dark else (29, 29, 31)
        f = ui_font(int(15 * k))
        tx = int(26 * k) + int(20 * k)
        cy = fy + fh // 2
        if field_text:
            d.text((tx, cy), field_text, font=f, fill=ink, anchor="lm")
            tx += int(f.getlength(field_text)) + int(2 * k)
        ch = int(19 * k)
        d.rectangle([tx, cy - ch // 2, tx + max(1, int(1.5 * k)) - 1, cy + ch // 2], fill=ink)
        self.win._paint(x, y, img)
        self.win._show(True)
        return (x + tx, y + cy - ch // 2, ch)          # caret (x, y, h) in px fisici

    def hide(self):
        self.win._show(False)


def speech_level(t):
    """Livello voce finto: sillabe e respiri, 0..1."""
    env = 0.5 + 0.5 * math.sin(t * 1.7)
    syl = abs(math.sin(t * 11.0)) * abs(math.sin(t * 3.3 + 1.0))
    return max(0.04, min(1.0, 0.15 + 0.85 * env * syl))


class Runner:
    def __init__(self, p, out, style, costs):
        self.p, self.out, self.style, self.costs = p, out, style, costs
        self.t0 = time.perf_counter()
        self.shots = []
        self.speaking = True

    def run(self, secs):
        end = time.perf_counter() + secs
        while time.perf_counter() < end:
            t = time.perf_counter()
            self.p.set_level(speech_level(t - self.t0) if self.speaking else 0.0)
            alive = self.p.tick()
            if alive:
                self.costs.append(self.p.last_frame_ms)
            dt = 1.0 / FPS - (time.perf_counter() - t)
            if dt > 0:
                time.sleep(dt)

    def shot(self, name, bbox):
        img = ImageGrab.grab(bbox=bbox, include_layered_windows=True, all_screens=True)
        path = os.path.join(self.out, f"{self.style}_{len(self.shots) + 1:02d}_{name}.png")
        img.save(path)
        self.shots.append((name, path))


def walk(style, out, costs_all):
    with caret_mod.dpi_scope():
        wl, wt, wr, wb = caret_mod.work_area(200, 200)
        k = caret_mod.dpi_for_point(200, 200) / 96.0
    sw, sh = int(560 * k), int(300 * k)
    sx = wl + int(60 * k)
    sy = wb - sh - int(30 * k)
    bbox = (sx, sy, sx + sw, sy + sh)
    stage = Stage(k)
    p = LivePanel(log=lambda m: None)
    p.set_style(style)
    costs = []
    r = Runner(p, out, style, costs)
    cap = stage.show(sx, sy, sw, sh)

    def start():
        p.anchor_override = cap
        p.open(None)

    # 01 in ascolto
    start()
    r.run(1.1)
    r.shot("listening", bbox)
    # 02 prime parole (incerte)
    p.update({"committed": "", "tentative": "Ciao Marco,", "rev": 1})
    r.run(0.7)
    r.shot("first-words", bbox)
    # 03 live + correzione (barrato + evidenziatore)
    p.update({"committed": "Ciao Marco, ti scrivo per il preventivo di giovedì: ehm", "tentative": "", "rev": 2})
    r.run(0.5)
    p.update({"committed": "Ciao Marco, ti scrivo per il preventivo di giovedì: abbiamo rivisto i numeri con Martino",
              "tentative": "", "rev": 3})
    r.run(1.3)
    p.update({"committed": LIVE + " Martina", "tentative": "e il totale scende", "rev": 4})
    r.run(0.28)
    r.shot("live-correction", bbox)
    r.run(1.0)
    # 04 dettatura lunga
    p.update({"committed": LIVE + " Martina e il totale scende a 4.200 euro, " + LONG, "tentative": "e la data del",
              "rev": 5})
    r.run(1.2)
    r.shot("long", bbox)
    # 05 pausa
    r.speaking = False
    p.update({"committed": LIVE + " Martina e il totale scende a 4.200 euro, " + LONG + " e la data del", "tentative": "",
              "rev": 6})
    r.run(0.6)
    p.set_phase("paused")
    r.run(0.7)
    r.shot("paused", bbox)
    # 06 formattazione
    p.set_phase("formatting")
    p.update({"committed": FINAL, "tentative": "", "rev": 7})
    r.run(1.3)
    r.shot("formatting", bbox)
    # 07 incollato: il cursore e' ora in fondo al testo incollato
    cap2 = stage.show(sx, sy, sw, sh, field_text=PASTED)
    p.anchor_override = cap2
    p.set_phase("inserted")
    r.run(0.35)
    r.shot("inserted", bbox)
    r.run(1.0)
    # 08 annullato
    cap = stage.show(sx, sy, sw, sh)
    r.speaking = True
    start()
    r.run(0.3)
    p.update({"committed": LIVE + " Martina e il totale scende a 4.200 euro,", "tentative": "", "rev": 1})
    r.run(1.0)
    p.set_phase("cancelled")
    r.run(0.45)
    r.shot("cancelled", bbox)
    r.run(1.3)
    # 09 rete giu'
    start()
    r.run(0.3)
    p.update({"committed": "Ciao Marco, ti scrivo per il preventivo", "tentative": "", "rev": 1})
    r.run(0.6)
    p.set_phase("offline")
    r.run(0.8)
    r.shot("offline", bbox)
    p.close(pasted=False)
    r.run(0.4)
    # 10 sotto il cursore (niente spazio sopra)
    stage.hide()
    ty = wt + int(8 * k)
    bbox_top = (sx, ty, sx + sw, ty + sh)
    cap_top = stage.show(sx, ty, sw, sh, caret_at_top=True)
    p.anchor_override = cap_top
    p.open(None)
    r.run(0.4)
    p.update({"committed": LIVE + " Martina", "tentative": "e il totale scende", "rev": 1})
    r.run(1.0)
    r.shot("below-caret", bbox_top)
    p.close(pasted=False)
    r.run(0.4)
    stage.hide()
    # 11 su app scura
    cap = stage.show(sx, sy, sw, sh, dark=True)
    start()
    r.run(0.3)
    p.update({"committed": LIVE + " Martina", "tentative": "e il totale scende", "rev": 1})
    r.run(1.0)
    r.shot("dark-app", bbox)
    p.close(pasted=True)
    r.run(0.5)
    # 12 anteprima dello stile (tasto che cambia stile), nessuna dettatura in corso
    cap = stage.show(sx, sy, sw, sh)
    p.anchor_override = cap
    r.speaking = False
    p.preview_style(style)
    r.run(0.55)
    r.shot("style-preview", bbox)
    r.run(1.6)
    alive = p.is_open()
    stage.hide()
    p.destroy()
    stage.win.destroy()
    costs_all[style] = costs
    return r.shots, alive


def contact_sheet(style, shots, out):
    imgs = [(n, Image.open(pth).convert("RGB")) for n, pth in shots]
    w = max(i.width for _, i in imgs)
    h = max(i.height for _, i in imgs)
    cols = 3
    rows = (len(imgs) + cols - 1) // cols
    head = 90
    cap = 44
    pad = 24
    W = cols * (w + pad) + pad
    H = head + rows * (h + cap + pad) + pad
    sheet = Image.new("RGB", (W, H), (233, 231, 226))
    d = ImageDraw.Draw(sheet)
    d.text((pad, 24), f"Wavetype live card · {live_panel.cs.TITLES[style]} · finestra reale, screenshot a schermo",
           font=ui_font(34, True), fill=(20, 20, 20))
    for i, (n, im) in enumerate(imgs):
        r_, c = divmod(i, cols)
        x = pad + c * (w + pad)
        y = head + r_ * (h + cap + pad)
        d.text((x, y + 6), f"{i + 1:02d}  {n}", font=ui_font(26), fill=(60, 58, 52))
        sheet.paste(im, (x, y + cap))
    path = os.path.join(out, f"styles_{style}.png")
    sheet.save(path)
    return path


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tests", "panel_out", "styles")
    styles = sys.argv[2:] or list(live_panel.STYLES)
    os.makedirs(out, exist_ok=True)
    costs = {}
    report = {}
    for st in styles:
        shots, alive = walk(st, out, costs)
        path = contact_sheet(st, shots, out)
        c = sorted(costs[st])
        report[st] = {"sheet": path, "frames": len(c),
                      "mean_ms": round(sum(c) / len(c), 2),
                      "p95_ms": round(c[int(len(c) * 0.95) - 1], 2),
                      "max_ms": round(c[-1], 2),
                      "open_after_preview": alive}
        print(st, json.dumps(report[st]))
    with open(os.path.join(out, "frame_times.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
