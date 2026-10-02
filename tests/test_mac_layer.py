"""
Banco dello strato macOS, nelle parti che non hanno bisogno di un Mac: gira uguale su Windows
e su macOS (CI), senza PyObjC, senza microfono, senza finestre.

Cosa prova:
  - mac_keys.Hybrid: tocco = toggle, tenuto >= 0,4 s = parla finche' tieni, reset (ESC & co.)
  - mac_keys.KeyState: modificatori solo da flagsChanged, VK di Windows -> tasti Mac,
    tasti 1-3 ingoiati in Edit, Ctrl+Option+Q/R/T ingoiati, istante vero del chord, Fn solo
    dal tasto 63, chord usato con un altro tasto, riallineo dopo un tap spento
  - mac_sys: keycode V/C fuori dal thread principale solo dalla cache, appunti salvati solo
    nei tipi noti, niente incolla se l'app bersaglio e' stata chiusa
  - mac_geom: pixel (origine in alto) <-> punti (origine in basso), Retina, schermi misti
  - testhooks: ricampionamento e blocchi del WAV finto, WavStream a tempo reale, eventi jsonl
  - paths e card_styles "come su darwin" (sys.platform finto, modulo ricaricato)
  - wavetype.worker_mac in un processo a parte con sys.platform = "darwin": il vero worker,
    con lo stato dei tasti scritto a mano al posto del tap

Uso:
  .venv/Scripts/python.exe tests/test_mac_layer.py
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mac_geom as G                                         # noqa: E402
import mac_keys as K                                         # noqa: E402
import testhooks as H                                        # noqa: E402


def eq(got, want, what=""):
    assert got == want, f"{what}: {got!r} != {want!r}"


def near(got, want, tol=1e-6, what=""):
    assert abs(got - want) <= tol, f"{what}: {got!r} !~ {want!r}"


# ------------------------------------------------------------------ Hybrid (tocco / tenuto)
def feed_all(h, seq):
    """seq = [(giu'?, istante)] -> eventi non nulli."""
    return [e for e in (h.feed(d, t) for d, t in seq) if e]


def test_tocco_breve_resta_in_ascolto_e_si_ferma_alla_seconda():
    h = K.Hybrid(0.4)
    eq(feed_all(h, [(True, 0.0), (False, 0.15)]), ["start"], "primo tocco")
    eq(h.state, "latched")
    eq(feed_all(h, [(False, 3.0), (True, 5.0)]), ["stop"], "secondo tocco")
    eq(feed_all(h, [(False, 5.1)]), [], "rilascio del secondo tocco")
    eq(h.state, "idle")


def test_tenuto_parla_finche_tieni():
    h = K.Hybrid(0.4)
    eq(feed_all(h, [(True, 0.0), (True, 0.3), (True, 2.0), (False, 2.5)]), ["start", "stop"])
    eq(h.state, "idle")


def test_soglia_esatta_e_tenuto():
    h = K.Hybrid(0.4)
    eq(feed_all(h, [(True, 0.0), (False, 0.4)]), ["start", "stop"], "0,4 s esatti = tenuto")


def test_reset_col_tasto_giu_ignora_il_rilascio():
    """ESC mentre tieni Fn: la registrazione e' annullata, rilasciare Fn non deve fare niente,
    la pressione dopo riparte da capo."""
    h = K.Hybrid(0.4)
    eq(feed_all(h, [(True, 0.0), (True, 1.0)]), ["start"])
    h.reset()
    eq(h.active(), False)
    eq(feed_all(h, [(False, 1.2)]), [], "rilascio dopo il reset")
    eq(feed_all(h, [(True, 2.0)]), ["start"], "nuova pressione")


def test_reset_in_ascolto_a_tasto_su():
    h = K.Hybrid(0.4)
    feed_all(h, [(True, 0.0), (False, 0.1)])
    h.reset()
    eq(h.state, "idle")
    eq(feed_all(h, [(True, 1.0)]), ["start"])


# ------------------------------------------------------------------ KeyState
def test_modificatori_solo_da_flagschanged():
    s = K.KeyState()
    s.handle(K.T_FLAGS, K.KC_FN, K.F_FN)
    assert s.fn() and s.chord()
    # il nostro Cmd+V sintetico porta solo Command: Fn tenuto non deve "rilasciarsi"
    s.handle(K.T_KEYDOWN, K.KC_V, K.F_CMD)
    s.handle(K.T_KEYUP, K.KC_V, K.F_CMD)
    assert s.fn(), "Fn perso per colpa di un keyDown sintetico"
    s.handle(K.T_FLAGS, K.KC_FN, 0)
    assert not s.chord()


def test_ctrl_option_e_vk_di_windows():
    s = K.KeyState()
    s.handle(K.T_FLAGS, 59, K.F_CTRL)
    assert not s.chord(), "solo Ctrl non basta"
    s.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT)
    assert s.chord() and s.ctrl_option()
    assert s.vk_down(0x11) and s.vk_down(0x12) and not s.vk_down(0x5B)   # Ctrl, Alt, Win
    s.handle(K.T_KEYDOWN, K.KC_ESC, K.F_CTRL | K.F_OPT)
    assert s.vk_down(0x1B) and not s.vk_down(0x51)
    s.handle(K.T_KEYUP, K.KC_ESC, 0)
    assert not s.vk_down(0x1B)


def test_lettere_del_chord_ingoiate_solo_con_ctrl_option():
    s = K.KeyState()
    assert s.handle(K.T_KEYDOWN, K.KC_R, 0) is False, "R da sola arriva all'app"
    s.handle(K.T_KEYUP, K.KC_R, 0)
    s.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT)
    assert s.handle(K.T_KEYDOWN, K.KC_R, K.F_CTRL | K.F_OPT) is True
    assert s.down(K.KC_R), "ingoiata ma vista dal worker"


