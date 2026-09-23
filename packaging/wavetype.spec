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
    # sherpa_onnx IS shipped (live words next to the caret): the app downloads the Nemotron model
    # on first run (model_fetch.py). Vosk is the old Italian-only backend: not used, not shipped.
    "vosk",
    # never imported by the app, pulled in by optional hooks
    "matplotlib", "IPython", "jupyter", "pytest", "setuptools", "pip",
]
if not WITH_LOCAL:
    excludes += ["faster_whisper", "ctranslate2", "onnxruntime", "huggingface_hub", "tokenizers",
                 "hf_xet"]

# sherpa-onnx has no PyInstaller hook: its extension (_sherpa_onnx.pyd) loads onnxruntime.dll and
# the sherpa-onnx C API dlls from its own sherpa_onnx/lib folder. Ship them there, same layout.
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

binaries = collect_dynamic_libs("sherpa_onnx")
hidden_sherpa = collect_submodules("sherpa_onnx", filter=lambda m: not m.endswith((".cli", ".__main__")))

a = Analysis(
    [os.path.join(ROOT, "wavetype.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=["first_run", "paths", "live_local", "live_panel", "card_styles", "caret",
                   "context", "edit_chips", "model_fetch"] + hidden_sherpa,
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
