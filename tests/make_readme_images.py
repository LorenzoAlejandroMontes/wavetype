"""
Renders the images used by README.md into docs/img/.
Run: .venv/Scripts/python.exe tests/make_readme_images.py
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import live_panel                                            # noqa: E402
from live_panel import LivePanel                             # noqa: E402

DT = 1 / 30.0
OUT = os.path.join(ROOT, "docs", "img")
SAID = "Hi Marco, quick update on the Thursday quote: we went over the numbers with"
TAIL = " Martina and the total comes down to 4,200, delivery by the end of the month"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def run(p, clock, secs, level=0.6, seed=[0]):
    for _ in range(int(secs / DT)):
        clock.advance(DT)
        seed[0] += 1
        p.set_level(level * (0.35 + 0.65 * abs(math.sin(seed[0] * 0.37) * math.sin(seed[0] * 0.11 + 1))))
        p.render_frame()


def panel(style, scale=2.0):
    clock = Clock()
    p = LivePanel(log=lambda m: None, clock=clock, scale=scale, style=style)
    p.anchor_override = (400, 900, 30)
    p.work_override = (0, 0, 1920, 1080)
    return p, clock


def correction_frame(style, scale=2.0):
    """The moment that sells the product: a wrong word struck out, the right one highlighted."""
    p, clock = panel(style, scale)
    p.open()
    run(p, clock, 1.0, 0.3)
    p.update({"committed": SAID + " Martino", "tentative": "", "rev": 1})
    run(p, clock, 1.6)
    p.update({"committed": SAID + " Martina and the total comes down to 4,200,",
              "tentative": "delivery by the end", "rev": 2})
    run(p, clock, 0.25)
    return p.render_frame()


def editor_bg(w, h):
    """A neutral dark editor-ish backdrop so the card reads as floating over your work."""
    img = Image.new("RGB", (w, h), (18, 19, 22))
    d = ImageDraw.Draw(img)
    widths = (0.62, 0.78, 0.45, 0.70, 0.55, 0.83, 0.38, 0.66, 0.74, 0.50, 0.81, 0.43, 0.69, 0.58)
    y = 40
    i = 0
    while y < h - 30:
        f = widths[i % len(widths)]
        d.rounded_rectangle([56, y, 56 + int((w - 150) * f), y + 20], radius=10, fill=(38, 39, 45))
        y += 46
        i += 1
    return img


def hero():
    card = correction_frame("stamp")
    W, H = card.width + 260, card.height + 210
    bg = editor_bg(W, H).convert("RGBA")
    bg.alpha_composite(card, ((W - card.width) // 2, (H - card.height) // 2))
    return bg.convert("RGB")


def styles_sheet():
    """The three built-in looks, cropped to their real bounds and labelled."""
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\consola.ttf", 22)
    except Exception:
        f = ImageFont.load_default()
    cards = []
    for name in live_panel.STYLES:
        c = correction_frame(name, scale=1.5)
        cards.append((name.upper(), c.crop(c.getbbox())))
    gap, pad, lab = 46, 44, 34
    W = max(c.width for _, c in cards) + pad * 2
    H = sum(c.height + lab for _, c in cards) + gap * (len(cards) - 1) + pad * 2
    sheet = Image.new("RGB", (W, H), (18, 19, 22)).convert("RGBA")
    d = ImageDraw.Draw(sheet)
    y = pad
    for name, c in cards:
        d.text((pad, y), name, font=f, fill=(128, 130, 138))
        sheet.alpha_composite(c, ((W - c.width) // 2, y + lab))
        y += lab + c.height + gap
    return sheet.convert("RGB")


def edit_frame(style="stamp", scale=2.0):
    """Edit Mode: text selected, say an instruction (or press 1-3)."""
    p, clock = panel(style, scale)
    p.open_edit(None, 26)
    run(p, clock, 1.0, 0.3)
    p.update({"committed": "make it shorter and", "tentative": "more direct", "rev": 1})
    run(p, clock, 0.5)
    return p.render_frame()


def edit_shot():
    card = edit_frame()
    W, H = card.width + 260, card.height + 210
    bg = editor_bg(W, H).convert("RGBA")
    bg.alpha_composite(card, ((W - card.width) // 2, (H - card.height) // 2))
    return bg.convert("RGB")


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, img in (("hero.png", hero()), ("edit.png", edit_shot()), ("styles.png", styles_sheet())):
        path = os.path.join(OUT, name)
        img.save(path)
        print(path, img.size)


if __name__ == "__main__":
    main()