def test_comandi_in_coda_anche_se_gia_rilasciati():
    s = K.KeyState()
    s.handle(K.T_KEYDOWN, K.KC_Q, 0)                      # Q da sola: testo, non comando
    s.handle(K.T_KEYUP, K.KC_Q, 0)
    eq(s.take_cmd(), None)
    s.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT)
    s.handle(K.T_KEYDOWN, K.KC_Q, K.F_CTRL | K.F_OPT)
    s.handle(K.T_KEYDOWN, K.KC_Q, K.F_CTRL | K.F_OPT, autorepeat=True)
    s.handle(K.T_KEYUP, K.KC_Q, K.F_CTRL | K.F_OPT)       # gia' su quando il worker guarda
    eq(s.take_cmd(), K.KC_Q, "il tocco breve resta in coda")
    eq(s.take_cmd(), None, "una volta sola, la ripetizione non conta")
    s.handle(K.T_KEYDOWN, K.KC_T, K.F_CTRL | K.F_OPT)
    s.resync(0, False)
    eq(s.take_cmd(), None, "tap spento e riacceso: coda svuotata")


def test_tasti_1_3_ingoiati_e_richiamati_una_volta():
    s = K.KeyState()
    got = []
    s.swallow = {K.KC_2: lambda kc: got.append(kc)}
    assert s.handle(K.T_KEYDOWN, K.KC_2, 0) is True
    assert s.handle(K.T_KEYDOWN, K.KC_2, 0, autorepeat=True) is True
    assert s.handle(K.T_KEYUP, K.KC_2, 0) is True
    eq(got, [K.KC_2], "callback solo sul primo keyDown")
    assert s.handle(K.T_KEYDOWN, K.KC_1, 0) is False, "1 non e' nella lista: passa"
    eq(sorted(K.DIGIT_KEYS), ["1", "2", "3"])


def test_tocco_misurato_sull_istante_del_tasto():
    """Fn toccato 0,1 s mentre la registrazione si apre lenta (AX, Bluetooth): il worker
    guarda di nuovo solo a 0,6 s, ma conta l'istante del tasto -> resta in ascolto."""
    s, h = K.KeyState(), K.Hybrid(0.4)
    s.handle(K.T_FLAGS, K.KC_FN, K.F_FN, now=10.0)
    eq(h.feed(*s.edge), "start")
    s.handle(K.T_FLAGS, K.KC_FN, 0, now=10.1)
    eq(s.edge, (False, 10.1))
    eq(h.feed(*s.edge), None, "guardato a 10,6 ma rilasciato a 10,1")
    eq(h.state, "latched")
    # un altro modificatore mentre Fn e' giu' non sposta l'istante del chord
    s.handle(K.T_FLAGS, K.KC_FN, K.F_FN, now=20.0)
    s.handle(K.T_FLAGS, 56, K.F_FN | K.F_SHIFT, now=20.3)
    eq(s.edge, (True, 20.0), "Shift non e' un cambio del chord")
    s.handle(K.T_FLAGS, K.KC_FN, 0, now=21.0)
    eq(s.edge, (False, 21.0))


