"""docs/img/logo-square.png -> assets/wavetype.ico (16..256 px), per l'exe, il setup e la finestra."""
import os
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = Image.open(os.path.join(ROOT, "docs", "img", "logo-square.png")).convert("RGBA")
os.makedirs(os.path.join(ROOT, "packaging", "art"), exist_ok=True)
out = os.path.join(ROOT, "assets", "wavetype.ico")
src.save(out, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out, os.path.getsize(out))

# Installer artwork (Inno Setup): small logo top-right on every page, tall panel on the
# welcome/finish pages. Several sizes so it stays sharp at 100-200% scaling.
BG = (12, 13, 15)
for s in (55, 64, 83, 92, 110, 119, 138):
    img = Image.new("RGB", (s, s), BG)
    img.paste(src.convert("RGB").resize((s, s), Image.LANCZOS), (0, 0))
    img.save(os.path.join(ROOT, "packaging", "art", f"small-{s}.bmp"))
for w, h in ((164, 314), (192, 386), (246, 459), (273, 556), (328, 604), (355, 700), (410, 797)):
    img = Image.new("RGB", (w, h), BG)
    logo = src.convert("RGB").resize((w, w), Image.LANCZOS)
    img.paste(logo, (0, (h - w) // 2))
    img.save(os.path.join(ROOT, "packaging", "art", f"large-{w}.bmp"))
print("installer art in packaging/art")
