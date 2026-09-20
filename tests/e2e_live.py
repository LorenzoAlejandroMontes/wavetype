"""
tests/e2e_live.py — banco end-to-end del live DENTRO wavetype.py (Lotto D).

Non simula l'integrazione: la esegue. Gira il vero loop principale (`wavetype.build_ui`), la vera
`start_rec` / `audio_cb` / `stop_and_process` / `_process`, il vero motore live e il vero pannello
a schermo. Cambiano solo tre cose, tutte ai bordi:
  - il microfono: una `InputStream` finta rigioca una registrazione d'archivio in blocchi da 1024
    campioni a 48 kHz, in tempo reale (l'audio dell'archivio e' a 16 kHz: viene ripetuto 3 volte,
    cosi' il downsample a media di 3 dell'app restituisce esattamente i campioni originali);
  - l'incolla: `insert_text` viene sostituita da una cattura (NIENTE viene incollato nelle app di
    nessuna clipboard toccata);
  - le risposte di Groq passano da una cache su disco (`tests/cache/`), come fa `tests/replay.py`:
    rigiocare due volte lo stesso scenario non costa nemmeno una richiesta.
Archivio, storico e log vanno in `tests/cache/e2e/`: `recordings/`, `wavetype_history.txt` e
`wavetype.log` dell'app non vengono toccati.

Uso:
  .venv/Scripts/python.exe tests/e2e_live.py                    # 4 scenari + foto del pannello
  .venv/Scripts/python.exe tests/e2e_live.py --drill feed       # il motore esplode in feed()
  .venv/Scripts/python.exe tests/e2e_live.py --drill stop-none  # stop() torna None
  .venv/Scripts/python.exe tests/e2e_live.py --drill panel      # il pannello esplode in update()
  .venv/Scripts/python.exe tests/e2e_live.py --drill net        # rete giu' (httpx.ConnectError)
  .venv/Scripts/python.exe tests/e2e_live.py --drill live-off   # LIVE = False
  .venv/Scripts/python.exe tests/e2e_live.py --recover          # solo Win+Ctrl+R (card + incolla)
  .venv/Scripts/python.exe tests/e2e_live.py --repeat 5         # igiene thread/GDI
"""
import argparse
import ctypes
import hashlib
import json
import os
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import httpx                                                   # noqa: E402
from PIL import ImageGrab                                      # noqa: E402

import caret as caret_mod                                      # noqa: E402
import live_engine                                             # noqa: E402
import live_panel                                              # noqa: E402
import wavetype                                                   # noqa: E402

OUT = os.path.join(ROOT, "tests", "panel_out")
WORK = os.path.join(ROOT, "tests", "cache", "e2e")
CACHE = os.path.join(ROOT, "tests", "cache")
os.makedirs(OUT, exist_ok=True)
os.makedirs(WORK, exist_ok=True)

BLOCK = 1024                      # campioni per blocco a 48 kHz (~21 ms), come un mic vero
DRILL = ""                        # prova di guasto in corso
USE_CACHE = True
NO_FMT_NET = False                # --no-fmt-net: niente formattazione Groq fuori cache (secchio token)

LOGS = []
STATS = {"stt_calls": 0, "live_req": 0, "live_cached": 0, "batch_req": 0, "batch_cached": 0,
         "fmt_req": 0, "fmt_cached": 0, "panel_err": 0, "beeps": [], "fmt_offline": 0}

# (nome, file, secondi in cui fotografare il pannello)
SCENARI = [
    ("corto <10s", "20260915-123327.wav", []),
    ("medio 40s", "20260916-155627.wav", [16.0, 34.0]),
    ("lungo >90s", "20260914-143848.wav", [22.0, 58.0, 96.0]),
    ("muto", "20260918-210538.wav", []),
]
CORTO = "20260915-123327.wav"


