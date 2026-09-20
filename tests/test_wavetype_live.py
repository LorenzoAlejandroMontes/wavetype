"""
Banco della logica live DENTRO wavetype.py, senza microfono, senza finestre, senza rete.

Il pannello e il motore sono sostituiti da stub che registrano le chiamate (l'API del pannello
e' quella concordata: STYLES, load_style, save_style, set_style, set_phase, set_level,
preview_style). Si guida il vero codice di wavetype.py: live_start, audio_cb, live_tick,
stop_and_process, _process, live_pasted, live_end, style_cycle, start_capture.

Uso:
  .venv/Scripts/python.exe tests/test_wavetype_live.py
"""
import os
import sys
import tempfile
import time

import httpx
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
import wavetype                                               # noqa: E402

LOGS = []
wavetype.log = lambda m: LOGS.append(str(m))
wavetype.beep = lambda *a, **k: None


# ---------- stub ----------
class StubPanel:
    def __init__(self, log=print):
        self.calls = []
        self.level = None
        self._open = False

    def open(self, hwnd):
        self.calls.append(("open", hwnd))
        self._open = True

    def open_edit(self, hwnd, sel_words=0):
        self.calls.append(("open_edit", (hwnd, sel_words)))
        self._open = True

    def open_recover(self, hwnd, dur=0.0):
        self.calls.append(("open_recover", (hwnd, round(float(dur), 2))))
        self._open = True

    def set_chip(self, key):
        self.calls.append(("chip", key))

    def set_result(self, words=0, chip=None):
        self.calls.append(("result", (words, chip)))

    def close(self, pasted=True):
        self.calls.append(("close", pasted))
        self._open = False

    def is_open(self):
        return self._open

    def holds_hud(self):
        return self._open

    def update(self, snap):
        self.calls.append(("update", snap.get("committed")))

    def tick(self):
        pass

    def destroy(self):
        self._open = False

    def set_style(self, name):
        self.calls.append(("style", name))

    def set_phase(self, p):
        self.calls.append(("phase", p))

    def set_level(self, x):
        self.level = x

    def preview_style(self, name):
        self.calls.append(("preview", name))
        self._open = True

    # utilita' del banco
    def phases(self):
        return [a for k, a in self.calls if k == "phase"]

    def of(self, kind):
        return [a for k, a in self.calls if k == kind]


class StubPanelMod:
    STYLES = ("stamp", "glyph", "signal")
    saved = "stamp"
    LivePanel = StubPanel

    @classmethod
    def load_style(cls):
        return cls.saved

    @classmethod
    def save_style(cls, name):
        cls.saved = name


class StubEngine:
    def __init__(self, *a, **kw):
        self.args, self.kw = a, kw
        self.snap = {"committed": "", "tentative": "", "rev": 0}
        self.started = self.cancelled = 0

    def start(self):
        self.started += 1

    def feed(self, block):
        pass

    def snapshot(self):
        return dict(self.snap)

    def stop(self, timeout=30):
        return None                      # come live_local: il testo finale e' del batch

    def cancel(self):
        self.cancelled += 1


class StubEngineMod:
    LiveEngine = StubEngine
    fail = None

    @classmethod
    def load_backend(cls):
        if cls.fail:
            raise cls.fail
        return object()


def reset():
    wavetype.live_panel = StubPanelMod
    wavetype.live_engine = StubEngineMod
    StubEngineMod.fail = None
    StubPanelMod.saved = "stamp"
    wavetype.live.update({"engine": None, "panel": None, "on": False, "open": False,
                       "expect_paste": False, "final": False, "close_at": 0.0,
                       "close_pasted": True, "ui": True, "cmds": [], "phase": "",
                       "ending": False, "level": 0.0, "voice_t": 0.0, "text_t": 0.0,
                       "rev": None, "engine_err": None, "edit": False, "recover": False,
                       "hwnd": 0})
    wavetype.rec["held"] = False
    wavetype.rec["edit"] = False
    wavetype.rec["chip"] = None
    wavetype.flags["cancel"] = False
    del wavetype.insert_jobs[:]
    del LOGS[:]