def test_altro_tasto_col_chord_lo_segna():
    s = K.KeyState()
    s.handle(K.T_KEYDOWN, 117, 0)                         # Canc avanti senza chord: niente
    s.handle(K.T_KEYUP, 117, 0)
    eq(s.other, False, "tasto senza chord")
    s.handle(K.T_FLAGS, K.KC_FN, K.F_FN)
    s.handle(K.T_KEYDOWN, K.KC_ESC, K.F_FN)               # ESC ha il suo percorso
    s.handle(K.T_KEYDOWN, K.KC_Q, K.F_FN)                 # Fn+Q (nota rapida) non e' un comando
    eq(s.other, True, "Fn+Q")
    s.handle(K.T_FLAGS, K.KC_FN, 0)
    s.handle(K.T_FLAGS, K.KC_FN, K.F_FN)
    eq(s.other, False, "nuova pressione: si riparte da capo")
    s.swallow = {K.KC_2: lambda kc: None}
    s.handle(K.T_KEYDOWN, K.KC_2, K.F_FN)                 # Edit Mode: 2 e' un comando
    s.handle(K.T_KEYDOWN, 117, K.F_FN, autorepeat=True)   # ripetizione di un tasto gia' giu'
    eq(s.other, False, "ESC, cifre ingoiate, ripetizioni")
    s.handle(K.T_KEYDOWN, 124, K.F_FN)                    # Fn+freccia
    eq(s.other, True, "Fn+freccia")
    s.handle(K.T_FLAGS, K.KC_FN, 0)
    s.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT)
    s.handle(K.T_KEYDOWN, K.KC_R, K.F_CTRL | K.F_OPT)
    eq(s.other, False, "Ctrl+Option+R e' un comando")
    s.handle(K.T_KEYDOWN, 123, K.F_CTRL | K.F_OPT)
    eq(s.other, True, "Ctrl+Option+freccia")


def test_fn_solo_dal_tasto_63():
    """Il bit F_FN lo portano anche le frecce: Shift rilasciato con una freccia giu' non e' Fn."""
    s = K.KeyState()
    s.handle(K.T_FLAGS, 56, K.F_SHIFT | K.F_FN)           # Shift giu', freccia gia' giu'
    s.handle(K.T_FLAGS, 56, K.F_FN)                       # Shift su, freccia ancora giu'
    eq((s.fn(), s.chord()), (False, False), "nessun Fn fantasma")
    s.handle(K.T_FLAGS, K.KC_FN, K.F_FN)
    s.handle(K.T_FLAGS, 59, K.F_FN | K.F_CTRL)            # Ctrl in piu' non cambia Fn
    s.handle(K.T_FLAGS, 59, 0)                            # e un flag senza F_FN nemmeno
    eq(s.fn(), True, "Fn resta giu' finche' non arriva il suo rilascio")
    s.handle(K.T_FLAGS, K.KC_FN, 0)
    eq(s.chord(), False)


def test_riallineo_dopo_tap_spento():
    s, h = K.KeyState(), K.Hybrid(0.4)
    s.handle(K.T_KEYDOWN, K.KC_Q, 0)                      # il keyUp della q e' andato perso
    s.handle(K.T_FLAGS, K.KC_FN, K.F_FN, now=1.0)
    eq(h.feed(*s.edge), "start")
    h.feed(True, 1.6)                                     # tenuto: modo "parla finche' tieni"
    s.resync(K.F_CTRL | K.F_OPT, False, now=5.0)          # sistema: Ctrl+Option giu', Fn su
    eq(s.down(K.KC_Q), False, "tasti svuotati: Ctrl+Option non chiude l'app")
    eq((s.fn(), s.ctrl_option(), s.chord()), (False, True, True))
    eq(s.edge, (True, 1.0), "chord ancora giu' (ora da Ctrl+Option): nessun cambio")
    s.resync(0, False, now=6.0)                           # rilascio di Fn perso: si ferma
    eq(s.edge, (False, 6.0))
    eq(h.feed(*s.edge), "stop")


def test_keycode_fuori_dal_thread_principale_solo_dalla_cache():
    import threading
    import mac_sys
    calls = []
    real = mac_sys._letter_keycode_tis
    mac_sys._letter_keycode_tis = lambda letter, default: calls.append(letter) or 47
    saved = dict(mac_sys._keycodes)
    try:
        mac_sys._keycodes.clear()
        got = []
        th = threading.Thread(target=lambda: got.append(mac_sys._letter_keycode("c", 8)))
        th.start()
        th.join()
        eq((got, calls), ([8], []), "worker senza cache: posizione ANSI, niente TIS")
        mac_sys.refresh_keycodes()                         # thread principale (il tick)
        eq(calls, ["v", "c"])
        th = threading.Thread(target=lambda: got.append(mac_sys._letter_keycode("c", 8)))
        th.start()
        th.join()
        eq((got[-1], calls), (47, ["v", "c"]), "worker: dalla cache")
    finally:
        mac_sys._letter_keycode_tis = real
        mac_sys._keycodes.clear()
        mac_sys._keycodes.update(saved)


