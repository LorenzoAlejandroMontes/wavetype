"""
Motore live LOCALE: anteprima parola per parola, gratis, senza rete.

Stesso contratto di live_engine.py (docs/ENGINE-NOTES.md, "Contratto motore -> pannello"), cosi'
wavetype.py lo scambia con una riga:  import live_local as live_engine

Perche' esiste: live_engine.py rimanda pezzi di audio a Groq (20 richieste/min), quindi le parole
arrivano a scatti, 3-5 s dopo (docs/ENGINE-NOTES.md). Qui un modello di riconoscimento in STREAMING gira
sulla CPU: ogni ~0,3 s di audio produce le parole nuove, senza rimandare nulla.

Il testo finale NON viene da qui: stop() torna sempre None, quindi wavetype.py usa il percorso di oggi
(Groq batch + formattazione LLM) sull'audio intero. Il motore locale serve solo all'anteprima.

Pezzi:
- feed()      -> dal callback audio: accoda e basta (nessun calcolo, nessuna attesa).
- worker      -> scende a 16 kHz, applica il guadagno, passa l'audio al modello a pezzi da 0,16 s,
                 legge l'ipotesi e decide cosa e' fissato (committed) e cosa no (tentative).
                 Se resta indietro piu' di SKIP_AHEAD_SEC salta l'audio arretrato (vedi sotto).
- modello     -> caricato UNA volta in un thread a parte (qualche secondo): nel frattempo l'audio
                 si accumula e viene recuperato appena il modello e' pronto.

Rete di recupero (20/09: "l'anteprima si ferma a 5 parole e resta li'"): il decoder non ha margine
infinito (RTF 0,43 a riposo, 1,17 sotto CPU contesa, banco tests/bench/live_lag/rtf.py). Quando la CPU
e' contesa il worker accumula ritardo e, macinando sempre TUTTA la coda, non lo recupera piu'. Qui il
ritardo si misura sull'orologio e, oltre SKIP_AHEAD_SEC, l'audio arretrato si butta: l'anteprima puo'
permettersi di perdere parole, il testo finale no (stop() torna sempre None, l'incollato viene da Groq).

Regola del fissato (difetto aperto: "a 96 s le tre righe visibili erano tutte grigie"):
- motore a sola aggiunta (transducer in streaming, greedy): una parola seguita da un'altra non
  cambia piu' -> si fissa subito; resta incerta solo l'ultima parola, quella che sta crescendo,
  che si fissa dopo PAUSE_COMMIT secondi senza token nuovi.
- motore che ripensa (Vosk): si fissa una parola quando due ipotesi di fila la dicono uguale ed e'
  finita da COMMIT_LAG secondi; in ogni caso dopo FORCE_LAG secondi si fissa comunque.
- pausa (endpoint del modello): si fissa tutto.
"""
import glob
import os
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")

# ---- CONFIG ----
SR = 16000
SR_IN = 48000                    # rate di cattura di wavetype.py (REC_SR)
STEP_SEC = 0.16                  # pezzo passato al modello per giro
COMMIT_LAG = 0.8                 # motore che ripensa: si fissa dopo tanto dalla fine della parola
FORCE_LAG = 2.0                  # tetto: oltre, una parola si fissa comunque (niente righe grigie)
PAUSE_COMMIT = 0.8               # motore a sola aggiunta: l'ultima parola si fissa dopo tanto silenzio
NUM_THREADS = 4                  # thread ONNX: meta' dei core fisici, l'app e il resto respirano
GAIN_MIN_SEC = 0.5               # prima di tanto audio il guadagno resta 1
SKIP_AHEAD_SEC = 3.0             # oltre tanto ritardo a video l'anteprima salta l'audio arretrato
SKIP_KEEP_SEC = 1.0              # ... e riparte da tanto indietro (il debito non si recupera piu')
BACKEND = "nemotron"             # "nemotron" (sherpa-onnx) | "vosk"
LANGUAGE = "auto"                # lingua: "auto" = la sceglie il modello (si detta anche in inglese);
#                                  misurato su 12 file italiani: auto 98/584 errori, "it" 95/584
NEMOTRON_GLOB = "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-*-int8-*"
NEMOTRON_CHUNK = "320ms"         # variante (chunk 80/160/320/560/1120 ms). Misurato il 20/09 su
#                                  20260914-143848.wav (103,4 s, 4 thread, a riposo): 160 ms -> RTF 0,923,
#                                  320 ms -> RTF 0,428 (meta' del costo) e testo migliore (198 parole
#                                  contro 205, l'attacco del 160 era spazzatura devanagari). Costo:
#                                  parola a video 0,40 -> 0,46 s.
VOSK_GLOB = "vosk-model-small-it-*"
# ----------------

