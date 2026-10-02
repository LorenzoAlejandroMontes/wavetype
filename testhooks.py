"""
testhooks.py — ganci per provare Wavetype senza mani e senza microfono (CI su macOS).

Contratto con la CI (spec mac del 02/10, da rispettare alla lettera):
  WAVETYPE_TEST_WAV=<file.wav>     lo stream di registrazione legge questo WAV invece del
                                   microfono: mono o stereo, qualsiasi rate (si ricampiona al
                                   rate dell'app), consegnato ad audio_cb a tempo reale, a
                                   blocchi; finito il file, silenzio.
  WAVETYPE_TEST_EVENTS=<file.jsonl> una riga JSON per evento, con "t" (time.time()) e "ev".
Senza le variabili nessuno di questi ganci fa niente: emit() esce alla prima riga.
"""
import json
import os
import threading
import time
import wave

import numpy as np

ENV_WAV = "WAVETYPE_TEST_WAV"
ENV_EVENTS = "WAVETYPE_TEST_EVENTS"
WAV_BLOCK = 512          # frame per blocco della sorgente finta. Lo stream vero e' aperto con
#                          blocksize=0 (lo sceglie il driver, CoreAudio di solito 512): un valore
#                          fisso vicino a quello e' il massimo che si puo' imitare.
CARD_EVERY = 0.5         # al massimo una riga "card" ogni tanti secondi per fase
LIVE_EVERY = 0.5         # al massimo una riga "live" (parole sulla card) ogni tanti secondi
LIVE_CLIP = 80           # caratteri di testo live nel file eventi

_lock = threading.Lock()
_card_last = {}
_live_last = {"t": None, "text": None}


def events_path():
    return os.environ.get(ENV_EVENTS) or None


def emit(ev, **fields):
    """Appende {"t":..., "ev": ev, ...} al file degli eventi. Non solleva mai."""
    path = events_path()
    if not path:
        return
    try:
        row = {"t": time.time(), "ev": ev}
        row.update(fields)
        line = json.dumps(row, ensure_ascii=False)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def card(x, y, w, h, phase, now=None):
    """Riga "card", al massimo una ogni CARD_EVERY secondi per fase (la card disegna a 30 ms)."""
    if not events_path():
        return False
    now = time.perf_counter() if now is None else now
    last = _card_last.get(phase)
    if last is not None and now - last < CARD_EVERY:
        return False
    _card_last[phase] = now
    emit("card", x=int(x), y=int(y), w=int(w), h=int(h), phase=phase)
    return True


def live(text, phase, now=None):
    """Riga "live" con le parole che la card sta mostrando: solo se il testo e' cambiato
    dall'ultima riga, e al massimo una ogni LIVE_EVERY secondi (la card disegna a 30 ms).
    La CI la usa per dire che le parole live sono apparse durante la registrazione."""
    if not events_path():
        return False
    text = text or ""
    if text == _live_last["text"]:
        return False
    now = time.perf_counter() if now is None else now
    last = _live_last["t"]
    if last is not None and now - last < LIVE_EVERY:
        return False
    _live_last["t"], _live_last["text"] = now, text
    emit("live", chars=len(text), text=clip(text, LIVE_CLIP), phase=phase)
    return True


def clip(s, n):
    """Testo accorciato per il file eventi (None resta None)."""
    if s is None:
        return None
    s = str(s)
    return s[-n:] if len(s) > n else s


# ------------------------------------------------------------------ sorgente WAV finta
def read_wav_any(path):
    """WAV PCM 8/16/24/32 bit, mono o stereo -> (array float32 mono in -1..1, rate)."""
    with wave.open(path, "rb") as w:
        n_ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype("float32") - 128.0) / 128.0
    elif sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype("int32")
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        a = v.astype("float32") / float(1 << 23)
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype("float32") / float(1 << 31)
    else:
        raise ValueError(f"WAV a {sw * 8} bit non gestito")
    if n_ch > 1:
        a = a[: (len(a) // n_ch) * n_ch].reshape(-1, n_ch).mean(axis=1)
    return a.astype("float32"), sr


def resample(a, sr_in, sr_out):
    """Ricampiona per interpolazione lineare (basta per un banco di prova: la voce resta
    intelligibile e la lunghezza in secondi e' esatta)."""
    a = np.asarray(a, dtype="float32").reshape(-1)
    if sr_in == sr_out or len(a) == 0:
        return a.copy()
    n_out = int(round(len(a) * sr_out / float(sr_in)))
    if n_out <= 0:
        return np.zeros(0, dtype="float32")
    x_out = np.arange(n_out, dtype="float64") * (sr_in / float(sr_out))
    return np.interp(x_out, np.arange(len(a), dtype="float64"), a).astype("float32")


def blocks(a, block=WAV_BLOCK):
    """Il segnale a blocchi (block, 1) come li darebbe lo stream; l'ultimo completato di zeri."""
    a = np.asarray(a, dtype="float32").reshape(-1)
    out = []
    for i in range(0, len(a), block):
        b = a[i:i + block]
        if len(b) < block:
            b = np.concatenate([b, np.zeros(block - len(b), dtype="float32")])
        out.append(b.reshape(-1, 1))
    return out


class WavStream:
    """Fa le veci di sounddevice.InputStream: stessa start/stop/close, stesso callback
    (indata, frames, time, status). Consegna il WAV a tempo reale, poi silenzio."""

    def __init__(self, path, samplerate, callback, block=WAV_BLOCK):
        a, sr = read_wav_any(path)
        self._blocks = blocks(resample(a, sr, samplerate), block)
        self.samplerate = samplerate
        self.block = block
        self.callback = callback
        self._stop = threading.Event()
        self._th = None

    def start(self):
        if self._th is not None:
            return
        self._th = threading.Thread(target=self._run, name="test-wav", daemon=True)
        self._th.start()

    def _run(self):
        dt = self.block / float(self.samplerate)
        silence = np.zeros((self.block, 1), dtype="float32")
        t_next = time.perf_counter()
        i = 0
        while not self._stop.is_set():
            b = self._blocks[i] if i < len(self._blocks) else silence
            i += 1
            try:
                self.callback(b.copy(), self.block, None, None)
            except Exception:
                pass
            t_next += dt
            wait = t_next - time.perf_counter()
            if wait > 0:
                self._stop.wait(wait)

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()
        th, self._th = self._th, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=1.0)
