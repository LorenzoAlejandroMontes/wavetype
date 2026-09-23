# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Wavetype (onedir). Build with packaging/build.ps1.
#
# Only files listed here go into the build: no glob over the repo, so groq_key.txt, vocab.txt,
# recordings/, models/, logs and history can never end up in dist/ by accident.
#
# WAVETYPE_WITH_LOCAL=1 also bundles the offline engine (faster-whisper + ctranslate2).
# Default is off: see packaging/README.md for the size numbers behind that choice.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
WITH_LOCAL = os.environ.get("WAVETYPE_WITH_LOCAL") == "1"


def asset(*parts):
    return os.path.join(ROOT, "assets", *parts)


FONTS = [f for f in os.listdir(asset("fonts")) if f.endswith((".woff2", ".txt"))]
datas = [(asset("fonts", f), "assets/fonts") for f in FONTS]
datas += [(asset(f), "assets") for f in ("pose-1-attesa.png", "pose-2-registra.png",
                                         "pose-3-elabora.png", "wavetype.ico")]
datas += [(os.path.join(ROOT, "LICENSE"), ".")]

excludes = [
    # live preview model runtime: useless without the ~750 MB model, which is not shipped
    "sherpa_onnx", "vosk",
    # never imported by the app, pulled in by optional hooks
    "matplotlib", "IPython", "jupyter", "pytest", "setuptools", "pip",
]
if not WITH_LOCAL:
    excludes += ["faster_whisper", "ctranslate2", "onnxruntime", "huggingface_hub", "tokenizers",
                 "hf_xet"]

a = Analysis(
    [os.path.join(ROOT, "wavetype.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=["first_run", "paths", "live_local", "live_panel", "card_styles", "caret",
                   "context", "edit_chips"],
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Wavetype",
    icon=asset("wavetype.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Wavetype")