def started_card(hwnd=7):
    reset()
    wavetype.rec["held"] = True
    wavetype.live_start(hwnd)
    wavetype.live_tick()
    return wavetype.live["panel"]


def voice_block(amp=0.05, n=1024):
    t = np.arange(n) / wavetype.REC_SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype("float32").reshape(-1, 1)


# ---------- motore ----------
def test_motore_locale_di_default():
    """Il motore dell'anteprima e' live_local e non riceve la chiave Groq."""
    assert wavetype.LIVE_ENGINE == "local", wavetype.LIVE_ENGINE
    import live_local
    reset()
    wavetype.live_engine = live_local
    orig = live_local.LiveEngine
    got = {}

    class Spy:
        def __init__(self, *a, **kw):
            got["a"], got["kw"] = a, kw
    live_local.LiveEngine = Spy
    try:
        wavetype.live_make_engine()
    finally:
        live_local.LiveEngine = orig
    assert got["a"] == () and "groq_key" not in got["kw"], got


def test_live_local_non_usa_la_rete():
    src = open(os.path.join(ROOT, "live_local.py"), encoding="utf-8").read()
    for bad in ("import httpx", "import requests", "urllib", "api.groq.com"):
        assert bad not in src, bad


def test_modulo_importato_e_live_local():
    import live_local
    # wavetype.live_engine qui e' lo stub dei test: si guarda il sorgente dell'import
    src = open(os.path.join(ROOT, "wavetype.py"), encoding="utf-8").read()
    assert "import live_local as live_engine" in src
    assert live_local.LiveEngine(log=lambda *a: None, backend="__nessuno__").stop() is None


def test_precarico_fallito_card_senza_parole():
    reset()
    StubEngineMod.fail = RuntimeError("modello assente")
    wavetype.live_preload()
    assert wavetype.live["engine_err"] is not None
    assert any("non caricato" in m for m in LOGS), LOGS
    assert not wavetype.engine_ready()
    wavetype.rec["held"] = True
    wavetype.live_start(5)
    wavetype.live_tick()
    p = wavetype.live["panel"]
    assert p is not None and p.of("open") == [5] and p.phases() == ["listening"]
    assert wavetype.live["on"] is False and wavetype.live["engine"] is None


def test_precarico_riuscito():
    reset()
    wavetype.live_preload()
    assert wavetype.live["engine_err"] is None and wavetype.engine_ready()
    assert any("pronto" in m for m in LOGS), LOGS


# ---------- livello e fasi ----------
def test_livello():
    f = wavetype.level_from_rms
    assert f(0) == 0.0 and f(0.001) == 0.0 and f(0.5) == 1.0
    assert abs(f(0.01) - 0.5) < 1e-6
    assert f(0.1) == 1.0


def test_fase_da_registrazione():
    f = wavetype.rec_phase
    assert f(10.0, 0.0) == "listening"
    assert f(10.0, 8.0) == "live"
    assert f(10.0, 7.0) == "paused"        # 3 s esatti di silenzio


