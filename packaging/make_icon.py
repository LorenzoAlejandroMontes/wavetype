"""docs/img/logo-square.png -> assets/wavetype.ico (16..256 px), per l'exe, il setup e la finestra."""
import os
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = Image.open(os.path.join(ROOT, "docs", "img", "logo-square.png")).convert("RGBA")
out = os.path.join(ROOT, "assets", "wavetype.ico")
src.save(out, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out, os.path.getsize(out))