class _Item:
    def __init__(self, types):
        self._t, self.read = types, []

    def types(self):
        return list(self._t)

    def dataForType_(self, t):
        self.read.append(t)
        return b"x"


def test_snapshot_appunti_solo_tipi_noti():
    import mac_sys
    cells = _Item([mac_sys.STRING, "com.microsoft.Excel.sheet", "public.tiff", "public.html"])
    image = _Item(["public.png", "com.apple.flat-rtfd"])
    real = mac_sys._pb
    mac_sys._pb = lambda: type("PB", (), {"pasteboardItems": lambda self: [cells, image]})()
    try:
        snap = mac_sys.snapshot_clipboard()
    finally:
        mac_sys._pb = real
    eq(cells.read, [mac_sys.STRING, "public.html"], "celle: niente formato nativo, niente immagine")
    eq(image.read, ["public.png"], "immagine copiata: torna")
    eq([sorted(d) for d in snap], [["public.html", mac_sys.STRING], ["public.png"]])


def test_incolla_saltato_se_app_bersaglio_chiusa():
    import mac_sys
    sent, clip = [], []
    real = (mac_sys.activate, mac_sys.send_cmd, mac_sys.set_clipboard_text, mac_sys.log)
    mac_sys.activate = lambda pid: None
    mac_sys.send_cmd = lambda *a: sent.append(a)
    mac_sys.set_clipboard_text = lambda text, transient=False: clip.append((text, transient)) or 1
    mac_sys.log = lambda m: None
    try:
        eq(mac_sys.insert_text(4242, "ciao"), False)
    finally:
        mac_sys.activate, mac_sys.send_cmd, mac_sys.set_clipboard_text, mac_sys.log = real
    eq((sent, clip), ([], [("ciao", False)]), "niente Cmd+V, testo negli appunti da tenere")


# ------------------------------------------------------------------ mac_geom
# schermo principale Retina 1440x900 pt, menu bar 25 pt, Dock 70 pt in basso
MAIN = ((0, 0, 1440, 900), (0, 70, 1440, 805), 2.0)
# secondo schermo 1x 1920x1080 a destra, bordi alti allineati (in Cocoa y=-180)
RIGHT = ((1440, -180, 1920, 1080), (1440, -180, 1920, 1055), 1.0)
# terzo schermo 1x sopra al principale
ABOVE = ((0, 900, 1440, 900), (0, 900, 1440, 900), 1.0)


def test_ribalta_la_y_andata_e_ritorno():
    r = (10, 20, 300, 40)
    eq(G.rect_bl(G.rect_tl(r, 900), 900), tuple(float(v) for v in r))
    eq(G.rect_tl((0, 70, 1440, 805), 900), (0.0, 25.0, 1440.0, 805.0), "visibleFrame")


def test_retina_punti_pixel():
    x, y, i = G.pt_to_px(100, 50, [MAIN])
    eq((x, y, i), (200.0, 100.0, 0))
    eq(G.work_area_px(200, 100, [MAIN]), (0, 50, 2880, 1660))
    eq(G.dpi_px(200, 100, [MAIN]), 192)
    (rx, ry, rw, rh), i = G.rect_pt_to_px((100, 50, 2, 18), [MAIN])
    eq((rx, ry, rw, rh, i), (200.0, 100.0, 4.0, 36.0, 0))


def test_frame_della_finestra_origine_in_basso():
    # card a (200, 100) px, 400x200 px, su Retina -> punti (100, 50, 200, 100) dall'alto
    eq(G.window_frame(200, 100, 400, 200, [MAIN], 0), (100.0, 750.0, 200.0, 100.0))


