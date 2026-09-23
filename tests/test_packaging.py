"""
tests/test_packaging.py — cartelle dei dati (paths.py) e prova della chiave (first_run.py).

  - dal sorgente ogni percorso resta quello di sempre (relativo, o accanto al modulo);
  - da eseguibile (sys.frozen) le impostazioni vanno in %APPDATA%\\Wavetype, i dati pesanti in
    %LOCALAPPDATA%\\Wavetype, gli asset nel pacchetto;
  - una chiave che non ha la forma gsk_ non parte nemmeno verso Groq;
  - con groq_key.txt presente: la chiave vera passa, una inventata viene rifiutata (401).
"""
import importlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import paths  # noqa: E402

RESULTS = []


def case(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, None))
        print(f"ok   {fn.__name__}")
    except Exception as e:
        RESULTS.append((fn.__name__, e))
        print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    return fn


def _frozen(tmp):
    """paths ricaricato come se girasse nell'eseguibile, con APPDATA/LOCALAPPDATA finti."""
    old = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA")}
    os.environ["APPDATA"] = os.path.join(tmp, "Roaming")
    os.environ["LOCALAPPDATA"] = os.path.join(tmp, "Local")
    sys.frozen = True
    sys._MEIPASS = os.path.join(tmp, "bundle")
    try:
        return importlib.reload(paths)
    finally:
        del sys.frozen
        del sys._MEIPASS
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@case
def test_sorgente_percorsi_di_sempre():
    p = importlib.reload(paths)
    assert not p.FROZEN
    assert p.config("groq_key.txt") == "groq_key.txt"
    assert p.state("recordings") == "recordings"
    assert p.config("card_style.txt", "C:\\x") == os.path.join("C:\\x", "card_style.txt")
    assert p.resource("assets/pose-1-attesa.png") == "assets/pose-1-attesa.png"


@case
def test_sorgente_moduli_invariati():
    import wavetype
    import live_panel
    import live_local
    assert wavetype.HISTORY == "wavetype_history.txt"
    assert wavetype.REC_DIR == "recordings"
    assert wavetype.LOG_FILE == "wavetype.log"
    assert wavetype.WAV_OUT == "last_rec.wav"
    assert live_panel.STYLE_FILE == os.path.join(live_panel.ROOT, "card_style.txt")
    assert live_local.MODELS_DIR == os.path.join(live_local.HERE, "models")


@case
def test_eseguibile_appdata_e_localappdata():
    with tempfile.TemporaryDirectory() as tmp:
        p = _frozen(tmp)
        assert p.FROZEN
        assert p.config("groq_key.txt") == os.path.join(tmp, "Roaming", "Wavetype", "groq_key.txt")
        assert p.config("card_style.txt", "C:\\ignorato") == os.path.join(tmp, "Roaming", "Wavetype",
                                                                         "card_style.txt")
        assert p.state("recordings") == os.path.join(tmp, "Local", "Wavetype", "recordings")
        assert p.state("models", "C:\\ignorato") == os.path.join(tmp, "Local", "Wavetype", "models")
        assert p.resource("assets") == os.path.join(tmp, "bundle", "assets")
        assert os.path.isdir(os.path.join(tmp, "Roaming", "Wavetype"))     # creata al primo uso
        assert os.path.isdir(os.path.join(tmp, "Local", "Wavetype"))
    importlib.reload(paths)


@case
def test_chiave_senza_forma_non_parte():
    import first_run
    for bad in ("", "  ", "sk-proj-abc", "gsk_short", "gsk_" + "x" * 10):
        assert first_run.validate_key(bad)[0] == "format", bad


@case
def test_salva_chiave_una_riga():
    import first_run
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "sub", "groq_key.txt")
        first_run.save_key("  gsk_" + "a" * 40 + "\n", dst)
        assert open(dst, encoding="utf-8").read() == "gsk_" + "a" * 40 + "\n"


@case
def test_groq_accetta_la_vera_e_rifiuta_la_finta():
    import first_run
    key = None
    for src in (os.path.join(ROOT, "groq_key.txt"),):
        if os.path.exists(src):
            key = open(src, encoding="utf-8").read().strip()
    if not key:
        print("     (nessuna groq_key.txt: salto la prova in rete)")
        return
    assert first_run.validate_key(key)[0] == "ok"
    assert first_run.validate_key("gsk_" + "Z" * 52)[0] == "invalid"


if __name__ == "__main__":
    ok = sum(1 for _, e in RESULTS if e is None)
    print(f"{ok}/{len(RESULTS)} ok")
    sys.exit(0 if ok == len(RESULTS) else 1)
