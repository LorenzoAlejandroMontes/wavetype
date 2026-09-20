"""
Motore live (Lotto B di docs/ENGINE-NOTES.md): trascrive mentre parli, cosi' allo stop resta da
trascrivere solo la coda e l'attesa crolla.

Groq non ha streaming: si rimanda audio. Qui si manda SOLO la coda non ancora fissata (il prefisso
intero costa: 0,56 s a 3 s di audio -> 2,5 s a 40 s, misurato in docs/ENGINE-NOTES.md).

Pezzi:
- feed()      -> chiamata dal callback audio: accoda e basta, nessun calcolo pesante, nessuna rete.
- worker      -> ogni ~4 s manda la coda non fissata a Groq (verbose_json + timestamp per parola),
                 il risultato diventa `tentative`; quando trova una pausa nella coda taglia li' e
                 sposta in `committed` le parole che stanno prima del taglio.
- stop()      -> un'ultima richiesta sulla coda, senza attesa di ritmo, poi committed + coda.
                 Torna None (mai eccezione, mai blocco) se qualcosa va storto: wavetype.py ricade
                 sul percorso di oggi, che ha comunque l'audio intero.

Vincoli duri del piano gratis Groq, PER MODELLO (console.groq.com/docs/rate-limits, letti il
2026-09-19): whisper-large-v3-turbo 20 richieste/min, 2.000/giorno, 7.200 s di audio/ora,
28.800 s di audio/giorno, minimo fatturato 10 s a richiesta. Sono tetti del SOLO STT: la
formattazione finale gira su un altro modello (openai/gpt-oss-120b) e ha i suoi, separati; una
richiesta di formattazione non toglie nulla a questi. Lo stesso tetto e' invece condiviso da tutto
cio' che usa la stessa chiave sullo stesso modello: l'app in esecuzione, il percorso batch di
wavetype.py e le sessioni di prova. Il governatore qui dentro tiene 15 richieste/min di ritmo e
18/min di tetto, non manda nulla se l'audio nuovo e' silenzio (Whisper sul silenzio allucina
"Grazie"/"Thank you.": 29 dettature su 215, docs/ENGINE-NOTES.md) e fa backoff su 429
leggendo `retry-after`. I conti che durano piu' di una dettatura (giorno, ora, 429 ripetuti) non
stanno qui: li tiene la guardia di live_dual.py, che si inietta come `guard=`.

Non importa wavetype.py di proposito: quel modulo importa sounddevice, keyboard, faster_whisper e
legge i file dell'app (effetti collaterali all'import). Le poche funzioni condivise sono ricopiate
qui sotto identiche (downsample, guadagno, energia a finestre di 0,2 s, regola sulla lingua).
"""
import io
import threading
import time
import wave
from collections import deque

import numpy as np

try:
    import httpx
except Exception:                      # senza httpx il motore semplicemente non parte
    httpx = None

# ---- CONFIG ----
SR = 16000                       # rate mandato a Groq (come wavetype.py)
SR_IN = 48000                    # rate di cattura (come wavetype.py: REC_SR)
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"