def test_ciclo_dettatura_completo():
    p = started_card()
    assert p.of("open") == [7] and p.phases() == ["listening"], p.calls
    assert wavetype.live["on"] and wavetype.live["engine"].started == 1
    # voce nel mic -> livello e fase live
    wavetype.audio_cb(voice_block(), 1024, None, None)
    assert wavetype.live["level"] > 0.5, wavetype.live["level"]
    wavetype.live_tick()
    assert p.phases()[-1] == "live" and p.level > 0.5
    # 3,5 s senza voce e senza parole -> paused
    wavetype.live["voice_t"] = time.perf_counter() - 3.5
    wavetype.live_tick()
    assert p.phases()[-1] == "paused"
    # parole nuove dal motore -> di nuovo live
    wavetype.live["engine"].snap = {"committed": "ciao", "tentative": "mondo", "rev": 3}
    wavetype.live_tick()
    assert p.phases()[-1] == "live" and ("update", "ciao") in p.calls
    # silenzio nel mic non tocca voice_t
    vt = wavetype.live["voice_t"]
    wavetype.audio_cb(np.zeros((1024, 1), dtype="float32"), 1024, None, None)
    assert wavetype.live["voice_t"] == vt and wavetype.live["level"] == 0.0
    # stop -> formatting (il _process vero qui non serve)
    orig = wavetype._process
    wavetype._process = lambda *a, **k: None

    class S:
        def stop(self):
            pass

        def close(self):
            pass
    wavetype.rec["stream"] = S()
    try:
        wavetype.stop_and_process()
        time.sleep(0.05)
    finally:
        wavetype._process = orig
    wavetype.live_tick()
    assert p.phases()[-1] == "formatting" and p.level == 0.0, p.calls
    # testo finale + incolla -> inserted, poi chiusura dopo LIVE_FINAL_HOLD
    wavetype.live_show_final("Ciao mondo, tutto bene.")
    wavetype.live["expect_paste"] = True
    wavetype.live_pasted()
    wavetype.live_tick()
    assert p.phases()[-1] == "inserted" and ("update", "Ciao mondo, tutto bene.") in p.calls
    assert p.is_open() and not p.of("close")
    wavetype.live["close_at"] = time.perf_counter() - 0.01
    wavetype.live_tick()
    assert p.of("close") == [True] and wavetype.live["open"] is False


def test_esc_mostra_annullato_e_poi_chiude():
    p = started_card()
    wavetype.live_end("cancelled", wavetype.LIVE_CANCEL_HOLD)
    wavetype.live_cancel()                    # come il finally di _process: non deve chiudere prima
    wavetype.live_tick()
    assert p.phases()[-1] == "cancelled" and not p.of("close"), p.calls
    assert wavetype.live["engine"].cancelled >= 1
    assert wavetype.live["close_at"] > time.perf_counter() + 0.3
    wavetype.live["close_at"] = time.perf_counter() - 0.01
    wavetype.live_tick()
    assert p.of("close") == [False]
    # la fase di registrazione non sovrascrive l'esito
    assert "live" not in p.phases()[p.phases().index("cancelled"):]