def test_schermi_misti_con_suggerimento():
    scr = [MAIN, RIGHT]
    x, y, i = G.pt_to_px(2000, 500, scr)                 # caret sul secondo schermo
    eq((x, y, i), (2000.0, 500.0, 1))
    # (2000, 500) px cade anche nel principale (1000, 250 pt): il suggerimento decide
    eq(G.screen_for_px(2000, 500, scr), 0, "senza suggerimento: il primo")
    eq(G.screen_for_px(2000, 500, scr, hint=1), 1)
    eq(G.work_area_px(2000, 500, scr, hint=1), (1440, 25, 3360, 1080), "menu bar in alto")
    eq(G.dpi_px(2000, 500, scr, hint=1), 96)
    fx, fy, fw, fh = G.window_frame(2000, 500, 400, 100, scr, 1)
    eq((fx, fy, fw, fh), (2000.0, 300.0, 400.0, 100.0), "1x: niente divisione")


def test_schermo_sopra_al_principale():
    scr = [MAIN, ABOVE]
    x, y, i = G.pt_to_px(100, -450, scr)
    eq(i, 1)
    eq((x, y), (100.0, -450.0))
    eq(G.work_area_px(x, y, scr, hint=1), (0, -900, 1440, 0))


def test_punto_fuori_da_tutti_va_al_piu_vicino():
    eq(G.screen_for_pt(5000, 100, [MAIN, RIGHT]), 1)
    eq(G.screen_for_pt(-50, 100, [MAIN, RIGHT]), 0)
    eq(G.screen_for_px(10, 10, []), None)