PACE_SEC = 4.0                   # ritmo: una richiesta ogni ~4 s = 15/min, margine sul tetto di 20
MAX_RPM = 18                     # tetto duro sulla finestra di 60 s reali (20 e' il limite Groq)
MIN_SEND_SEC = 3.0               # coda piu' corta di cosi': non vale una richiesta. Misurato:
#                                  su 1,5 s di coda Whisper tira a indovinare ("che mala gran
#                                  madre" al posto di "Sistema la grammatica"), e l'anteprima nasce
#                                  sbagliata; a 3 s l'ipotesi regge (ed e' il passo del probe).
COMMIT_MIN_SEC = 6.0             # si fissa solo se prima del taglio c'e' almeno tanto audio
COMMIT_HARD_SEC = 25.0           # oltre: si taglia comunque (nel punto piu' silenzioso)
PAUSE_MIN_SEC = 0.5              # pausa minima per considerarla una cesura
PAUSE_WIN_SEC = 0.2              # finestra di energia (stessa di split_points in wavetype.py)
EDGE_GUARD_SEC = 0.6             # non si taglia a ridosso del bordo vivo: potrebbe essere mezza parola
SILENCE_RMS = 0.003              # sotto = silenzio (soglia misurata sull'archivio, UPGRADE §1)
SILENCE_PEAK = 0.005             # ...ma solo se ANCHE la finestra piu' forte sta sotto: stessa
SILENCE_WIN = 0.3                # regola di silence_check in wavetype.py:71-76. Senza, una dettatura
SILENCE_HOP = 0.1                # detta piano (rms 0,0029, picco 0,0092) veniva buttata.
MAX_FINAL_SEC = 600              # coda massima per la richiesta finale: Groq rifiuta oltre ~25 MB
#                                  (wavetype.py:156 spezza gia' a 20 MB = 625 s a 16k/16 bit). Sopra
#                                  si torna al batch, che sa spezzare; noi no.
STT_TIMEOUT = 25.0               # richiesta di giro
STT_TIMEOUT_FINAL = 30.0         # richiesta finale
MAX_FAILS = 3                    # tanti errori di fila = motore degradato
AUDIO_SEC_HOUR = 7200            # limite Groq: secondi di audio all'ora (solo modello STT)
AUDIO_SEC_SLOW = 5000            # oltre: si rallenta il ritmo (era 6000: troppo vicino al tetto,
#                                  il Pacer conta solo la dettatura in corso e non vede l'app)
AUDIO_SEC_STOP = 5760            # oltre: niente piu' giri live, resta solo la richiesta finale.
#                                  5.760 = 80% di 7.200, la stessa soglia della guardia di
#                                  live_dual.py: il margine serve al batch e alle altre sessioni
PROMPT_TAIL_WORDS = 40           # coda di committed passata come prompt (come groq_transcribe_long)
OK_LANGS = ("it", "en", "es")
# ----------------


