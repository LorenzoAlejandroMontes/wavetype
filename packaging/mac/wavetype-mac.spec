# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Wavetype on macOS (onedir + .app bundle). Build with packaging/mac/build.sh.
#
# Same rule as the Windows spec: only files listed here go into the build, no glob over the repo
# for data, so groq_key.txt, vocab.txt, recordings/, models/, logs and history never end up in dist/.
#
# Differences from packaging/wavetype.spec, each on purpose:
#   - the offline engine (faster-whisper + ctranslate2 + onnxruntime) is IN by default: on Mac the
#     first run and the CI test run without a Groq key, and the local engine is what answers then.
#     WAVETYPE_WITH_LOCAL=0 leaves it out.
#   - arm64 only, macOS 14+: onnxruntime ships macOS wheels for arm64 / macOS 14 only.
#   - signing: WAVETYPE_CODESIGN_IDENTITY empty -> ad-hoc (PyInstaller's default), otherwise that
#     identity on every collected binary, with hardened runtime and packaging/mac/entitlements.plist.
#   - icon: the .icns that build.sh makes from docs/img/logo-square.png (WAVETYPE_ICNS).
import glob
import os
import re

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
WITH_LOCAL = os.environ.get("WAVETYPE_WITH_LOCAL", "1") != "0"
IDENTITY = os.environ.get("WAVETYPE_CODESIGN_IDENTITY", "").strip() or None
ICNS = os.environ.get("WAVETYPE_ICNS") or os.path.join(ROOT, "build", "mac", "Wavetype.icns")
ENTITLEMENTS = os.path.join(SPECPATH, "entitlements.plist")
# DECISION (reversible until the first public release, then frozen: TCC grants and the
# Application Support folder hang on it): reverse-DNS of the maintainer, not of a domain we own.
BUNDLE_ID = "com.lorenzomontes.wavetype"


def _version():
    """Same source as the Windows build: ProductVersion in packaging/version_info.txt
    (read by PyInstaller's EXE(version=...) on Windows)."""
    with open(os.path.join(ROOT, "packaging", "version_info.txt"), encoding="utf-8") as f:
        m = re.search(r"StringStruct\('ProductVersion',\s*'([0-9][0-9.]*)'\)", f.read())
    if not m:
        raise SystemExit("ProductVersion not found in packaging/version_info.txt")
    return m.group(1)


VERSION = _version()


def asset(*parts):
    return os.path.join(ROOT, "assets", *parts)


FONTS = [f for f in os.listdir(asset("fonts")) if f.endswith((".woff2", ".txt"))]
datas = [(asset("fonts", f), "assets/fonts") for f in FONTS]
datas += [(asset(f), "assets") for f in ("pose-1-attesa.png", "pose-2-registra.png",
                                         "pose-3-elabora.png", "wavetype.ico")]
datas += [(os.path.join(ROOT, "LICENSE"), ".")]

excludes = [
    "vosk",
    "matplotlib", "IPython", "jupyter", "pytest", "setuptools", "pip",
    # Windows-only, never importable here: keeps the analysis warnings short
    "win32api", "win32con", "win32gui", "win32event", "winerror", "pywintypes", "comtypes",
    "pycaw", "keyboard", "winsound",
]
if not WITH_LOCAL:
    excludes += ["faster_whisper", "ctranslate2", "onnxruntime", "huggingface_hub", "tokenizers",
                 "hf_xet"]

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


def _libs(pkg):
    try:
        return collect_dynamic_libs(pkg)
    except Exception as e:                       # package not installed: say it, keep building
        print(f"[wavetype-mac.spec] no dynamic libs from {pkg}: {e}")
        return []


# sherpa-onnx has no PyInstaller hook (its extension loads libonnxruntime and the sherpa C API
# dylibs from sherpa_onnx/lib). ctranslate2 and onnxruntime ship their dylibs inside the package
# too: collected the same way so the loader finds them at the same relative path.
binaries = _libs("sherpa_onnx")
if WITH_LOCAL:
    binaries += _libs("ctranslate2") + _libs("onnxruntime")
    # transcribe(vad_filter=True) loads faster_whisper/assets/silero_vad_*.onnx, and no
    # PyInstaller hook ships it (none in pyinstaller-hooks-contrib 2026.7)
    datas += collect_data_files("faster_whisper")
hidden_sherpa = collect_submodules("sherpa_onnx", filter=lambda m: not m.endswith((".cli", ".__main__")))

# Every top-level module of the app, so a platform module imported lazily (the Mac layer) is in
# the bundle without editing this list. Module names only: no data file is picked up this way.
APP_MODULES = sorted(os.path.splitext(os.path.basename(p))[0]
                     for p in glob.glob(os.path.join(ROOT, "*.py"))
                     if os.path.basename(p) != "wavetype.py")
PYOBJC = ["objc", "Foundation", "AppKit", "Quartz", "ApplicationServices", "AVFoundation",
          "CoreFoundation"]
# PyObjC wrappers load parts of themselves lazily: take every submodule, no hook does it for us
for _pkg in list(PYOBJC):
    try:
        PYOBJC += collect_submodules(_pkg)
    except Exception as e:
        print(f"[wavetype-mac.spec] no submodules from {_pkg}: {e}")

a = Analysis(
    [os.path.join(ROOT, "wavetype.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=APP_MODULES + PYOBJC + hidden_sherpa,
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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=IDENTITY,
    entitlements_file=ENTITLEMENTS,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Wavetype")
app = BUNDLE(
    coll,
    name="Wavetype.app",
    icon=ICNS if os.path.exists(ICNS) else None,
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": "Wavetype",
        "CFBundleDisplayName": "Wavetype",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "14.0",
        "LSUIElement": True,                     # menu-bar style agent: no Dock icon
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHumanReadableCopyright": "MIT License",
        "NSMicrophoneUsageDescription": "Wavetype turns your voice into text in any app.",
    },
)