def test_rete_giu_mostra_offline_e_non_incolla():
    p = started_card()
    tmp = tempfile.mkdtemp(prefix="wavetype-test-")
    saved = {k: getattr(wavetype, k) for k in ("REC_DIR", "WAV_OUT", "HISTORY", "USE_GROQ",
                                              "GROQ_TRIES", "groq_transcribe", "model")}
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("prova: rete giu'")
    try:
        wavetype.REC_DIR = os.path.join(tmp, "rec")
        wavetype.WAV_OUT = os.path.join(tmp, "last.wav")
        wavetype.HISTORY = os.path.join(tmp, "hist.txt")
        wavetype.USE_GROQ, wavetype.GROQ_TRIES, wavetype.model = True, 1, None
        wavetype.groq_transcribe = down
        wavetype.rec["held"] = False
        frames = [voice_block(n=wavetype.REC_SR // 4) for _ in range(4)]     # 1 s di "voce"
        wavetype._process(frames, 0)
    finally:
        for k, v in saved.items():
            setattr(wavetype, k, v)
    wavetype.live_tick()
    assert calls, "groq_transcribe non chiamato"
    assert p.phases()[-1] == "offline", p.calls
    assert not p.of("close") and not wavetype.insert_jobs
    assert wavetype.live["close_at"] > time.perf_counter() + 1.5
    assert os.listdir(os.path.join(tmp, "rec")), "audio non archiviato"


def test_stt_ok_non_e_offline():
    reset()
    saved = (wavetype.USE_GROQ, wavetype.groq_transcribe)
    try:
        wavetype.USE_GROQ = True
        wavetype.groq_transcribe = lambda *a, **k: ("ciao", "it")
        wavetype.stt("x.wav", np.zeros(10, dtype="float32"), time.perf_counter())
        assert not wavetype.stt_net_failed()
    finally:
        wavetype.USE_GROQ, wavetype.groq_transcribe = saved


def test_corta_si_chiude_senza_esito():
    p = started_card()
    wavetype.rec["held"] = False
    wavetype._process([voice_block(n=1000)], 0)          # 0,02 s: sotto MIN_SEC
    wavetype.live_tick()
    assert p.of("close") == [False], p.calls


# ---------- stile ----------
def test_prossimo_stile():
    s = ("stamp", "glyph", "signal")
    assert wavetype.next_style("stamp", s) == "glyph"
    assert wavetype.next_style("glyph", s) == "signal"
    assert wavetype.next_style("signal", s) == "stamp"
    assert wavetype.next_style("boh", s) == "stamp"


def test_stile_anteprima_senza_dettatura():
    reset()
    assert wavetype.style_cycle() == "glyph"
    assert StubPanelMod.saved == "glyph"
    assert any("[stile] glyph" in m for m in LOGS), LOGS
    wavetype.live_tick()
    p = wavetype.live["panel"]
    assert ("style", "glyph") in p.calls and p.of("preview") == ["glyph"], p.calls
    wavetype.style_cycle()
    wavetype.style_cycle()
    assert StubPanelMod.saved == "stamp"


def test_stile_durante_dettatura_niente_anteprima():
    p = started_card()
    wavetype.style_cycle()
    wavetype.live_tick()
    assert p.of("style")[-1] == "glyph" and not p.of("preview"), p.calls


def test_win_ctrl_t_non_avvia_la_dettatura():
    reset()
    import win32con
    down = {win32con.VK_CONTROL, win32con.VK_LWIN, wavetype.VK_T}
    saved = (wavetype._down, wavetype.copy_selection, wavetype.start_rec)
    wavetype._down = lambda vk: vk in down

    def boom(*a, **k):
        raise AssertionError("non doveva registrare")
    wavetype.copy_selection = wavetype.start_rec = boom
    try:
        t0 = time.perf_counter()
        assert wavetype.start_capture() == "T"
        assert time.perf_counter() - t0 < 0.3          # esce subito, non aspetta 0,7 s
        down.clear()
        assert wavetype._wait_modifiers_up() is None      # senza watch: come prima
    finally:
        wavetype._down, wavetype.copy_selection, wavetype.start_rec = saved
    assert wavetype.rec["held"] is False


def test_chord_style_annulla_registrazione_appena_partita():
    p = started_card()

    class S:
        def stop(self):
            pass

        def close(self):
            pass
    wavetype.rec.update({"stream": S(), "t0": time.perf_counter(), "frames": []})
    wavetype.ui["state"] = "rec"
    wavetype.chord_style()
    wavetype.live_tick()
    assert wavetype.rec["held"] is False and wavetype.ui["state"] == "idle"
    assert p.of("close") == [False] and StubPanelMod.saved == "glyph"


class FakeSD:
    class InputStream:
        def __init__(self, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    @staticmethod
    def query_devices(kind=None):
        return {"name": "finto"}


def edit_card(sel="uno due tre quattro"):
    """Edit avviato come da Win+Ctrl su una selezione: card aperta e disegnata una volta."""
    reset()
    saved = wavetype.sd
    wavetype.sd = FakeSD
    try:
        wavetype.start_rec(edit=True, sel=sel)
        wavetype.live_tick()
    finally:
        wavetype.sd = saved
    return wavetype.live["panel"]


def test_edit_apre_la_card_e_nasconde_l_hud():
    """Edit Mode ha la sua card (tre comandi, numero di parole): e' l'unico indicatore, l'HUD
    resta nascosto come in dettatura. Il motore locale parte per scrivere l'istruzione."""
    p = edit_card()
    try:
        assert [w for _h, w in p.of("open_edit")] == [4], p.calls
        assert wavetype.live["open"] and wavetype.live["edit"]
        assert wavetype.live_panel_holds_hud()
        assert wavetype.live["on"] and wavetype.live["engine"].started == 1
        wavetype.live["engine"].snap = {"committed": "in inglese", "tentative": "", "rev": 1}
        wavetype.live_tick()
        assert "in inglese" in p.of("update")                   # l'istruzione a schermo mentre la dici
        wavetype.live["voice_t"] = time.perf_counter() - 10        # nessuna fase "paused" in Edit
        wavetype.live_tick()
        assert "paused" not in p.phases() and "live" not in p.phases()
    finally:
        wavetype.rec["held"] = False
        wavetype.ui["state"] = "idle"


def test_edit_stop_va_in_rewriting():
    p = edit_card()
    saved = wavetype.threading.Thread
    wavetype.threading.Thread = lambda *a, **k: type("T", (), {"start": lambda self: None})()
    try:
        wavetype.stop_and_process()
        wavetype.live_tick()
    finally:
        wavetype.threading.Thread = saved
        wavetype.ui["state"] = "idle"
    assert p.phases()[-1] == "rewriting", p.phases()


def test_edit_tasto_sceglie_il_chip_senza_parlare():
    """Tasto 2 durante Edit: la card blocca English, niente trascrizione, a EDIT_PROMPT va il
    testo d'istruzione del chip; esito "done" con la pillola del chip e incolla."""
    p = edit_card("ciao marco come stai")
    got = {}
    saved = (wavetype.edit_transform, wavetype.stt)
    wavetype.edit_transform = lambda sel, instr: got.update(sel=sel, instr=instr) or "hi marco how are you"
    wavetype.stt = lambda *a, **k: (_ for _ in ()).throw(AssertionError("stt chiamata"))
    try:
        assert wavetype._chip_key("2") is False                    # False = il tasto non arriva all'app
        assert wavetype.rec["chip"] == "english"
        wavetype.live_tick()
        assert p.of("chip") == ["2"]
        wavetype.rec["held"] = False
        wavetype._process([], 5, True, "ciao marco come stai", "english")
        wavetype.live_tick()
    finally:
        wavetype.edit_transform, wavetype.stt = saved
        wavetype.ui["state"] = "idle"
    assert got["instr"] == wavetype.edit_chips.instruction("english"), got
    assert p.of("result") == [(5, "english")] and p.phases()[-1] == "done", p.calls
    assert wavetype.insert_jobs == [(5, "hi marco how are you")]
    wavetype.live_pasted()
    assert wavetype.live["close_at"] > 0 and p.phases()[-1] == "done"   # niente "inserted" in Edit


def test_edit_chip_detto_e_istruzione_libera():
    reset()
    instr, cid = wavetype.edit_instruction("correggi la grammatica")
    assert cid == "grammar" and instr == wavetype.edit_chips.instruction("grammar")
    instr, cid = wavetype.edit_instruction("rendilo piu' corto e togli i nomi")
    assert cid is None and instr == "rendilo piu' corto e togli i nomi"


def test_edit_tasti_fuori_da_edit_non_fanno_nulla():
    reset()
    wavetype.rec["held"] = True                                    # dettatura, non Edit
    assert wavetype._chip_key("1") is False
    assert wavetype.rec["chip"] is None
    wavetype.rec["held"] = False


def test_edit_none_mostra_offline():
    p = edit_card()
    saved = wavetype.edit_transform
    wavetype.edit_transform = lambda sel, instr: None
    wavetype.rec["held"] = False
    try:
        wavetype._finish("in inglese", "it", 0, True, "ciao", time.perf_counter())
        wavetype.live_tick()
    finally:
        wavetype.edit_transform = saved
        wavetype.ui["state"] = "idle"
    assert p.phases()[-1] == "offline" and wavetype.insert_jobs == []
    assert wavetype.live["close_at"] > 0


def test_comando_edit_senza_selezione_mostra_nothing():
    """"Correggi la grammatica" detto senza testo selezionato: non si incolla come dettatura,
    la card diventa quella Edit e dice "nothing selected"."""
    p = started_card()
    wavetype.rec["held"] = False
    saved = wavetype.format_text
    wavetype.format_text = lambda t, l: (_ for _ in ()).throw(AssertionError("formattato"))
    try:
        wavetype._finish("Correggi la grammatica.", "it", 7, False, "", time.perf_counter())
        wavetype.live_tick()
    finally:
        wavetype.format_text = saved
    assert wavetype.insert_jobs == []
    assert p.of("open_edit") == [(7, 0)] and p.phases()[-1] == "nothing", p.calls
    assert wavetype.live["close_at"] > 0


def test_edit_fallito_non_rincolla_la_selezione():
    """Groq e modello locale giu': prima la selezione veniva rincollata identica, in silenzio.
    Ora niente incolla, una riga nel log e il bip d'errore."""
    reset()
    saved = (wavetype.edit_transform, wavetype.beep)
    beeps = []
    wavetype.edit_transform = lambda sel, instr: None
    wavetype.beep = lambda *a, **k: beeps.append(a)
    del wavetype.insert_jobs[:]
    try:
        wavetype._finish("correggi la grammatica", "it", 0, True, "ciao marco", time.perf_counter())
    finally:
        wavetype.edit_transform, wavetype.beep = saved
    assert wavetype.insert_jobs == [], wavetype.insert_jobs
    assert beeps and any("[edit] non riuscito" in m for m in LOGS)


def test_edit_transform_torna_none_se_tutto_fallisce():
    saved = (wavetype.USE_GROQ, wavetype.groq_retry, wavetype.urllib.request.urlopen)

    def boom(*a, **k):
        raise OSError("giu'")
    wavetype.USE_GROQ, wavetype.groq_retry, wavetype.urllib.request.urlopen = True, boom, boom
    try:
        assert wavetype.edit_transform("ciao marco", "in inglese") is None
    finally:
        wavetype.USE_GROQ, wavetype.groq_retry, wavetype.urllib.request.urlopen = saved


def test_recupero_salta_le_istruzioni_edit():
    """Win+Ctrl+R dopo un Edit: l'ultima registrazione e' l'istruzione detta, non una dettatura."""
    saved = wavetype.REC_DIR
    with tempfile.TemporaryDirectory() as d:
        wavetype.REC_DIR = d
        try:
            dett = os.path.join(d, "20260919-100000.wav")
            open(dett, "wb").close()
            os.utime(dett, (time.time() - 60, time.time() - 60))
            ed = wavetype.archive_path(edit=True)
            assert os.path.basename(ed).startswith(wavetype.EDIT_PREFIX)
            open(ed, "wb").close()
            assert wavetype.last_recording() == dett
            assert not os.path.basename(wavetype.archive_path()).startswith(wavetype.EDIT_PREFIX)
        finally:
            wavetype.REC_DIR = saved


def test_hud_spento_mai_da_fermo():
    """HUD_ON spento: da fermo e all'avvio l'HUD non e' mai voluto, in nessuna skin; resta solo la
    rete di sicurezza mentre registri o elabori senza card. Riacceso torna com'era."""
    saved = (wavetype.HUD_ON, wavetype.SKIN, wavetype.flags["dismiss"])
    try:
        wavetype.HUD_ON = False
        for skin in wavetype.SKINS:
            wavetype.SKIN = skin
            assert wavetype.hud_want(False, True, True) is False, skin      # fermo, appena avviato
            assert wavetype.hud_want(False, False, False) is False, skin
            assert wavetype.hud_want(True, False, False) is True, skin       # rete di sicurezza
        wavetype.HUD_ON = True
        wavetype.flags["dismiss"] = False
        wavetype.SKIN = "face"
        assert wavetype.hud_want(False, False, False) is True                # faccina sempre visibile
        wavetype.SKIN = "dog"
        assert wavetype.hud_want(False, False, False) is False
        assert wavetype.hud_want(False, False, True) is True                 # i 4 s d'avvio
    finally:
        wavetype.HUD_ON, wavetype.SKIN, wavetype.flags["dismiss"] = saved


# ---------- recupero (Win+Ctrl+R) ----------
def _wav(path, sec=2.0, sr=16000):
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * sec))
    return path


def _recupero(text, sec=2.0, archivio=True):
    """Fa girare reprocess_last() col vero codice di wavetype: torna (pannello, cartella)."""
    reset()
    wavetype.live["ui"] = True
    tmp = tempfile.mkdtemp(prefix="wavetype-rec-")
    saved = {k: getattr(wavetype, k) for k in ("REC_DIR", "HISTORY", "stt", "_finish")}
    try:
        wavetype.REC_DIR = os.path.join(tmp, "rec")
        os.makedirs(wavetype.REC_DIR)
        wavetype.HISTORY = os.path.join(tmp, "hist.txt")
        if archivio:
            _wav(os.path.join(wavetype.REC_DIR, "20260920-120000.wav"), sec)
        wavetype.stt = lambda path, a16, t0: (text, "it")
        def _fin(*a, **k):
            wavetype.live["expect_paste"] = wavetype.live["open"]
            wavetype.insert_jobs.append((a[2], a[0]))
        wavetype._finish = _fin
        wavetype.reprocess_last(7)
    finally:
        for k, v in saved.items():
            setattr(wavetype, k, v)
    wavetype.live_tick()
    return wavetype.live["panel"], tmp


def test_recupero_apre_la_card_con_la_durata():
    p, _ = _recupero("Ciao Marco, il preventivo e' pronto.", sec=2.0)
    assert p.of("open_recover") == [(7, 2.0)], p.calls
    assert p.phases()[0] == "recovering", p.calls
    assert wavetype.live["recover"] is True
    assert wavetype.insert_jobs, "il testo recuperato non e' stato incollato"


def test_recupero_incollato_mostra_recovered():
    p, _ = _recupero("Ciao Marco.", sec=3.0)
    wavetype.live_show_final("Ciao Marco.")
    wavetype.live_pasted()
    wavetype.live_tick()
    assert p.phases()[-1] == "recovered", p.calls
    assert ("update", "Ciao Marco.") in p.calls
    assert wavetype.live["close_at"] > time.perf_counter() + 1.0     # LIVE_RECOVERED_HOLD
    assert not p.of("close")


def test_recupero_senza_archivio_mostra_no_audio():
    p, _ = _recupero("qualcosa", archivio=False)
    assert p.of("open_recover") == [(7, 0.0)], p.calls
    assert p.phases()[-1] == "no_audio", p.calls
    assert not wavetype.insert_jobs
    assert wavetype.live["close_at"] > time.perf_counter() + 1.5     # LIVE_RECOVER_HOLD


def test_recupero_testo_vuoto_dopo_formattazione_non_lascia_la_card_appesa():
    """Trascritto c'e' ma la formattazione torna EMPTY: _finish non incolla e non tocca la card,
    il recupero deve comunque chiudere con un esito (visto nell'e2e del 20/09 su un audio muto)."""
    reset()
    wavetype.live["ui"] = True
    tmp = tempfile.mkdtemp(prefix="wavetype-rec-")
    saved = {k: getattr(wavetype, k) for k in ("REC_DIR", "HISTORY", "stt", "format_text")}
    try:
        wavetype.REC_DIR = os.path.join(tmp, "rec")
        os.makedirs(wavetype.REC_DIR)
        wavetype.HISTORY = os.path.join(tmp, "hist.txt")
        _wav(os.path.join(wavetype.REC_DIR, "20260920-130000.wav"), 3.0)
        wavetype.stt = lambda path, a16, t0: (".", "it")
        wavetype.format_text = lambda text, lang: "EMPTY"
        wavetype.reprocess_last(7)
    finally:
        for k, v in saved.items():
            setattr(wavetype, k, v)
    wavetype.live_tick()
    p = wavetype.live["panel"]
    assert p.phases()[-1] == "unrecovered", p.calls
    assert not wavetype.insert_jobs
    assert wavetype.live["close_at"] > time.perf_counter() + 1.5


def test_recupero_senza_testo_mostra_unrecovered():
    p, _ = _recupero("", sec=2.0)
    assert p.phases()[-1] == "unrecovered", p.calls
    assert not wavetype.insert_jobs


def test_una_dettatura_dopo_il_recupero_torna_normale():
    """La card del recupero non lascia lo stato acceso: la dettatura dopo fa "inserted"."""
    _recupero("Ciao.", sec=1.0)
    p = started_card()
    assert wavetype.live["recover"] is False
    wavetype.rec["held"] = False
    wavetype.live_show_final("Ciao mondo.")
    wavetype.live_pasted()
    wavetype.live_tick()
    assert p.phases()[-1] == "inserted", p.calls


# ---------- contesto: il cursore a meta' di una frase gia' scritta ----------
def _detta(ctx, formattato="Domani arrivo presto.", acceso=True):
    """Fa girare il vero _finish() con un contesto dato: torna (incollato, riga di storico)."""
    reset()
    tmp = tempfile.mkdtemp(prefix="wavetype-ctx-")
    hist = os.path.join(tmp, "hist.txt")
    saved = {k: getattr(wavetype, k) for k in ("HISTORY", "format_text", "CONTEXT_ON")}
    try:
        wavetype.HISTORY = hist
        wavetype.format_text = lambda text, lang: formattato
        wavetype.CONTEXT_ON = acceso
        wavetype._finish("grezzo", "it", 7, False, "", time.perf_counter(), ctx=ctx)
    finally:
        for k, v in saved.items():
            setattr(wavetype, k, v)
    riga = open(hist, encoding="utf-8").read() if os.path.exists(hist) else ""
    return (wavetype.insert_jobs[-1][1] if wavetype.insert_jobs else None), riga


def test_ctx_meta_frase_abbassa_e_spazia():
    out, _ = _detta("quindi volevo dirti che")
    assert out == " domani arrivo presto.", out


def test_ctx_dopo_il_punto_resta_maiuscolo():
    out, _ = _detta("Ho finito il lavoro. ")
    assert out == "Domani arrivo presto.", out


def test_ctx_sconosciuto_si_comporta_come_prima():
    out, _ = _detta(None)
    assert out == "Domani arrivo presto.", out


def test_ctx_spento_non_tocca_niente():
    out, _ = _detta("quindi volevo dirti che", acceso=False)
    assert out == "Domani arrivo presto.", out


def test_ctx_lo_storico_tiene_il_testo_formattato_puro():
    """Il replay confronta OUT: con la baseline: l'adattamento al cursore non ci entra."""
    out, riga = _detta("quindi volevo dirti che")
    assert "OUT:Domani arrivo presto." in riga, riga
    assert out.startswith(" domani"), out


def test_ctx_letto_in_dettatura_e_non_rubato_da_quella_dopo():
    reset()
    saved = wavetype.ctx_mod
    try:
        class FintoCtx:
            testo = "quindi volevo dirti che"
            @staticmethod
            def before_caret(hwnd, budget=None):
                return FintoCtx.testo
            starts_sentence = staticmethod(lambda prev: wavetype.ctx_mod is None)
        wavetype.ctx_mod = FintoCtx
        wavetype.rec["t0"] = 100.0
        wavetype.read_context(7, 100.0)
        assert wavetype.rec["ctx"] == "quindi volevo dirti che", wavetype.rec["ctx"]
        wavetype.rec["ctx"] = None
        wavetype.rec["t0"] = 200.0                 # e' partita un'altra dettatura
        wavetype.read_context(7, 100.0)
        assert wavetype.rec["ctx"] is None, wavetype.rec["ctx"]
    finally:
        wavetype.ctx_mod = saved


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    bad = 0
    for n, f in tests:
        try:
            f()
            print(f"ok   {n}")
        except Exception as ex:
            bad += 1
            print(f"FAIL {n}: {ex!r}")
    print(f"{len(tests) - bad}/{len(tests)} ok" if not bad else f"{bad} falliti")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