# ------------------------------------------------------------------ testhooks: WAV finto
def _write_wav(path, a, sr, ch=1, sw=2):
    a = np.asarray(a, dtype="float64")
    if ch == 2:
        a = np.stack([a, a], axis=1).reshape(-1)
    if sw == 2:
        raw = (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
    elif sw == 1:
        raw = (np.clip(a, -1, 1) * 127 + 128).astype(np.uint8).tobytes()
    else:                                             # 24 bit
        v = (np.clip(a, -1, 1) * ((1 << 23) - 1)).astype("<i4")
        raw = b"".join(int(x).to_bytes(4, "little", signed=True)[:3] for x in v)
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(sr)
        w.writeframes(raw)


def test_ricampiona_lunghezza_e_frequenza():
    sr = 16000
    t = np.arange(sr) / sr
    a = np.sin(2 * np.pi * 440 * t).astype("float32")
    b = H.resample(a, sr, 48000)
    eq(len(b), 48000, "1 s resta 1 s")
    crossings = int(np.sum((b[:-1] < 0) & (b[1:] >= 0)))
    assert 438 <= crossings <= 442, crossings
    eq(len(H.resample(a, 48000, 48000)), len(a))
    eq(len(H.resample(np.zeros(0), 8000, 48000)), 0)


def test_wav_stereo_8_e_24_bit():
    sr = 22050
    a = 0.5 * np.sin(2 * np.pi * 300 * np.arange(sr // 10) / sr)
    with tempfile.TemporaryDirectory() as d:
        for ch, sw, tol in ((2, 2, 1e-3), (1, 1, 2e-2), (1, 3, 1e-5)):
            p = os.path.join(d, f"t{ch}{sw}.wav")
            _write_wav(p, a, sr, ch=ch, sw=sw)
            got, gsr = H.read_wav_any(p)
            eq(gsr, sr)
            eq(len(got), len(a), f"{ch}ch {sw * 8}bit")
            assert float(np.max(np.abs(got - a))) < tol, (ch, sw, float(np.max(np.abs(got - a))))


def test_blocchi_della_misura_dello_stream():
    bl = H.blocks(np.ones(1100, dtype="float32"), 512)
    eq([b.shape for b in bl], [(512, 1)] * 3)
    eq(float(bl[-1][76:].sum()), 0.0, "coda completata di zeri")
    eq(float(bl[-1][:76].sum()), 76.0)


def test_wavstream_a_tempo_reale_poi_silenzio():
    sr = 48000
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "voce.wav")
        _write_wav(p, 0.3 * np.ones(int(0.1 * 16000)), 16000)   # 0,1 s a 16 kHz
        got = []
        st = H.WavStream(p, sr, lambda b, n, ti, stt: got.append((time.perf_counter(), b, n)))
        t0 = time.perf_counter()
        st.start()
        time.sleep(0.40)
        st.stop()
        st.close()
    n = len(got)
    expect = 0.40 * sr / H.WAV_BLOCK                      # ~37 blocchi in 0,4 s
    assert 0.7 * expect <= n <= 1.15 * expect + 2, (n, expect)
    assert all(b.shape == (H.WAV_BLOCK, 1) and k == H.WAV_BLOCK for _t, b, k in got)
    voiced = int(np.ceil(0.1 * sr / H.WAV_BLOCK))         # 10 blocchi di voce, poi silenzio
    assert all(float(np.abs(b).max()) > 0.2 for _t, b, _k in got[:voiced - 1])
    assert all(float(np.abs(b).max()) == 0.0 for _t, b, _k in got[voiced + 1:])
    assert got[0][0] - t0 < 0.1, "il primo blocco arriva subito"


# ------------------------------------------------------------------ testhooks: eventi
def test_eventi_jsonl_e_card_ogni_mezzo_secondo():
    old = os.environ.pop(H.ENV_EVENTS, None)
    try:
        H.emit("ready")                                   # senza variabile: niente
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ev.jsonl")
            os.environ[H.ENV_EVENTS] = p
            H._card_last.clear()
            H.emit("hotkey", what="start")
            H.emit("pasted", chars=3, text="ciao"[:200])
            assert H.card(10, 20, 300, 80, "live", now=100.0)
            assert not H.card(10, 20, 300, 80, "live", now=100.3)
            assert H.card(10, 20, 300, 80, "formatting", now=100.3)
            assert H.card(11, 20, 300, 80, "live", now=100.6)
            H.emit("context", before=H.clip("x" * 100, 40))
            rows = [json.loads(ln) for ln in open(p, encoding="utf-8")]
        eq([r["ev"] for r in rows], ["hotkey", "pasted", "card", "card", "card", "context"])
        assert all(isinstance(r["t"], float) and r["t"] > 1.6e9 for r in rows)
        eq(rows[0]["what"], "start")
        eq({k: rows[2][k] for k in ("x", "y", "w", "h", "phase")},
           {"x": 10, "y": 20, "w": 300, "h": 80, "phase": "live"})
        eq(len(rows[5]["before"]), 40)
        eq(H.clip(None, 40), None)
    finally:
        os.environ.pop(H.ENV_EVENTS, None)
        if old is not None:
            os.environ[H.ENV_EVENTS] = old


# ------------------------------------------------------------------ paths e card_styles "su Mac"
def _as_platform(platform, frozen, fn):
    """Ricarica i moduli con sys.platform (e sys.frozen) finti, chiama fn, rimette tutto."""
    real_p, had_f, real_f = sys.platform, hasattr(sys, "frozen"), getattr(sys, "frozen", None)
    try:
        sys.platform = platform
        if frozen:
            sys.frozen = True
        elif had_f:
            del sys.frozen
        return fn()
    finally:
        sys.platform = real_p
        if had_f:
            sys.frozen = real_f
        elif hasattr(sys, "frozen"):
            del sys.frozen
        import paths
        import card_styles
        importlib.reload(paths)
        importlib.reload(card_styles)


def test_paths_su_darwin():
    home = tempfile.mkdtemp()
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = home
    try:
        def load():
            import paths
            importlib.reload(paths)
            return paths.FROZEN, paths.CONFIG_DIR, paths.STATE_DIR, paths.config("groq_key.txt")
        frozen, cfg, st, key = _as_platform("darwin", True, load)
        want = os.path.join(home, "Library", "Application Support", "Wavetype")
        eq((frozen, cfg, st), (True, want, want))
        eq(key, os.path.join(want, "groq_key.txt"))
        assert os.path.isdir(want), "config() crea la cartella"
        import paths
        eq(paths._dirs("darwin", False), ("", ""), "dal sorgente non cambia niente")
        eq(paths._dirs("win32", True)[0].endswith("Wavetype"), True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_card_styles_su_darwin_tasti_e_font():
    def load():
        import card_styles
        importlib.reload(card_styles)
        return (card_styles.K_CHORD, card_styles.K_RECOVER, card_styles.K_UNDO,
                card_styles.R_FOOTERS["no_audio"][1], card_styles._fallback_paths()[0])
    got = _as_platform("darwin", False, load)
    eq(got, ("fn", "Ctrl+Opt+R", "Cmd+Z", "Ctrl+Opt+R", "/System/Library/Fonts/SFNS.ttf"))
    import card_styles
    eq((card_styles.K_CHORD, card_styles.K_RECOVER, card_styles.K_UNDO),
       ("fn", "Ctrl+Opt+R", "Cmd+Z") if sys.platform == "darwin"
       else ("Win+Ctrl", "Win+Ctrl+R", "Ctrl+Z"), "tornato com'era")


def test_moduli_mac_si_importano_senza_pyobjc():
    for m in ("mac_ax", "mac_sys", "mac_panel", "mac_keys", "mac_geom", "testhooks"):
        importlib.import_module(m)
    import mac_sys
    a = mac_sys.tone(440, 100)
    eq(len(a), 4410)
    eq(float(a[0]), 0.0, "attacco morbido")
    assert float(np.abs(a).max()) <= 0.25 + 1e-6


# ------------------------------------------------------------------ worker_mac vero, in "darwin"
WORKER_PROBE = r'''
import json, os, sys, threading, time
sys.path.insert(0, ROOT)
# i pacchetti pesanti si importano col sistema vero, poi si finge darwin per i moduli di Wavetype
import numpy, sounddevice, httpx, tkinter, PIL.Image  # noqa
try:
    import faster_whisper  # noqa
except Exception:
    pass
sys.platform = "darwin"
import wavetype as W
import mac_keys as K
assert W.IS_MAC and not W.IS_WIN
calls = []
slow = [0.0]
W.log = lambda m: None
W.beep = lambda *a, **k: None
W.live_cancel = lambda: None
W.testhooks.emit = lambda ev, **f: calls.append(("ev", ev, f.get("what")))

def fake_start():
    calls.append("start")
    time.sleep(slow[0])             # registrazione che si apre lenta (AX, microfono Bluetooth)
    W.rec["held"] = True
    W.rec["edit"] = False
    W.rec["t0"] = time.perf_counter()
    W.ui["state"] = "rec"

def fake_stop():
    calls.append("stop")
    W.rec["held"] = False
    W.ui["state"] = "idle"

W.start_capture_mac = fake_start
W.stop_and_process = fake_stop
W.chord_recover = lambda: (calls.append("recover"), fake_stop() if W.rec["held"] else None)
W.chord_style = lambda: calls.append("style")
W.live_end = lambda *a, **k: None
th = threading.Thread(target=W.worker_mac, daemon=True)
th.start()
S = K.STATE

def fn(down, wait):
    S.handle(K.T_FLAGS, K.KC_FN, K.F_FN if down else 0)
    time.sleep(wait)

out = {}
# 1) tocco breve: parte, resta in ascolto, il secondo tocco ferma
fn(True, 0.10); fn(False, 0.30)
out["tap_rec"] = W.rec["held"]
fn(True, 0.10); fn(False, 0.20)
out["tap"] = [c for c in calls if isinstance(c, str)]
del calls[:]
# 2) tenuto 0,6 s: parte, al rilascio si ferma
fn(True, 0.60); fn(False, 0.20)
out["hold"] = [c for c in calls if isinstance(c, str)]
del calls[:]
# 3) Ctrl+Option poi R: parte, R = recupero (annulla la registrazione), rilasciare non riparte
S.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT); time.sleep(0.10)
S.handle(K.T_KEYDOWN, K.KC_R, K.F_CTRL | K.F_OPT); time.sleep(0.10)
S.handle(K.T_KEYUP, K.KC_R, K.F_CTRL | K.F_OPT); time.sleep(0.10)
S.handle(K.T_FLAGS, 58, 0); time.sleep(0.15)
out["chord_r"] = [c for c in calls if isinstance(c, str)]
del calls[:]
# 4) ESC mentre tieni Fn: annulla, rilasciare Fn non fa niente
fn(True, 0.10)
S.handle(K.T_KEYDOWN, K.KC_ESC, K.F_FN); time.sleep(0.10)
S.handle(K.T_KEYUP, K.KC_ESC, K.F_FN); time.sleep(0.05)
out["esc_rec"] = W.rec["held"]
fn(False, 0.15)
out["esc"] = [c if isinstance(c, str) else c[2] for c in calls if c != ("ev", "hotkey", "start")]
del calls[:]
# 5) tocco di 0,1 s mentre l'avvio dura 0,5 s: resta in ascolto, il secondo tocco ferma
slow[0] = 0.5
fn(True, 0.10); fn(False, 0.70)
out["slow_rec"] = W.rec["held"]
slow[0] = 0.0
fn(True, 0.10); fn(False, 0.20)
out["slow"] = [c for c in calls if isinstance(c, str)]
del calls[:]
# 6) Fn+Canc avanti: annullata, il rilascio di Fn non ferma ne' riparte
fn(True, 0.10)
S.handle(K.T_KEYDOWN, 117, K.F_FN); time.sleep(0.10)
S.handle(K.T_KEYUP, 117, K.F_FN); time.sleep(0.05)
out["fn_del_rec"] = W.rec["held"]
fn(False, 0.15)
out["fn_del"] = [c if isinstance(c, str) else c[2] for c in calls if c != ("ev", "hotkey", "start")]
del calls[:]
# 7) R di 30 ms mentre la registrazione si apre (0,3 s): recupero, niente ascolto rimasto aperto
slow[0] = 0.3
S.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT); time.sleep(0.05)
S.handle(K.T_KEYDOWN, K.KC_R, K.F_CTRL | K.F_OPT); time.sleep(0.03)
S.handle(K.T_KEYUP, K.KC_R, K.F_CTRL | K.F_OPT); time.sleep(0.05)
S.handle(K.T_FLAGS, 58, 0); time.sleep(0.6)
out["fast_r_rec"] = W.rec["held"]
out["fast_r"] = [c for c in calls if isinstance(c, str)]
del calls[:]
# 8) ESC di 30 ms mentre la registrazione si apre: annullata
fn(True, 0.05)
S.handle(K.T_KEYDOWN, K.KC_ESC, K.F_FN); time.sleep(0.03)
S.handle(K.T_KEYUP, K.KC_ESC, K.F_FN); time.sleep(0.05)
fn(False, 0.6)
out["fast_esc_rec"] = W.rec["held"]
del calls[:]
# 9) Ctrl+Option e R nello stesso giro del worker: recupero, nessuna dettatura al giro dopo
slow[0] = 0.0
S.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT)
S.handle(K.T_KEYDOWN, K.KC_R, K.F_CTRL | K.F_OPT); time.sleep(0.15)
S.handle(K.T_KEYUP, K.KC_R, K.F_CTRL | K.F_OPT)
S.handle(K.T_FLAGS, 58, 0); time.sleep(0.15)
out["same_tick_r_rec"] = W.rec["held"]
out["same_tick_r"] = [c for c in calls if isinstance(c, str)]
del calls[:]
# 10) Ctrl+Option+Q di 30 ms mentre la registrazione si apre: esce (run #2 della CI: restava su)
slow[0] = 0.3
S.handle(K.T_FLAGS, 58, K.F_CTRL | K.F_OPT); time.sleep(0.05)
S.handle(K.T_KEYDOWN, K.KC_Q, K.F_CTRL | K.F_OPT); time.sleep(0.03)
S.handle(K.T_KEYUP, K.KC_Q, K.F_CTRL | K.F_OPT); time.sleep(0.05)
S.handle(K.T_FLAGS, 58, 0); time.sleep(0.6)
out["quit"] = W.flags["quit"]
out["quit_calls"] = [c for c in calls if isinstance(c, str)]
th.join(1.0)
out["alive"] = th.is_alive()
print("PROBE " + json.dumps(out))
'''


def test_worker_mac_vero_in_darwin():
    code = "ROOT = " + repr(ROOT) + "\n" + WORKER_PROBE
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                       cwd=ROOT)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("PROBE ")]
    assert line, f"probe senza esito (rc={r.returncode}): {r.stderr[-800:]}"
    out = json.loads(line[-1][6:])
    eq(out["tap_rec"], True, "dopo il tocco breve resta in ascolto")
    eq(out["tap"], ["start", "stop"], "tocco, tocco")
    eq(out["hold"], ["start", "stop"], "tenuto")
    eq(out["chord_r"], ["start", "recover", "stop"], "Ctrl+Option+R")
    eq(out["esc_rec"], False, "ESC ferma")
    eq(out["esc"], ["start", "cancel"], "ESC poi rilascio di Fn: nessuno stop in piu'")
    eq(out["slow_rec"], True, "tocco breve con avvio lento: resta in ascolto")
    eq(out["slow"], ["start", "stop"], "avvio lento, tocco, tocco")
    eq(out["fn_del_rec"], False, "Fn+Canc annulla")
    eq(out["fn_del"], ["start", "cancel"], "Fn+Canc poi rilascio di Fn: nessuno stop in piu'")
    eq(out["fast_r_rec"], False, "R breve durante l'avvio: niente ascolto rimasto aperto")
    eq(out["fast_r"], ["start", "recover", "stop"], "R breve durante l'avvio: recupero")
    eq(out["fast_esc_rec"], False, "ESC breve durante l'avvio annulla")
    eq(out["same_tick_r_rec"], False, "Ctrl+Option+R nello stesso giro: niente dettatura")
    eq(out["same_tick_r"] in (["recover"], ["start", "recover", "stop"]), True,
       f"Ctrl+Option+R nello stesso giro: {out['same_tick_r']}")
    eq(out["quit_calls"], ["start"], "Q arrivata mentre l'avvio era in corso")
    eq((out["quit"], out["alive"]), (True, False), "Ctrl+Option+Q breve durante l'avvio esce")


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