# ------------------------------------------------------------------ utilita'
def sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def cached(kind, key, fn):
    """(valore, era_in_cache) su tests/cache/, come replay.py."""
    p = os.path.join(CACHE, f"{kind}-{key}.json")
    if USE_CACHE and os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))["v"], True
        except Exception:
            pass
    v = fn()
    if USE_CACHE:
        try:
            json.dump({"v": v}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return v, False


ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
ctypes.windll.user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
ctypes.windll.user32.GetGuiResources.restype = ctypes.c_uint


def gui_res(kind=0):
    """Oggetti GDI (0) o USER (1) del processo: servono a vedere se il pannello ne perde.
    Senza argtypes l'handle a 64 bit viene troncato e la chiamata risponde 0."""
    try:
        h = ctypes.windll.kernel32.GetCurrentProcess()
        return int(ctypes.windll.user32.GetGuiResources(h, kind))
    except Exception:
        return -1


def engine_threads():
    """Thread di lavoro del motore (Groq: live-engine*, locale: live-local; il caricamento
    del modello, live-local-load, finisce da solo e non conta)."""
    return [t.name for t in threading.enumerate()
            if "live-engine" in t.name or t.name == "live-local"]


# ------------------------------------------------------------------ microfono finto
class FakeStream:
    """InputStream di sounddevice, ma i blocchi vengono da un file d'archivio, in tempo reale."""

    def __init__(self, samplerate=48000, channels=1, dtype="float32", callback=None, **kw):
        self.sr = samplerate
        self.cb = callback
        self.audio = FakeSD.audio
        self.t = threading.Thread(target=self._run, name="e2e-mic", daemon=True)
        self.stop_flag = threading.Event()
        self.done = threading.Event()
        self.fed = 0

    def _run(self):
        t0 = time.perf_counter()
        i = 0
        while not self.stop_flag.is_set() and i < len(self.audio):
            blk = self.audio[i:i + BLOCK].reshape(-1, 1)
            i += BLOCK
            try:
                self.cb(blk, len(blk), None, None)
            except Exception as e:
                print(f"  [e2e] audio_cb ha sollevato: {type(e).__name__}: {e}")
            self.fed = i
            target = t0 + i / self.sr
            d = target - time.perf_counter()
            if d > 0:
                time.sleep(d)
        self.done.set()

    def start(self):
        self.t.start()

    def stop(self):
        self.stop_flag.set()

    def close(self):
        self.stop_flag.set()
        self.t.join(1.0)


class FakeSD:
    audio = np.zeros(0, dtype="float32")
    InputStream = FakeStream

    @staticmethod
    def query_devices(kind=None):
        return {"name": "microfono finto (e2e)"}


# ------------------------------------------------------------------ rete in cache
class FakeResp:
    def __init__(self, data, status=200):
        self.status_code = status
        self._j = data
        self.headers = {}
        self.text = ""

    def json(self):
        return self._j


# Il motore che wavetype.py usa davvero: live_local (LIVE_ENGINE = "local", nessuna rete) oppure
# live_engine (Groq). Le prove di guasto valgono per entrambi; la cache della POST solo per Groq.
ENGINE_MOD = wavetype.live_engine


class DrillEngine(ENGINE_MOD.LiveEngine):
    """Il motore vero, con le prove di guasto."""

    def feed(self, block):
        if DRILL == "feed":
            raise RuntimeError("prova: feed() esplode")
        return super().feed(block)

    def stop(self, timeout=30):
        if DRILL == "stop-none":
            super().cancel()
            return None
        return super().stop(timeout)


class CachedEngine(DrillEngine):
    """Solo motore Groq: la POST passa dalla cache."""

    def _post(self, wav, data, timeout):
        key = sha(wav, json.dumps(data, sort_keys=True))
        j, hit = cached("live", key, lambda: self._real(wav, data, timeout))
        if hit:
            STATS["live_cached"] += 1
        else:
            STATS["live_req"] += 1
        return FakeResp(j)

    def _real(self, wav, data, timeout):
        r = super()._post(wav, data, timeout)
        if r.status_code != 200:
            raise RuntimeError(f"groq http {r.status_code}: {str(r.text)[:120]}")
        return r.json()


def patch_net():
    _transcribe = wavetype.groq_transcribe
    _format = wavetype.groq_format

    def transcribe(wav_path, force_lang=None, context=""):
        with open(wav_path, "rb") as f:
            key = sha(f.read(), force_lang, context, wavetype.VOCAB)
        v, hit = cached("e2e-stt", key, lambda: list(_transcribe(wav_path, force_lang, context)))
        STATS["batch_cached" if hit else "batch_req"] += 1
        return tuple(v)

    def fmt(text, lang):
        if NO_FMT_NET:
            key = sha("fmt", wavetype.GROQ_LLM_MODEL, wavetype.build_prompt(text, lang))
            path = os.path.join(CACHE, f"fmt-{key}.json")
            if not os.path.exists(path):          # niente rete: format_text ripiega sul locale
                STATS["fmt_offline"] += 1
                raise RuntimeError("e2e --no-fmt-net: formattazione non in cache")
        v, hit = cached("fmt", sha("fmt", wavetype.GROQ_LLM_MODEL, wavetype.build_prompt(text, lang)),
                        lambda: _format(text, lang))
        STATS["fmt_cached" if hit else "fmt_req"] += 1
        return v

    wavetype.groq_transcribe = transcribe
    wavetype.groq_format = fmt


class NetDown:
    """httpx con la rete giu': la POST solleva ConnectError, il resto serve a groq_retry."""
    ConnectError = httpx.ConnectError
    TransportError = httpx.TransportError
    HTTPStatusError = httpx.HTTPStatusError

    @staticmethod
    def post(*a, **k):
        raise httpx.ConnectError("prova: rete giu'")

    class Client:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            raise httpx.ConnectError("prova: rete giu'")

        def close(self):
            pass


class BrokenPanel(live_panel.LivePanel):
    def update(self, snap):
        raise RuntimeError("prova: update() del pannello esplode")


# ------------------------------------------------------------------ innesti
def patch_all(drill):
    global USE_CACHE
    log_path = os.path.join(WORK, "e2e.log")

    def log(msg):
        LOGS.append(str(msg))
        if "pannello disattivato" in str(msg):
            STATS["panel_err"] += 1
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass

    wavetype.log = log                       # wavetype.log dell'app resta pulito
    wavetype.sd = FakeSD
    wavetype.REC_DIR = os.path.join("tests", "cache", "e2e", "rec")
    wavetype.WAV_OUT = os.path.join(WORK, "last_rec.wav")
    wavetype.HISTORY = os.path.join(WORK, "history.txt")
    wavetype.insert_text = fake_insert
    wavetype.beep = lambda f, ms=90: STATS["beeps"].append(f)
    ENGINE_MOD.LiveEngine = CachedEngine if wavetype.LIVE_ENGINE == "groq" else DrillEngine
    patch_net()
    _stt = wavetype.stt

    def stt(wav_path, a16, t0):
        STATS["stt_calls"] += 1
        return _stt(wav_path, a16, t0)

    wavetype.stt = stt
    if drill == "panel":
        live_panel.LivePanel = BrokenPanel
    if drill == "net":
        USE_CACHE = False
        wavetype.httpx = NetDown
        live_engine.httpx = NetDown
    if drill == "live-off":
        wavetype.LIVE = False


PASTED = []


def fake_insert(hwnd, text):
    PASTED.append((time.perf_counter(), text))


# ------------------------------------------------------------------ fondale
def backdrop():
    """Finestrella scura in basso al centro: sfondo certo per le foto, nessuna app toccata."""
    import win32api
    import win32con
    import win32gui
    hinst = win32api.GetModuleHandle(None)
    cls = "WavetypeE2EBackdrop"
    try:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = cls
        wc.hInstance = hinst
        wc.lpfnWndProc = lambda h, m, w, l: win32gui.DefWindowProc(h, m, w, l)
        wc.hbrBackground = win32gui.CreateSolidBrush(win32api.RGB(16, 18, 24))
        win32gui.RegisterClass(wc)
    except Exception:
        pass
    wl, wt, wr, wb = caret_mod.work_area(10, 10)
    bw, bh = 1100, 380
    h = win32gui.CreateWindowEx(win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW, cls,
                                "e2e", win32con.WS_POPUP,
                                (wl + wr) // 2 - bw // 2, wb - bh, bw, bh, 0, 0, hinst, None)
    win32gui.ShowWindow(h, win32con.SW_SHOWNOACTIVATE)
    return h


def grab(tag):
    """Foto del pannello vero a schermo (coordinate fisiche: il processo e' DPI-unaware)."""
    p = wavetype.live.get("panel")
    if p is None or not p.hwnd or not p.is_open():
        print(f"  [foto] {tag}: pannello non a schermo")
        return None
    import win32gui
    try:
        with caret_mod.dpi_scope():
            r = win32gui.GetWindowRect(p.hwnd)
            box = (max(0, r[0] - 8), max(0, r[1] - 8), r[2] + 8, r[3] + 8)
            img = ImageGrab.grab(box, all_screens=True)
        path = os.path.join(OUT, f"e2e_{tag}.png")
        img.save(path)
        print(f"  [foto] {path} {img.size}", flush=True)
        return path
    except Exception as e:
        print(f"  [foto] {tag}: {e}")
        return None


def hud_visible():
    """1 se la finestra dell'HUD (faccina/cane) di QUESTO processo e' a schermo. Il Wavetype vero di
    l'app vera puo' girare accanto con la stessa classe di finestra: si contano solo le nostre."""
    import win32gui
    import win32process
    me, found = os.getpid(), []

    def each(h, _):
        if win32gui.GetClassName(h) == "WavetypeHUD" and                 win32process.GetWindowThreadProcessId(h)[1] == me and win32gui.IsWindowVisible(h):
            found.append(h)
        return True
    win32gui.EnumWindows(each, None)
    return int(bool(found))


def hud_idle(sec=5.0):
    """HUD a schermo da fermo (avvio compreso: l'HUD di prima restava 4 s dopo il boot)."""
    on = n = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < sec:
        time.sleep(0.05)
        n += 1
        on += hud_visible()
    return f"{on}/{n}"


# ------------------------------------------------------------------ una dettatura
def load_audio(name):
    a, sr = wavetype.read_wav(os.path.join(ROOT, "recordings", name))
    return np.repeat(a, 3).astype("float32"), len(a) / sr      # 16k -> 48k, come il mic vero


def dictate(label, name, shots=(), tag=""):
    FakeSD.audio, dur = load_audio(name)
    del PASTED[:]
    STATS["stt_calls"] = 0
    beeps0 = len(STATS["beeps"])
    live0 = STATS["live_req"] + STATS["live_cached"]
    print(f"\n--- {label}: {name} ({dur:.1f}s) ---", flush=True)
    t_start = time.perf_counter()
    wavetype.start_rec(edit=False)
    t_open = time.perf_counter() - t_start
    st = wavetype.rec["stream"]
    shots = list(shots)
    hud_on = hud_n = 0
    while not st.done.is_set():
        time.sleep(0.05)
        hud_n += 1
        hud_on += hud_visible()
        el = time.perf_counter() - t_start
        if shots and el >= shots[0]:
            grab(f"{tag}{int(shots.pop(0))}s")
    t_stop = time.perf_counter()
    wavetype.stop_and_process()
    while time.perf_counter() - t_stop < 90 and wavetype.ui["state"] != "idle":
        time.sleep(0.02)
    t_end = time.perf_counter()
    while time.perf_counter() - t_end < 2.0 and not PASTED:   # l'incolla la fa il loop principale
        time.sleep(0.02)
    lat = (PASTED[0][0] - t_stop) if PASTED else None
    time.sleep(1.4)                                  # lascia finire la chiusura del pannello
    row = {"scenario": label, "file": name, "dur": round(dur, 1),
           "start_rec_ms": round(t_open * 1000, 1),
           "pasted": PASTED[0][1] if PASTED else None,
           "stop_to_paste_s": round(lat, 2) if lat is not None else None,
           "via": "batch" if STATS["stt_calls"] else ("live" if PASTED else "nessuno"),
           "live_requests": STATS["live_req"] + STATS["live_cached"] - live0,
           "beeps": STATS["beeps"][beeps0:],
           "panel_open_after": wavetype.live_panel_open(),
           "hud_visible_during_rec": f"{hud_on}/{hud_n}",
           "engine_threads": engine_threads(), "gdi": gui_res(0), "user": gui_res(1)}
    print(f"  incollato: {str(row['pasted'])[:90]!r}")
    print(f"  stop->incolla {row['stop_to_paste_s']}s · via {row['via']} · "
          f"richieste live {row['live_requests']} · bip {row['beeps']} · "
          f"start_rec {row['start_rec_ms']} ms · thread motore {row['engine_threads']} · "
          f"HUD visibile {row['hud_visible_during_rec']} campioni")
    return row


# ------------------------------------------------------------------ recupero (Win+Ctrl+R)
def recover_run(label, shots=(), tag="recover_"):
    """Win+Ctrl+R sull'ultima registrazione in archivio: nessun microfono, la card deve comparire
    da sola (recovering -> recovered) e il testo deve tornare nella finestra."""
    import win32gui
    del PASTED[:]
    beeps0 = len(STATS["beeps"])
    target = wavetype.last_recording()
    print(f"\n--- {label}: {os.path.basename(target) if target else 'archivio vuoto'} ---",
          flush=True)
    t0 = time.perf_counter()
    hwnd = win32gui.GetForegroundWindow()
    fasi = []
    threading.Thread(target=wavetype.reprocess_last, args=(hwnd, target), name="e2e-recover",
                     daemon=True).start()
    shots = list(shots)
    aperta = 0
    while time.perf_counter() - t0 < 90:
        time.sleep(0.05)
        el = time.perf_counter() - t0
        ph = wavetype.live.get("phase")
        if ph and (not fasi or fasi[-1] != ph):
            fasi.append(ph)
        aperta += wavetype.live_panel_open()
        if shots and el >= shots[0]:
            grab(f"{tag}{shots.pop(0):.1f}s")
        if PASTED and el - (PASTED[0][0] - t0) > 0.4:
            break
        if wavetype.ui["state"] == "idle" and el > 4 and not PASTED and not wavetype.live_panel_open():
            break
    grab(f"{tag}esito")
    lat = (PASTED[0][0] - t0) if PASTED else None
    time.sleep(2.0)                                   # lascia finire la chiusura della card
    row = {"scenario": label, "file": os.path.basename(target) if target else None,
           "dur": None, "pasted": PASTED[0][1] if PASTED else None,
           "stop_to_paste_s": round(lat, 2) if lat is not None else None,
           "via": "recupero", "fasi": " > ".join(fasi), "beeps": STATS["beeps"][beeps0:],
           "panel_open_after": wavetype.live_panel_open(),
           "panel_samples": aperta}
    print(f"  incollato: {str(row['pasted'])[:70]!r} · fasi {row['fasi']!r} · "
          f"tasto->incolla {row['stop_to_paste_s']}s · card aperta dopo {row['panel_open_after']}")
    return row


def recover_scenarios():
    """Una dettatura corta (riempie l'archivio) e poi il recupero di quella stessa."""
    rows = []
    if not wavetype.last_recording():
        rows.append(dictate("prima del recupero", CORTO, tag="pre_recover_"))
    rows.append(recover_run("recupero", [0.6, 1.6], "recover_"))
    return rows


# ------------------------------------------------------------------ Edit Mode
# Testo selezionato finto (niente dati personali). La riscrittura NON va a Groq: edit_transform e'
# sostituita da una finta lenta 1,2 s (serve a fotografare "rewriting"); si prova la card, l'HUD,
# il chip e l incolla, non il modello. L istruzione detta e una dettatura vera di 7,7 s.
EDIT_SEL = "ciao marco ti scrivo per il preventivo di giovedi fammi sapere se va bene per te"
EDIT_SAID = "20260915-123327.wav"   # parlata (l unica edit-*.wav vera e silenzio: la guardia la scarta)
EDIT_CALLS = []


def fake_edit(sel, instr):
    EDIT_CALLS.append(instr)
    time.sleep(1.2)
    return "Ciao Marco, ti scrivo per il preventivo di giovedì: fammi sapere se va bene per te."


def edit_run(label, name, chip_key=None, shots=(), tag=""):
    """Un Edit come da Win+Ctrl su una selezione: istruzione detta (o tasto 1-3 dopo 1 s)."""
    import win32gui
    FakeSD.audio, dur = load_audio(name)
    del PASTED[:], EDIT_CALLS[:]
    beeps0 = len(STATS["beeps"])
    print(f"\n--- {label}: {name} ({dur:.1f}s) ---", flush=True)
    wavetype.rec["hwnd"] = win32gui.GetForegroundWindow()
    t_start = time.perf_counter()
    wavetype.start_rec(edit=True, sel=EDIT_SEL)
    st = wavetype.rec["stream"]
    shots = list(shots)
    hud_on = hud_n = 0

    def sample():
        nonlocal hud_on, hud_n
        hud_n += 1
        hud_on += hud_visible()
        el = time.perf_counter() - t_start
        if shots and el >= shots[0]:
            grab(f"{tag}{shots.pop(0):.1f}s")
    while not st.done.is_set():
        time.sleep(0.05)
        sample()
        if chip_key and time.perf_counter() - t_start >= 1.0:
            wavetype._chip_key(chip_key)        # quello che fa l'hook del tasto (il worker qui non gira)
            break
    t_stop = time.perf_counter()
    wavetype.stop_and_process()
    while time.perf_counter() - t_stop < 60 and (wavetype.ui["state"] != "idle" or not PASTED):
        time.sleep(0.05)
        sample()
        if wavetype.ui["state"] == "idle" and time.perf_counter() - t_stop > 3 and not PASTED:
            break
    lat = (PASTED[0][0] - t_stop) if PASTED else None
    t_end = time.perf_counter()
    while time.perf_counter() - t_end < 0.35:     # la pillola "done" prima della chiusura
        time.sleep(0.05)
        sample()
    grab(f"{tag}done")
    while time.perf_counter() - t_end < 2.2:
        time.sleep(0.05)
        sample()
    p = wavetype.live.get("panel")
    row = {"scenario": label, "file": name, "dur": round(dur, 1),
           "pasted": PASTED[0][1] if PASTED else None,
           "stop_to_paste_s": round(lat, 2) if lat is not None else None,
           "via": "edit", "instr": (EDIT_CALLS[0][:70] if EDIT_CALLS else None),
           "chip": getattr(p, "chip", None), "beeps": STATS["beeps"][beeps0:],
           "panel_open_after": wavetype.live_panel_open(),
           "hud_visible_during_rec": f"{hud_on}/{hud_n}"}
    print(f"  istruzione a EDIT_PROMPT: {row['instr']!r} · chip {row['chip']}")
    print(f"  incollato: {str(row['pasted'])[:60]!r} · stop->incolla {row['stop_to_paste_s']}s · "
          f"HUD visibile {row['hud_visible_during_rec']} campioni · card aperta dopo {row['panel_open_after']}")
    return row


def edit_scenarios():
    wavetype.live_preload()                  # come l'app all'avvio: il modello locale e' gia' pronto
    saved = wavetype.edit_transform
    wavetype.edit_transform = fake_edit
    try:
        return [edit_run("edit detto", EDIT_SAID, None, [1.5, 4.0, 8.2], "edit_detto_"),
                edit_run("edit tasto 1", CORTO, "1", [0.6, 1.3, 2.0], "edit_tasto_")]
    finally:
        wavetype.edit_transform = saved


# ------------------------------------------------------------------ giro completo
def run_all(args):
    while not wavetype.live["ui"] and time.perf_counter() < args.t_boot + 20:
        time.sleep(0.1)
    time.sleep(1.2)                                   # prewarm del caret + primo frame dell'HUD
    idle0 = hud_idle()                                # da fermo, dentro la finestra dei 4 s d'avvio
    print(f"HUD visibile da fermo (avvio) {idle0} campioni", flush=True)
    rows = []
    g0, u0, th0 = gui_res(0), gui_res(1), len(threading.enumerate())
    try:
        if args.repeat:
            for i in range(args.repeat):
                rows.append(dictate(f"ripetizione {i + 1}", CORTO, tag=f"rip{i + 1}_"))
        elif args.drill:
            rows.append(dictate(f"drill {args.drill}", args.file or CORTO, tag=f"{args.drill}_"))
        elif args.edit:
            rows += edit_scenarios()
        elif args.recover:
            rows += recover_scenarios()
        elif args.file:
            rows.append(dictate("singolo", args.file, [2.0, 5.0], tag="one_"))
        else:
            for label, name, shots in SCENARI:
                rows.append(dictate(label, name, shots, tag=f"{label.split()[0]}_"))
            rows += recover_scenarios()
            rows += edit_scenarios()
    except Exception as e:
        import traceback
        traceback.print_exc()
        rows.append({"scenario": "ERRORE", "pasted": f"{type(e).__name__}: {e}"})
    time.sleep(0.6)
    idle1 = hud_idle(3.0)                             # da fermo, dopo le dettature
    summary = {"drill": args.drill, "live": wavetype.LIVE, "rows": rows,
               "hud_on": wavetype.HUD_ON, "hud_visible_idle_boot": idle0, "hud_visible_idle_end": idle1,
               "gdi_prima": g0, "gdi_dopo": gui_res(0), "user_prima": u0, "user_dopo": gui_res(1),
               "thread_prima": th0, "thread_dopo": len(threading.enumerate()),
               "thread_motore_dopo": engine_threads(),
               "thread_vivi": sorted(t.name for t in threading.enumerate()),
               "richieste_groq_vere": STATS["live_req"] + STATS["batch_req"] + STATS["fmt_req"],
               "richieste_da_cache": STATS["live_cached"] + STATS["batch_cached"] + STATS["fmt_cached"],
               "eccezioni_pannello": STATS["panel_err"],
               "formattazioni_fuori_cache_senza_rete": STATS["fmt_offline"]}
    name = f"e2e-{args.drill or ('rip' if args.repeat else ('edit' if args.edit else 'base'))}.json"
    json.dump(summary, open(os.path.join(WORK, name), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n=== riepilogo ===")
    for r in rows:
        print(f"{r.get('scenario', '?'):16} via {r.get('via', '?'):8} "
              f"stop->incolla {str(r.get('stop_to_paste_s')):>6}s  "
              f"incollato {len(r['pasted'] or '') if 'pasted' in r else 0:4d} char")
    print(f"GDI {g0} -> {summary['gdi_dopo']} · USER {u0} -> {summary['user_dopo']} · "
          f"thread {th0} -> {summary['thread_dopo']} {summary['thread_motore_dopo']}")
    print(f"richieste Groq vere {summary['richieste_groq_vere']} · "
          f"da cache {summary['richieste_da_cache']} · "
          f"eccezioni pannello {summary['eccezioni_pannello']} · "
          f"formattazioni senza rete (ripiego locale) {STATS['fmt_offline']}")
    for r in rows:
        if "hud_visible_during_rec" in r:
            print(f"HUD durante {r['scenario']}: {r['hud_visible_during_rec']}")
    print(f"HUD visibile da fermo: avvio {idle0} · fine {idle1} campioni (HUD_ON={wavetype.HUD_ON})")
    print(f"dettaglio in {os.path.join(WORK, name)}")
    wavetype.flags["quit"] = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drill", default="", choices=["", "feed", "stop-none", "panel", "net",
                                                    "live-off"])
    ap.add_argument("--repeat", type=int, default=0)
    ap.add_argument("--file", default="")
    ap.add_argument("--edit", action="store_true", help="solo gli scenari Edit Mode")
    ap.add_argument("--recover", action="store_true", help="solo il recupero (Win+Ctrl+R)")
    ap.add_argument("--no-fmt-net", action="store_true",
                    help="formattazione Groq solo da cache (secchio token vuoto): fuori cache ripiego locale")
    args = ap.parse_args()
    global NO_FMT_NET
    NO_FMT_NET = args.no_fmt_net
    args.t_boot = time.perf_counter()
    global DRILL
    DRILL = args.drill
    patch_all(args.drill)
    bd = backdrop()
    threading.Thread(target=run_all, args=(args,), name="e2e-driver", daemon=True).start()
    try:
        wavetype.build_ui()                     # il vero loop principale, sul thread principale
    finally:
        try:
            import win32gui
            win32gui.DestroyWindow(bd)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