_PUNCT = " \t\n.,;:!?…\"'“”‘’()[]{}«»-–—"


# ---------- helper (stesso comportamento di live_engine.py / wavetype.py) ----------
def downsample_48k_to_16k(a):
    """48k -> 16k (fattore 3) con media a blocchi. Identica a wavetype.py:323."""
    n = (len(a) // 3) * 3
    if n == 0:
        return np.zeros(0, dtype="float32")
    return a[:n].reshape(-1, 3).mean(axis=1).astype("float32")


def gain_for(r):
    """Stesso guadagno di wavetype.py:625 (0.10 / rms, tra 1 e 40)."""
    return float(np.clip(0.10 / r, 1.0, 40.0)) if r > 1e-5 else 1.0


def norm_word(w):
    return w.strip(_PUNCT).lower()


def tokens_to_words(tokens, starts, offset=0.0, tail_end=None):
    """Token BPE (inizio parola = spazio o '▁') -> [(parola, inizio, fine)] in secondi assoluti.
    La fine di una parola e' l'inizio del token successivo; per l'ultima si usa `tail_end`."""
    words = []
    new = True
    for i, tok in enumerate(tokens):
        t = float(starts[i]) + offset if i < len(starts) else offset
        tok = tok.replace("▁", " ")
        if tok.startswith(" "):
            new = True
        piece = tok.strip()
        if not piece:                                    # token di solo spazio (Nemotron lo usa
            continue                                     # come separatore): la prossima e' nuova
        if new or not words:
            if words:
                w, s, _ = words[-1]
                words[-1] = (w, s, t)
            words.append((piece, t, t))
        else:
            w, s, _ = words[-1]
            words[-1] = (w + piece, s, t)
        new = tok.endswith(" ")
    if words:
        w, s, e = words[-1]
        words[-1] = (w, s, max(e + 0.08, tail_end if tail_end is not None else e + 0.08))
    return words


def find_model(pattern, prefer=""):
    """Cartella del modello dentro models/ (None se manca). `prefer` sceglie tra varianti."""
    hits = sorted(p for p in glob.glob(os.path.join(MODELS_DIR, pattern)) if os.path.isdir(p))
    if prefer:
        pick = [p for p in hits if prefer in os.path.basename(p)]
        if pick:
            return pick[0]
    return hits[0] if hits else None


# ---------- motori di riconoscimento ----------
class NemotronBackend:
    """NVIDIA Nemotron 3.5 ASR Streaming 0.6B int8 su sherpa-onnx: streaming vero, 40 lingue
    (italiano incluso). Transducer greedy: l'ipotesi cresce e basta, non ripensa."""
    name = "nemotron"
    append_only = True

    def __init__(self, model_dir=None, language=LANGUAGE, num_threads=NUM_THREADS,
                 chunk=NEMOTRON_CHUNK):
        import sherpa_onnx                               # import pesante: solo qui
        d = model_dir or find_model(NEMOTRON_GLOB, prefer=chunk)
        if not d:
            raise FileNotFoundError(f"modello Nemotron assente in {MODELS_DIR}")
        self.dir = d
        self.language = language

        def f(name):
            hits = glob.glob(os.path.join(d, name))
            if not hits:
                raise FileNotFoundError(f"{name} assente in {d}")
            return hits[0]

        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=f("tokens.txt"), encoder=f("encoder*.onnx"), decoder=f("decoder*.onnx"),
            joiner=f("joiner*.onnx"), num_threads=num_threads, sample_rate=SR,
            decoding_method="greedy_search", enable_endpoint_detection=False)
        # Niente endpoint/reset: misurato su 20260918-170408.wav, dopo reset() il modello perde il
        # filo ("e sulla roadmap che avevi fatto" -> "cento modi diversi") e la coda si tronca;
        # senza reset il testo e' identico alla decodifica offline dello stesso file. La pausa
        # la gestisce la regola PAUSE_COMMIT del motore.

    def new_stream(self):
        s = self.rec.create_stream()
        if self.language:
            try:
                s.set_option("language", self.language)
            except Exception:
                pass
        return s

    def accept(self, st, audio):
        """Passa `audio` (16k) al modello. Ritorna (parole dell'enunciato in corso, endpoint)."""
        s = st["s"]
        s.accept_waveform(SR, audio)
        while self.rec.is_ready(s):
            self.rec.decode_stream(s)
        r = self.rec.get_result_all(s)
        now = st["fed"] / SR
        words = tokens_to_words(list(r.tokens), list(r.timestamps), offset=st["offset"],
                                tail_end=None)
        words = [(w, a, min(b, now)) for w, a, b in words]
        return words, False

    def reset(self, st):
        self.rec.reset(st["s"])
        st["offset"] = st["fed"] / SR

    def finish(self, st):
        s = st["s"]
        s.accept_waveform(SR, np.zeros(int(1.0 * SR), dtype="float32"))   # coda: svuota il chunk
        s.input_finished()
        while self.rec.is_ready(s):
            self.rec.decode_stream(s)
        r = self.rec.get_result_all(s)
        return tokens_to_words(list(r.tokens), list(r.timestamps), offset=st["offset"])