# ---------- helper audio (copiati da wavetype.py, stesso comportamento) ----------
def downsample_48k_to_16k(a):
    """48k -> 16k (fattore 3) con media a blocchi. Identica a wavetype.py:323."""
    n = (len(a) // 3) * 3
    if n == 0:
        return np.zeros(0, dtype="float32")
    return a[:n].reshape(-1, 3).mean(axis=1).astype("float32")


def rms(a):
    if len(a) == 0:
        return 0.0
    return float(np.sqrt(np.mean(a.astype("float64") ** 2)))


def gain_for(r):
    """Stesso guadagno di wavetype.py:625 (0.10 / rms, tra 1 e 40)."""
    return float(np.clip(0.10 / r, 1.0, 40.0)) if r > 1e-5 else 1.0


def wav_bytes(a, sr=SR):
    """WAV mono 16 bit in memoria (niente file temporanei: il disco non serve)."""
    pcm = (np.clip(a, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def frame_rms(a, sr, win=PAUSE_WIN_SEC, hop=0.05):
    """Energia (rms) su finestre scorrevoli di `win` secondi. Ritorna (valori, passo in campioni)."""
    w, h = int(win * sr), int(hop * sr)
    if len(a) < w or w <= 0 or h <= 0:
        return np.zeros(0), h
    c = np.concatenate(([0.0], np.cumsum(a.astype("float64") ** 2)))
    idx = np.arange(1 + (len(a) - w) // h) * h
    return np.sqrt((c[idx + w] - c[idx]) / w), h


def has_speech(a):
    """C'e' parlato in `a`? Regola di wavetype.silence_check: silenzio solo se l'rms globale E la
    finestra piu' forte stanno sotto soglia. Un 'Si'' detto piano dentro una coda lunga alza il
    picco ma non l'rms: qui non deve passare per silenzio, o la dettatura sparisce."""
    if len(a) == 0:
        return False
    if rms(a) >= SILENCE_RMS:
        return True
    e, _ = frame_rms(a, SR, win=SILENCE_WIN, hop=SILENCE_HOP)
    return len(e) > 0 and float(np.max(e)) >= SILENCE_PEAK


def find_pause(a, sr, min_lead=COMMIT_MIN_SEC, min_pause=PAUSE_MIN_SEC,
               edge_guard=EDGE_GUARD_SEC, forced=False):
    """Indice di taglio dentro `a`, o None.

    Cerca l'ULTIMA pausa (corsa di finestre sotto soglia lunga almeno `min_pause`) che cada dopo
    `min_lead` secondi e prima del bordo vivo. `forced` = si e' oltre il tetto duro: se non c'e' una
    pausa vera si taglia comunque sulla finestra piu' silenziosa della zona ammessa."""
    e, hop = frame_rms(a, sr)
    if len(e) == 0:
        return None
    lo = int(min_lead * sr / hop)                       # prima finestra ammessa
    hi = len(e) - int(edge_guard * sr / hop)            # ultima finestra ammessa
    if hi <= lo:
        return None
    speech = float(np.percentile(e, 90)) if len(e) else 0.0
    thr = max(SILENCE_RMS * 1.5, 0.12 * speech)
    quiet = e[lo:hi] < thr
    hop_sec = hop / sr                                  # N finestre coprono win + (N-1)*hop secondi
    need = max(1, int(np.ceil((min_pause - PAUSE_WIN_SEC) / hop_sec)) + 1)
    best = None
    run = 0
    for i, q in enumerate(quiet):
        run = run + 1 if q else 0
        if run >= need:
            best = (i - run + 1, i)                     # l'ultima corsa buona vince: fissa di piu'
    if best is None:
        if not forced:
            return None
        j = lo + int(np.argmin(e[lo:hi]))               # tetto duro: il punto meno peggio
        return int(j * hop + int(PAUSE_WIN_SEC * sr) // 2)
    mid = lo + (best[0] + best[1]) // 2
    return int(mid * hop + int(PAUSE_WIN_SEC * sr) // 2)


# ---------- helper testo ----------
_PUNCT = " \t\n.,;:!?…\"'“”‘’()[]{}«»-–—"


def norm_word(w):
    return w.strip(_PUNCT).lower()


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def dedupe_overlap(left, right, max_k=8, min_k=2):
    """Toglie da `right` le prime k parole se ripetono le ultime k di `left` (eco del prompt:
    la coda di committed viene passata come prompt e Whisper a volte la riscrive)."""
    # solo la CODA di `left` serve (si confrontano al piu' max_k parole): su `left` intero il
    # costo cresce con la dettatura e lo paga il lock che tocca anche feed() dal callback audio
    # (misurato: 5,5 ms a 20.000 parole, 0,06 ms a 200).
    lw = [norm_word(x) for x in left.split()[-8 * max_k:] if norm_word(x)]
    rw = right.split()
    rn = [norm_word(x) for x in rw]
    for k in range(min(max_k, len(lw), len(rn)), min_k - 1, -1):
        if lw[-k:] == rn[:k]:
            return " ".join(rw[k:]), k
    return right, 0


def join_text(left, right):
    """Unisce due pezzi togliendo la sovrapposizione al giunto."""
    left, right = (left or "").strip(), (right or "").strip()
    if not left:
        return right
    if not right:
        return left
    right, _ = dedupe_overlap(left, right)
    return (left + " " + right).strip() if right else left


def lang_code(lang):
    """Groq risponde 'Italian', l'API accetta 'it'. Identica a wavetype.py:227."""
    l = (lang or "").strip().lower()
    return {"italian": "it", "english": "en", "spanish": "es"}.get(l, l if len(l) == 2 else "")


# ---------- governatore di ritmo ----------
class Pacer:
    """Ritmo + limiti DELLA SOLA DETTATURA IN CORSO (si azzera a ogni start()). Due orologi di
    proposito: `clock` puo' essere finto (test accelerati), ma la finestra delle 20 richieste/min
    usa SEMPRE il tempo reale, perche' il limite e' reale ed e' condiviso con tutto cio' che usa
    la stessa chiave sullo stesso modello STT (app, percorso batch, altre sessioni).
    I conti che attraversano le dettature stanno nella guardia di live_dual.py."""

    def __init__(self, interval=PACE_SEC, max_rpm=MAX_RPM, clock=time.monotonic, real_clock=None):
        self.interval = interval
        self.max_rpm = max_rpm
        self.clock = clock
        self.real_clock = real_clock or time.monotonic
        self.sent = deque()          # istanti reali delle richieste (finestra 60 s)
        self.audio = deque()         # (istante reale, secondi di audio) finestra 3600 s
        self.last = None             # istante (orologio di ritmo) dell'ultima richiesta
        self.block_until = 0.0       # backoff dopo un 429 (orologio reale)
        self.tries = 0
        self.headers = {}            # ultimi x-ratelimit-* letti

    def _prune(self, now):
        while self.sent and now - self.sent[0] > 60.0:
            self.sent.popleft()
        while self.audio and now - self.audio[0][0] > 3600.0:
            self.audio.popleft()

    def audio_sec_hour(self):
        self._prune(self.real_clock())
        return sum(s for _, s in self.audio)

    def interval_now(self):
        """Ritmo effettivo: si allarga se la quota audio dell'ora si sta consumando o se Groq
        dichiara poche richieste rimaste."""
        iv = self.interval
        if self.audio_sec_hour() > AUDIO_SEC_SLOW:
            iv = max(iv, 8.0)
        try:
            left = int(self.headers.get("x-ratelimit-remaining-requests", "9999"))
            if left < 50:
                iv = max(iv, 10.0)
        except Exception:
            pass
        return iv

    def exhausted(self):
        return self.audio_sec_hour() > AUDIO_SEC_STOP

    def wait_for_next(self):
        """Secondi da aspettare prima della prossima richiesta (0 = si puo' mandare)."""
        now_r = self.real_clock()
        self._prune(now_r)
        waits = [0.0]
        if now_r < self.block_until:
            waits.append(self.block_until - now_r)
        if self.last is not None:
            waits.append(self.last + self.interval_now() - self.clock())
        if len(self.sent) >= self.max_rpm:
            waits.append(60.0 - (now_r - self.sent[0]) + 0.05)
        return max(waits)

    def note_request(self, audio_sec=0.0):
        now_r = self.real_clock()
        self.sent.append(now_r)
        self.audio.append((now_r, audio_sec))
        self.last = self.clock()
        self._prune(now_r)

    def note_ok(self, headers=None):
        self.tries = 0
        if headers:
            self.headers = {k.lower(): v for k, v in dict(headers).items()
                            if k.lower().startswith("x-ratelimit")}

    def note_429(self, retry_after=None):
        """Backoff esponenziale, ma se Groq dice quanto aspettare si rispetta quello."""
        self.tries += 1
        wait = 1.0 * (3 ** (self.tries - 1))
        try:
            if retry_after is not None:
                wait = max(wait, float(str(retry_after).rstrip("s")))
        except Exception:
            pass
        self.block_until = self.real_clock() + min(wait, 60.0)
        return wait


# ---------- motore ----------
class LiveEngine:
    """Contratto in docs/ENGINE-NOTES.md. Riusabile: start() azzera tutto e non lascia thread."""

    def __init__(self, groq_key, vocab="", log=print, sr_in=SR_IN,
                 http_post=None, clock=time.monotonic, pace=PACE_SEC,
                 final_policy="live", batch_under_sec=30.0, min_live_sec=0.0, guard=None):
        self.key = groq_key
        self.vocab = vocab or ""
        self.log = log
        self.sr_in = sr_in
        self.clock = clock
        self.pace = pace
        # "live": il testo finale e' quello cucito dal live.
        # "batch_under": sotto batch_under_sec stop() torna None -> wavetype.py usa il percorso di oggi
        # (gia' veloce li': 1,56 s mediana sotto i 10 s) e il live resta solo anteprima.
        self.final_policy = final_policy
        self.batch_under_sec = batch_under_sec
        # Nessuna richiesta finche' la dettatura non supera questi secondi: sotto i ~10 s
        # l'anteprima non fa in tempo a comparire e ogni richiesta e' regalata al limite condiviso.
        self.min_live_sec = min_live_sec
        # Guardia dei limiti che durano oltre la dettatura (live_dual.Guard, o qualunque oggetto
        # con start/allow/note_request/note_headers/note_429). Assente = si va come prima.
        self.guard = guard
        self._http_post = http_post
        self._client = None
        self._lock = threading.Lock()
        self._thread = None
        self._reset()

    # ---- stato ----
    def _reset(self):
        self._pending = []           # blocchi come arrivano dal callback (sr_in)
        self._carry = np.zeros(0, dtype="float32")   # resto non divisibile per 3
        self._blocks = []            # blocchi a 16k ancora utili
        self._base = 0               # indice assoluto del primo campione in _blocks
        self._total = 0              # campioni a 16k ricevuti
        self._sumsq = 0.0            # energia totale: serve al guadagno, che resta uguale per tutti
        self._last_req_end = 0       # fin dove l'ultima richiesta ha gia' guardato
        self._commit_idx = 0         # indice assoluto del punto fissato
        self._committed = ""
        self._tentative = ""
        self._rev = 0
        self._lang = ""
        self._lang_pinned = ""
        self._prev_words = []        # ipotesi precedente (LocalAgreement)
        self._fails = 0
        self._degraded = False
        self._stopping = False
        self._cancelled = False
        self._guard_off = False      # la guardia ha detto no: niente piu' richieste in questa
        #                              dettatura, il testo finale lo fa il percorso batch
        self._incomplete = False     # del parlato non e' mai arrivato a una trascrizione: il
        #                              testo live sarebbe monco -> stop() torna None (batch)
        self._result = None
        self._requests = 0
        self._req_log = []           # istanti reali delle richieste: serve solo alle misure
        self._wake = threading.Event()
        self._done = threading.Event()
        self._pacer = Pacer(interval=self.pace, clock=self.clock)

    # ---- API ----
    def start(self):
        """Nuova dettatura. Se un worker vecchio e' ancora vivo lo si chiude prima: niente thread
        appesi tra una dettatura e l'altra."""
        if self._thread is not None and self._thread.is_alive():
            self.cancel()
        with self._lock:
            self._reset()
        self._guard_call("start")
        if self._http_post is None:
            if httpx is None:
                self.log("[live] httpx assente: motore live non disponibile")
                self._degraded = True
                return
            self._client = httpx.Client(timeout=STT_TIMEOUT)
        self._thread = threading.Thread(target=self._loop, name="live-engine", daemon=True)
        self._thread.start()

    def feed(self, block):
        """Dal callback PortAudio: SOLO accodare. Niente rete, niente numpy pesante, niente attese."""
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
        """Ultima richiesta sulla coda, poi committed + coda. None = fallito: usa il percorso batch."""
        try:
            if self._thread is None:
                return None
            self._stopping = True
            self._wake.set()
            if not self._done.wait(timeout):
                self.log("[live] stop: scaduto il tempo, ricado sul percorso batch")
                self.cancel()
                return None
            self._join_thread()
            self._close_client()
            return self._result
        except Exception as e:                       # stop() non solleva MAI
            self.log(f"[live] stop: errore {e}")
            try:
                self.cancel()
            except Exception:
                pass
            return None

    def cancel(self):
        """Ferma tutto in fretta: il client chiuso fa cadere anche la richiesta in volo."""
        self._cancelled = True
        self._stopping = True
        self._wake.set()
        self._close_client()
        self._join_thread(0.5)

    # ---- interno ----
    def _join_thread(self, timeout=2.0):
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        if t is not None and not t.is_alive():
            self._thread = None

    def _close_client(self):
        c, self._client = self._client, None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _starve(self):
        """Motore fermo (degradato o quota oraria finita): l'audio che continua ad arrivare non
        sara' mai trascritto da noi e a 64 KB/s riempie la RAM (misurato: 600 s tenuti = 38 MB;
        su una dettatura da 2h38m sarebbero 607 MB). Lo si butta e si dichiara il risultato
        inutilizzabile: il testo finale lo fa il percorso batch, che ha comunque l'audio intero."""
        if self._blocks:
            self._commit_idx = self._total
            self._drop_committed()
        self._incomplete = True

    def _loop(self):
        me = threading.current_thread()
        try:
            while not self._stopping and not self._cancelled:
                if self._thread is not me:    # uno start() nuovo ha gia' messo un altro worker:
                    return                    # in due sullo stesso stato ci si pesta i piedi
                self._drain()
                if self._degraded or self._guard_off or self._pacer.exhausted():
                    self._starve()
                    self._wake.wait(0.5)
                    continue
                w = self._pacer.wait_for_next()
                if w > 0:
                    self._wake.wait(min(w, 0.2))
                    continue
                if not self._new_speech():
                    self._wake.wait(0.2)
                    continue
                self._cycle(final=False)
            if self._cancelled or self._thread is not me:
                return
            self._drain()
            self._final()
        except Exception as e:
            self.log(f"[live] worker: {e}")
            self._result = None
        finally:
            if self._thread is me:      # un worker sorpassato non sveglia la stop() della
                self._done.set()        # dettatura nuova con un risultato che non e' suo

    def _drain(self):
        """Sposta i blocchi accodati da feed() in coda all'audio a 16k. Il resto non divisibile per
        3 viene riportato al giro dopo: nessun campione perso, timestamp che non slittano."""
        with self._lock:
            pend, self._pending = self._pending, []
        if not pend:
            return
        a = np.concatenate([self._carry] + pend) if len(self._carry) else np.concatenate(pend)
        if self.sr_in == SR:
            new, self._carry = a, np.zeros(0, dtype="float32")
        else:
            n = (len(a) // 3) * 3
            new, self._carry = downsample_48k_to_16k(a[:n]), a[n:]
        if len(new):
            self._blocks.append(new)
            self._total += len(new)
            self._sumsq += float(np.sum(new.astype("float64") ** 2))

    def _gain(self):
        """Stesso guadagno di wavetype.py ma calcolato sull'INTERA dettatura finora, non sul pezzo:
        cosi' i pezzi non arrivano a Groq con volumi diversi tra loro."""
        if self._total < int(0.5 * SR):
            return 1.0
        return gain_for(float(np.sqrt(self._sumsq / self._total)))

    def _uncommitted(self):
        if not self._blocks:
            return np.zeros(0, dtype="float32")
        a = np.concatenate(self._blocks)
        off = self._commit_idx - self._base
        return a[max(0, off):]

    def _drop_committed(self):
        """Butta i blocchi interamente prima del punto fissato: la RAM resta ~25 s di audio."""
        while self._blocks and self._base + len(self._blocks[0]) <= self._commit_idx:
            self._base += len(self._blocks.pop(0))

    def _new_speech(self):
        """Si manda solo se c'e' abbastanza coda E se l'audio nuovo non e' silenzio: sul silenzio
        Whisper inventa ("Grazie", "Thank you."), e ogni richiesta inutile mangia il limite."""
        if self._total < self.min_live_sec * SR:
            return False
        seg = self._uncommitted()
        if len(seg) < MIN_SEND_SEC * SR:
            return False
        if rms(seg) < SILENCE_RMS:
            return False
        last = self._last_req_end
        fresh = self._total - max(last, self._commit_idx)
        if fresh <= 0:
            return False
        if last > self._commit_idx:                  # gia' mandata parte di questa coda
            tail = seg[-fresh:] if fresh <= len(seg) else seg
            if len(tail) >= 0.5 * SR and rms(tail) < SILENCE_RMS:
                return False                          # solo silenzio in piu': niente richiesta
        return True

    def _prompt(self):
        bits = [b for b in (self.vocab, " ".join(self._committed.split()[-PROMPT_TAIL_WORDS:])) if b]
        return " ".join(bits)

    def _post(self, wav, data, timeout):
        if self._http_post is not None:
            return self._http_post(wav, data, timeout)
        return self._client.post(
            GROQ_URL, headers={"Authorization": f"Bearer {self.key}"}, timeout=timeout,
            files={"file": ("a.wav", wav, "audio/wav")}, data=data)

    def _guard_call(self, name, *a):
        """Chiama la guardia senza mai farla pesare sul motore: un difetto li' non deve fermare
        una dettatura (il Pacer tiene comunque il tetto al minuto)."""
        g = self.guard
        if g is None:
            return None
        try:
            return getattr(g, name)(*a)
        except Exception as e:
            self.log(f"[live] guardia ({name}): {e}")
            return None

    def _guard_ok(self, audio_sec):
        """La guardia prima di OGNI richiesta. False = questa dettatura passa al percorso batch."""
        if self.guard is None:
            return True
        out = self._guard_call("allow", audio_sec)
        return True if out is None else bool(out)

    def _transcribe(self, seg, final):
        """Una richiesta sulla coda `seg` (16k). Ritorna (testo, parole, lingua) o None."""
        if not self._guard_ok(len(seg) / SR):
            self._guard_off = True       # niente altre richieste live in questa dettatura: il
            return None                  # testo finale lo fa il percorso batch (audio intero)
        g = self._gain()
        audio = np.clip(seg * g, -1.0, 1.0) if g > 1.2 else seg
        data = {"model": GROQ_STT_MODEL, "response_format": "verbose_json",
                "timestamp_granularities[]": "word"}
        p = self._prompt()
        if p:
            data["prompt"] = p
        if self._lang_pinned:
            data["language"] = self._lang_pinned
        try:
            self._pacer.note_request(len(seg) / SR)
            self._guard_call("note_request", len(seg) / SR)
            self._requests += 1
            self._req_log.append(time.monotonic())
            r = self._post(wav_bytes(audio), data, STT_TIMEOUT_FINAL if final else STT_TIMEOUT)
            if r.status_code == 429:
                w = self._pacer.note_429(r.headers.get("retry-after"))
                self._guard_call("note_429", r.headers.get("retry-after"))
                self._guard_off = True   # un 429 spegne il live per questa dettatura: ritentare
                #                          dentro la stessa dettatura ha gia' l'aria del 429 dopo
                self.log(f"[live] 429: aspetto {w:.1f}s")
                return None
            if r.status_code != 200:
                self._fails += 1
                self.log(f"[live] http {r.status_code}: {str(r.text)[:120]}")
                if self._fails >= MAX_FAILS:
                    self._degraded = True
                return None
            self._pacer.note_ok(getattr(r, "headers", None))
            self._guard_call("note_headers", getattr(r, "headers", None))
            j = r.json()
        except Exception as e:
            self._fails += 1
            if self._fails >= MAX_FAILS:
                self._degraded = True
            self.log(f"[live] richiesta fallita: {e}")
            return None
        self._fails = 0
        lang = j.get("language", "") or ""
        words = [w for w in (j.get("words") or []) if (w.get("word") or "").strip()]
        text = (j.get("text") or "").strip()
        if not words and text:                       # niente timestamp: parole senza tempi
            words = [{"word": w, "start": 0.0, "end": 0.0} for w in text.split()]
        self._note_lang(lang, len(seg) / SR, text)
        return text, words, lang

    def _note_lang(self, lang, dur, text):
        """Prima richiesta in auto-detect; poi si fissa la lingua, cosi' i pezzi non cambiano idea a
        meta' dettatura. Regola di wavetype.py:566: fuori da IT/EN/ES -> italiano."""
        if not lang or self._lang_pinned:
            return
        code = lang_code(lang)
        if dur < 1.5 or not text:
            return
        if code not in OK_LANGS:
            self.log(f"[live] lingua '{lang}' fuori da IT/EN/ES -> fisso italiano")
            code = "it"
        self._lang_pinned = code
        self._lang = code

    def _cycle(self, final=False):
        seg = self._uncommitted()
        if len(seg) < (0.3 if final else MIN_SEND_SEC) * SR:
            if final:
                self._set_tentative("")
            return
        # coda muta: non si manda e non si scrive nulla. All'ultimo giro il metro e' quello di
        # wavetype.py (rms E picco): sbagliare li' significa buttare la dettatura, non un'anteprima.
        if (not has_speech(seg)) if final else (rms(seg) < SILENCE_RMS):
            if final:
                self._set_tentative("")
            return
        req_end = self._commit_idx + len(seg)
        out = self._transcribe(seg, final)
        if out is None:
            if final:                    # la coda non e' mai stata trascritta: il testo sarebbe
                self._incomplete = True  # monco (misurato: 10 parole su 24 perse su un 429)
            return
        if self._thread is not threading.current_thread():
            return                       # sorpassati mentre eravamo in rete: queste parole sono
            #                              della dettatura prima, non si scrivono in questa
        text, words, _ = out
        text, cut_k = dedupe_overlap(self._committed, text)      # eco del prompt
        if cut_k:
            words = words[cut_k:]
        k = 0 if final else self._maybe_commit(seg, words)
        # `tentative` e' SEMPRE solo la coda: se in questo giro si e' fissato qualcosa, quelle parole
        # sono gia' in `committed` e il pannello non le deve ricevere due volte.
        self._set_tentative(" ".join(w["word"].strip() for w in words[k:]).strip() if k else text)
        self._last_req_end = req_end

    def _set_tentative(self, t):
        with self._lock:
            if t != self._tentative:
                self._tentative = t
                self._rev += 1

    def _maybe_commit(self, seg, words):
        """Fissa le parole che stanno prima della pausa. Ritorna quante ne ha fissate."""
        dur = len(seg) / SR
        forced = dur >= COMMIT_HARD_SEC
        cut = find_pause(seg, SR, forced=forced)
        if cut is None or cut <= 0:
            self._prev_words = words
            return 0
        t_cut = cut / SR
        k = sum(1 for w in words if float(w.get("end") or 0.0) <= t_cut)
        if k == 0:
            if rms(seg[:cut]) < SILENCE_RMS:         # solo silenzio prima del taglio: avanza e basta
                self._commit_idx += cut
                self._drop_committed()
            self._prev_words = words
            return 0
        # LocalAgreement: si fissa solo cio' che due ipotesi di fila dicono uguale (salvo tetto duro)
        now_n = [norm_word(w["word"]) for w in words]
        prev_n = [norm_word(w["word"]) for w in self._prev_words]
        agreed = common_prefix(now_n, prev_n)
        if not forced and agreed < k:
            self._prev_words = words
            return 0
        head = " ".join(w["word"].strip() for w in words[:k]).strip()
        if rms(seg[:cut]) < SILENCE_RMS:             # testo nato da audio muto: allucinazione, via
            head = ""
        with self._lock:
            if head:
                self._committed = join_text(self._committed, head)
            self._rev += 1
        self._commit_idx += cut
        self._drop_committed()
        rest = []
        for w in words[k:]:                          # tempi rebased sulla nuova coda
            rest.append({"word": w["word"], "start": float(w.get("start") or 0.0) - t_cut,
                         "end": float(w.get("end") or 0.0) - t_cut})
        self._prev_words = rest
        return k

    def _final(self):
        """Ultima richiesta: nessuna attesa di ritmo (il tetto 20/min resta, ma qui e' una sola)."""
        self._pacer.last = None
        if self._degraded or self._guard_off:   # guardia scattata (o 429): il testo finale lo fa
            self._result = None                 # il batch, e non si spende nemmeno un tentativo
            return
        dur_all = self._total / SR
        if dur_all < 0.3:
            self._result = None
            return
        if self.final_policy == "batch_under" and dur_all < self.batch_under_sec:
            # Sotto soglia il batch e' gia' veloce quanto noi (misurato): il testo finale viene da
            # li', e non si spende nemmeno la richiesta finale. Il live resta solo anteprima.
            self.log(f"[live] policy: audio {dur_all:.1f}s < {self.batch_under_sec:.0f}s -> "
                     "testo finale dal percorso batch")
            self._result = None
            return
        tail = len(self._uncommitted()) / SR
        if tail > MAX_FINAL_SEC:
            self.log(f"[live] coda finale di {tail:.0f}s: oltre il tetto di Groq, "
                     "testo finale dal percorso batch")
            self._result = None
            return
        self._cycle(final=True)
        snap = self.snapshot()
        text = join_text(snap["committed"], snap["tentative"])
        if self._incomplete or not text.strip():
            self._result = None       # meglio ripagare una richiesta batch che incollare mezza
            return                    # dettatura (o niente, quando il testo e' vuoto)
        self._result = (text.strip(), self._lang_pinned or lang_code(self._lang) or "")