class VoskBackend:
    """Vosk (Kaldi): streaming vero, solo italiano, ripensa le parole nell'ipotesi parziale."""
    name = "vosk"
    append_only = False

    def __init__(self, model_dir=None, pattern=VOSK_GLOB):
        import vosk
        vosk.SetLogLevel(-1)
        d = model_dir or find_model(pattern)
        if not d:
            raise FileNotFoundError(f"modello Vosk assente in {MODELS_DIR}")
        self.dir = d
        self._vosk = vosk
        self.model = vosk.Model(d)

    def new_stream(self):
        r = self._vosk.KaldiRecognizer(self.model, SR)
        r.SetWords(True)
        r.SetPartialWords(True)
        return r

    @staticmethod
    def _words(j, key):
        return [(w["word"], float(w["start"]), float(w["end"])) for w in j.get(key) or []
                if w.get("word")]

    def accept(self, st, audio):
        import json
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        r = st["s"]
        if r.AcceptWaveform(pcm):
            return self._words(json.loads(r.Result()), "result"), True
        return self._words(json.loads(r.PartialResult()), "partial_result"), False

    def reset(self, st):
        pass                                             # Vosk riparte da solo dopo un Result()

    def finish(self, st):
        import json
        return self._words(json.loads(st["s"].FinalResult()), "result")


BACKENDS = {"nemotron": NemotronBackend, "vosk": VoskBackend}
_models = {}                                             # un modello caricato per processo
_models_lock = threading.Lock()


def load_backend(name=BACKEND, **kw):
    """Carica (una volta sola) il motore `name`. Solleva se il modello non c'e'."""
    key = (name, tuple(sorted(kw.items())))
    with _models_lock:
        if key not in _models:
            _models[key] = BACKENDS[name](**kw)
        return _models[key]


# ---------- motore ----------
class LiveEngine:
    """Contratto in docs/ENGINE-NOTES.md. Gli argomenti di live_engine.LiveEngine sono accettati
    (wavetype.py:268 li passa) ma quelli di Groq qui non servono."""

    def __init__(self, groq_key=None, vocab="", log=print, sr_in=SR_IN, backend=BACKEND,
                 backend_kw=None, clock=time.monotonic, **_ignored):
        self.log = log
        self.sr_in = sr_in
        self.clock = clock
        self.backend_name = backend
        self.backend_kw = backend_kw or {}
        self.backend = None
        self._ready = threading.Event()
        self._load_err = None
        self._lock = threading.Lock()
        self._thread = None
        self._gen = 0
        self._reset()
        threading.Thread(target=self._load, name="live-local-load", daemon=True).start()

    # ---- caricamento modello (una volta, in sottofondo) ----
    def _load(self):
        t0 = time.perf_counter()
        try:
            self.backend = load_backend(self.backend_name, **self.backend_kw)
            self.log(f"[live-local] modello {self.backend_name} pronto in "
                     f"{time.perf_counter() - t0:.1f}s ({os.path.basename(self.backend.dir)})")
        except Exception as e:
            self._load_err = e
            self.log(f"[live-local] modello non caricato: {e}")
        finally:
            self._ready.set()

    # ---- stato ----
    def _reset(self):
        self._pending = []
        self._carry = np.zeros(0, dtype="float32")
        self._fed16 = 0                  # campioni a 16k gia' passati al modello
        self._sumsq = 0.0
        self._total = 0
        self._committed = ""
        self._tentative = ""
        self._rev = 0
        self._cwords = []                # parole fissate con (inizio, fine): servono a test e banco
        self._skipped = 0.0              # secondi di audio saltati per recuperare (solo anteprima)
        self._utt_done = 0               # parole dell'enunciato in corso gia' fissate
        self._prev = []                  # ipotesi precedente (motore che ripensa)
        self._stopping = False
        self._finish = False
        self._done = threading.Event()
        self._st = None

    # ---- API ----
    def start(self):
        """Nuova dettatura: un worker nuovo; quello vecchio (se c'e') si accorge e se ne va."""
        self._stopping = True
        with self._lock:
            self._gen += 1
            self._reset()
            gen = self._gen
        self._thread = threading.Thread(target=self._loop, args=(gen,), name="live-local",
                                        daemon=True)
        self._thread.start()

    def feed(self, block):
        """Dal callback PortAudio: SOLO accodare."""
        try:
            a = np.array(block, dtype="float32", copy=True).reshape(-1)
        except Exception:
            return
        with self._lock:
            self._pending.append(a)

    def snapshot(self):
        with self._lock:
            return {"committed": self._committed, "tentative": self._tentative, "rev": self._rev}

    def stop(self, timeout=30):
        """Il testo finale lo fa sempre il percorso di oggi (Groq batch): None subito, niente
        attesa sull'incolla. Il worker si chiude da solo."""
        self._stopping = True
        return None

    def cancel(self):
        self._stopping = True
        t = self._thread
        if t is not None and t.is_alive():
            t.join(0.5)

    def finish(self, timeout=30):
        """Per test e banco: elabora tutto l'audio arrivato, fissa tutto, ritorna il testo."""
        self._finish = True
        self._stopping = True
        self._done.wait(timeout)
        return self.snapshot()["committed"]

    def committed_words(self):
        with self._lock:
            return list(self._cwords)

    # ---- interno ----
    def _loop(self, gen):
        try:
            self._ready.wait()
            if self.backend is None:
                return
            self._st = {"s": self.backend.new_stream(), "fed": 0, "offset": 0.0}
            buf = np.zeros(0, dtype="float32")
            step = int(STEP_SEC * SR)
            t0 = self.clock()                            # da qui in poi l'audio arriva in tempo reale
            while gen == self._gen:
                buf = np.concatenate([buf, self._drain()])
                buf = self._catch_up(buf, step, t0)
                if len(buf) >= step:
                    n = (len(buf) // step) * step
                    self._process(buf[:n], gen)
                    buf = buf[n:]
                    continue
                if self._stopping:
                    if self._finish:
                        if len(buf):
                            self._process(buf, gen)
                        self._flush()
                    return
                time.sleep(0.02)
        except Exception as e:
            self.log(f"[live-local] worker: {e}")
        finally:
            if gen == self._gen:
                self._done.set()

    def _catch_up(self, buf, step, t0):
        """Ritardo a video = tempo passato - audio gia' dato al modello (piu' quello saltato).
        Oltre SKIP_AHEAD_SEC butta l'arretrato e riparte da SKIP_KEEP_SEC indietro: senza questo un
        momento di CPU contesa lascia un debito che il decoder non recupera piu' (misurato 20/09:
        +58,9 s su 103 s di dettatura). Chi alimenta piu' veloce del tempo reale (test, banco) ha
        ritardo negativo e non salta mai."""
        lag = (self.clock() - t0) - (self._st["fed"] / SR + self._skipped)
        if lag <= SKIP_AHEAD_SEC or len(buf) <= step:
            return buf
        drop = min(int((lag - SKIP_KEEP_SEC) * SR), len(buf) - step)
        drop = (drop // step) * step
        if drop <= 0:
            return buf
        self._skipped += drop / SR
        self.log(f"[live-local] anteprima indietro di {lag:.1f}s: saltati {drop / SR:.1f}s "
                 f"(il testo incollato non ne risente)")
        return buf[drop:]

    def _drain(self):
        with self._lock:
            pend, self._pending = self._pending, []
        if not pend:
            return np.zeros(0, dtype="float32")
        a = np.concatenate([self._carry] + pend) if len(self._carry) else np.concatenate(pend)
        if self.sr_in == SR:
            new, self._carry = a, np.zeros(0, dtype="float32")
        else:
            n = (len(a) // 3) * 3
            new, self._carry = downsample_48k_to_16k(a[:n]), a[n:]
        self._total += len(new)
        self._sumsq += float(np.sum(new.astype("float64") ** 2))
        return new

    def _gain(self):
        """Guadagno di wavetype.py sull'intera dettatura finora (il microfono puo' essere basso: rms
        0,002-0,003 in wavetype.log, docs/ENGINE-NOTES.md)."""
        if self._total < GAIN_MIN_SEC * SR:
            return 1.0
        return gain_for(float(np.sqrt(self._sumsq / self._total)))

    def _process(self, audio, gen):
        g = self._gain()
        if g > 1.2:
            audio = np.clip(audio * g, -1.0, 1.0)
        st = self._st
        st["fed"] += len(audio)
        words, endpoint = self.backend.accept(st, audio)
        if gen != self._gen:                             # uno start() nuovo ci ha sorpassati:
            return                                       # queste parole non sono sue
        self._update(words, endpoint, st["fed"] / SR)
        if endpoint:
            self.backend.reset(st)

    def _update(self, words, endpoint, now):
        """Ipotesi dell'enunciato in corso -> committed/tentative."""
        done = self._utt_done
        if endpoint:
            k = len(words)
        else:
            k = done
            for i in range(done, len(words)):
                w, s, e = words[i]
                last = i == len(words) - 1
                if self.backend.append_only:
                    ok = not last or e <= now - PAUSE_COMMIT
                else:
                    same = i < len(self._prev) and norm_word(self._prev[i][0]) == norm_word(w)
                    ok = (not last and same and e <= now - COMMIT_LAG) or e <= now - FORCE_LAG
                if not ok:
                    break
                k = i + 1
        new = words[done:k] if k > done else []
        tent = " ".join(w for w, _, _ in words[max(k, done):]) if not endpoint else ""
        with self._lock:
            changed = False
            if new:
                add = " ".join(w for w, _, _ in new)
                self._committed = (self._committed + " " + add).strip()
                self._cwords.extend(new)
                changed = True
            if tent != self._tentative:
                self._tentative = tent
                changed = True
            if changed:
                self._rev += 1
        self._prev = words
        self._utt_done = 0 if endpoint else max(k, done)

    def _flush(self):
        words = self.backend.finish(self._st)
        self._update(words, True, self._st["fed"] / SR)
