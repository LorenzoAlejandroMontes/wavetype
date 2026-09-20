"""
Motore live DOPPIO (opzione B): anteprima locale gratuita + testo finale gia' pronto allo stop.

Due motori sulla stessa dettatura, stesso contratto di docs/ENGINE-NOTES.md, cosi' wavetype.py lo
scambia con una riga (import live_dual as live_engine):
- l'anteprima parola per parola resta quella di live_local.py (Nemotron sulla CPU, 0,40 s di
  mediana, zero richieste): snapshot() viene SEMPRE da li', il pannello non cambia di una virgola;
- il testo finale lo cuce live_engine.py, che manda a Groq i pezzi chiusi MENTRE parli: allo stop
  resta solo la coda, non l'intera dettatura. stop() torna (testo grezzo, lingua) o None.

None = "usa il percorso di oggi": wavetype.py fa il batch sull'audio intero, che e' sempre archiviato.
Nessuna dettatura si perde per colpa di questo modulo, e niente qui solleva verso il chiamante.

Economia (decisa il 19/09): sotto i 30 s la dettatura costa ZERO richieste live. L'anteprima la
fa gia' il locale, e il batch sotto i 30 s e' gia' veloce (1,56 s di mediana sotto i 10 s,
docs/ENGINE-NOTES.md): mandare anche i pezzi significherebbe pagare due volte lo stesso
audio. Il lato Groq parte quando la dettatura supera i 30 s e recupera in una volta i pezzi gia'
chiusi (min_live_sec = batch_under_sec = 30 su live_engine.LiveEngine).

Ritmo: PACE_DUAL = 12 s, non i 4 s di live_engine da solo. Li' il ritmo serviva a far comparire
le parole; qui le parole le disegna il locale e la rete serve solo ad avere il grezzo pronto allo
stop. 12 s = ~5 richieste/min per dettatura, sotto la soglia della guardia (14/min) anche quando
si detta senza fermarsi.

Guardia (Guard): i limiti che durano piu' di una dettatura. Il Pacer di live_engine si azzera a
ogni start() e non vede ne' il percorso batch ne' l'app che gira: qui i conti sono di processo e
sopravvivono al riavvio (groq_usage.json, per data locale). Si controlla PRIMA di ogni richiesta;
quando scatta, quella dettatura non manda piu' nulla e stop() torna None (batch).
wavetype.py chiama note_batch_request() per ogni richiesta STT del percorso di oggi, cosi' i conti
sono veri e non solo quelli del live.
"""
import json
import os
import threading
import time
from collections import deque
from datetime import date

import live_engine
import live_local

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- CONFIG ----
SR_IN = 48000                    # rate di cattura di wavetype.py (REC_SR)
BATCH_UNDER_SEC = 30.0           # sotto: nessuna richiesta live, il testo finale viene dal batch
PACE_DUAL = 12.0                 # ritmo del lato Groq (vedi sopra: l'anteprima non dipende da lui)

# Limiti Groq piano gratis, PER MODELLO (console.groq.com/docs/rate-limits, letti il 2026-09-19).
# whisper-large-v3-turbo: 20 richieste/min, 2.000/giorno, 7.200 s di audio/ora, 28.800 s/giorno.
# La formattazione gira su openai/gpt-oss-120b e ha i suoi limiti, separati: non entra in questi
# conti. Il minimo fatturato e' 10 s a richiesta (console.groq.com/docs/speech-to-text): ogni
# richiesta qui viene contata max(10 s, durata vera).
STT_RPM, STT_RPD, STT_ASH, STT_ASD = 20, 2000, 7200, 28800
MIN_BILLED = 10.0

# Soglie della guardia: sotto i tetti veri, perche' il tetto e' condiviso con l'app in esecuzione,
# col percorso batch e con le altre sessioni. Misurato sul riuso reale (233 dettature di
# wavetype_history.txt rigiocate): 1 dettatura su 233 passa al batch.
G_RPM = 14                       # 70% di 20, sulla finestra scorrevole di 60 s
G_RPD = 1600                     # 80% di 2.000, sul giorno locale
G_ASH = 5760                     # 80% di 7.200, sulla finestra scorrevole di 3.600 s
G_ASD = 23040                    # 80% di 28.800, sul giorno locale
G_PER_DICT = 40                  # tetto di richieste live in UNA dettatura (~8 minuti a 12 s)
COOL_429 = 60.0                  # un 429: live spento per questa dettatura + tanti secondi
WIN_429 = 600.0                  # due 429 dentro questa finestra...
OFF_429 = 3600.0                 # ...e il live resta spento per un'ora
USAGE_FILE = "groq_usage.json"   # contatori del giorno (in .gitignore: e' roba di runtime)
KEEP_DAYS = 7                    # giorni tenuti nel file: serve solo l'oggi, il resto e' storia
# ----------------


def _today():
    return date.today().isoformat()


class UsageStore:
    """Contatori per data locale su un JSON piccolo. Non solleva mai: file assente, illeggibile o
    corrotto = giorno vuoto (meglio ripartire da zero che bloccare la dettatura). Scrittura
    atomica (file temporaneo + os.replace): un riavvio a meta' scrittura non lascia un file rotto,
    e comunque il caso peggiore e' un giorno che riparte da zero."""

    def __init__(self, path=None, day=None, log=print):
        self.path = path or os.path.join(HERE, USAGE_FILE)
        self.day = day or _today
        self.log = log
        self.days = self._read()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            out = {}
            for k, v in dict(raw).items():
                out[str(k)] = {"req": int(v.get("req", 0)),
                               "audio": float(v.get("audio", 0.0))}
            return out
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.log(f"[guardia] {USAGE_FILE} illeggibile ({e}): riparto dai contatori a zero")
            return {}

    def today(self):
        """Riga del giorno locale (creata se manca)."""
        return self.days.setdefault(self.day(), {"req": 0, "audio": 0.0})

    def add(self, billed_sec):
        d = self.today()
        d["req"] += 1
        d["audio"] += float(billed_sec)
        self._write()
        return d

    def set_req(self, n):
        """Riconciliazione con Groq: il numero vero di richieste del giorno vince sul nostro."""
        d = self.today()
        if int(n) > d["req"]:
            d["req"] = int(n)
            self._write()

    def _write(self):
        try:
            keep = sorted(self.days)[-KEEP_DAYS:]
            self.days = {k: self.days[k] for k in keep}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.days, f, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            self.log(f"[guardia] {USAGE_FILE} non scritto ({e}): i conti valgono per la sessione")


class Guard:
    """Guardia dei limiti STT. allow() si chiama PRIMA di ogni richiesta; quando dice no, quella
    dettatura non manda piu' nulla e il testo finale lo fa il percorso batch.

    Thread: allow/note_* arrivano dal worker del motore e da wavetype.py (note_batch_request),
    quindi tutto sotto lock. Mai un'eccezione verso fuori."""

    def __init__(self, store=None, log=print, clock=time.monotonic, path=None, day=None):
        self.log = log
        self.clock = clock
        self.store = store if store is not None else UsageStore(path=path, day=day, log=log)
        self._lock = threading.RLock()
        self.sent = deque()          # istanti delle richieste (finestra 60 s)
        self.audio = deque()         # (istante, secondi fatturati) finestra 3.600 s
        self.per_dict = 0            # richieste live della dettatura in corso
        self.off_until = 0.0         # spento fino a qui (429)
        self.hits_429 = deque()
        self.headers = {}            # ultimi x-ratelimit-* visti: servono alle misure
        self.trips = []              # (finestra, valore, soglia) di ogni scatto: test e log

    # ---- API usata da live_engine.LiveEngine ----
    def start(self):
        """Nuova dettatura: si azzera solo il tetto per dettatura, il resto e' di processo."""
        with self._lock:
            self.per_dict = 0

    def allow(self, audio_sec=0.0):
        try:
            with self._lock:
                return self._allow(float(audio_sec))
        except Exception as e:                      # una guardia rotta non ferma una dettatura:
            self.log(f"[guardia] errore ({e}): lascio passare")   # il Pacer tiene comunque 18/min
            return True

    def note_request(self, audio_sec=0.0):
        """Una richiesta STT e' partita (live o batch): entra in tutti i conti."""
        try:
            with self._lock:
                now = self.clock()
                b = max(MIN_BILLED, float(audio_sec))
                self._prune(now)
                self.sent.append(now)
                self.audio.append((now, b))
                self.per_dict += 1
                d = self.store.add(b)
                return d
        except Exception as e:
            self.log(f"[guardia] conteggio fallito: {e}")
            return None

    def note_headers(self, headers):
        """x-ratelimit-* di Groq: se dice quante richieste restano oggi, il nostro contatore si
        allinea al suo (le richieste fatte da altro con la stessa chiave le vediamo solo cosi')."""
        try:
            if not headers:
                return
            h = {k.lower(): v for k, v in dict(headers).items() if k.lower().startswith("x-rate")}
            if not h:
                return
            with self._lock:
                self.headers = h
                left = h.get("x-ratelimit-remaining-requests")
                lim = h.get("x-ratelimit-limit-requests")
                if left is None:
                    return
                total = int(lim) if lim is not None else STT_RPD
                if total >= STT_RPD:                 # l'header del minuto (20) non e' del giorno
                    self.store.set_req(max(0, total - int(left)))
        except Exception as e:
            self.log(f"[guardia] header non letti: {e}")

    def note_429(self, retry_after=None):
        """Groq ha detto basta. Live spento per almeno COOL_429 s (o quanto dice lui); due 429
        dentro WIN_429 = spento per un'ora: insistere su una chiave gia' satura e' come non avere
        il live, ma con l'attesa in piu'."""
        try:
            with self._lock:
                now = self.clock()
                wait = COOL_429
                try:
                    if retry_after is not None:
                        wait = max(wait, float(str(retry_after).rstrip("s")))
                except Exception:
                    pass
                self.hits_429.append(now)
                while self.hits_429 and now - self.hits_429[0] > WIN_429:
                    self.hits_429.popleft()
                if len(self.hits_429) >= 2:
                    wait = max(wait, OFF_429)
                self.off_until = max(self.off_until, now + wait)
                self.log(f"[guardia] 429 di Groq: live spento per {wait:.0f}s "
                         f"({len(self.hits_429)} in {WIN_429:.0f}s) — testo finale dal batch")
        except Exception as e:
            self.log(f"[guardia] 429 non registrato: {e}")

    # ---- interno ----
    def _prune(self, now):
        while self.sent and now - self.sent[0] > 60.0:
            self.sent.popleft()
        while self.audio and now - self.audio[0][0] > 3600.0:
            self.audio.popleft()

    def _allow(self, audio_sec):
        now = self.clock()
        self._prune(now)
        d = self.store.today()
        if now < self.off_until:
            left = round(self.off_until - now, 1)
            self.trips.append(("429", left, 0))
            self.log(f"[guardia] 429: live ancora spento per {left:.0f}s — testo finale dal "
                     "percorso batch")
            return False
        checks = (
            ("richieste/min", len(self.sent), G_RPM, STT_RPM),
            ("richieste/giorno", d["req"], G_RPD, STT_RPD),
            ("audio/ora", round(sum(s for _, s in self.audio)), G_ASH, STT_ASH),
            ("audio/giorno", round(d["audio"]), G_ASD, STT_ASD),
            ("richieste in questa dettatura", self.per_dict, G_PER_DICT, G_PER_DICT),
        )
        for name, val, thr, hard in checks:
            if val >= thr:
                return self._trip(name, val, thr, hard)
        return True

    def _trip(self, name, val, thr, hard):
        self.trips.append((name, val, thr))
        self.log(f"[guardia] {name}: {val} >= {thr} (tetto Groq {hard}) — questa dettatura passa "
                 "al percorso batch, niente altre richieste live")
        return False

    # ---- lettura, per misure e test ----
    def state(self):
        with self._lock:
            d = self.store.today()
            return {"rpm": len(self.sent), "rpd": d["req"],
                    "ash": round(sum(s for _, s in self.audio), 1), "asd": round(d["audio"], 1),
                    "per_dict": self.per_dict, "off_for": max(0.0, self.off_until - self.clock())}


# ---------- guardia di processo (una per app) ----------
_GUARD = None
_GUARD_LOCK = threading.Lock()


def guard(log=None):
    """La guardia condivisa da tutti i motori e dal percorso batch di questo processo."""
    global _GUARD
    with _GUARD_LOCK:
        if _GUARD is None:
            _GUARD = Guard(log=log or print)
        elif log is not None:
            _GUARD.log = log
            _GUARD.store.log = log
        return _GUARD


def note_batch_request(audio_sec=0.0):
    """Da chiamare per OGNI richiesta STT del percorso batch di wavetype.py (anche i pezzi di
    groq_transcribe_long): sono sullo stesso modello e consumano gli stessi tetti. Senza questa,
    la guardia conterebbe solo il live e lascerebbe passare il doppio. Non solleva mai."""
    try:
        guard().note_request(audio_sec)
    except Exception:
        pass


# ---------- il motore doppio ----------
def load_backend(*a, **kw):
    """Precarico del modello locale (wavetype.live_preload lo chiama sul modulo del motore)."""
    return live_local.load_backend(*a, **kw)


class DualEngine:
    """Contratto in docs/ENGINE-NOTES.md. Anteprima dal locale, testo finale da Groq.

    Nessun metodo solleva: qualunque cosa vada storta sul lato Groq vale None, cioe' il percorso
    di oggi. `local` e `groq` si possono iniettare (test)."""

    def __init__(self, groq_key=None, vocab="", log=print, sr_in=SR_IN, clock=time.monotonic,
                 backend=live_local.BACKEND, backend_kw=None, http_post=None, guard_obj=None,
                 pace=PACE_DUAL, batch_under_sec=BATCH_UNDER_SEC, local=None, groq=None,
                 **_ignored):
        self.log = log
        self.guard = guard_obj if guard_obj is not None else guard(log)
        self.local = local if local is not None else live_local.LiveEngine(
            log=log, sr_in=sr_in, backend=backend, backend_kw=backend_kw, clock=clock)
        self.groq = groq
        if self.groq is None and (groq_key or http_post is not None):
            self.groq = live_engine.LiveEngine(
                groq_key, vocab=vocab, log=log, sr_in=sr_in, clock=clock, pace=pace,
                http_post=http_post, guard=self.guard,
                # stessa soglia due volte: sotto i 30 s niente richieste live (min_live_sec) e
                # niente richiesta finale (batch_under) -> la dettatura corta costa zero
                final_policy="batch_under", batch_under_sec=batch_under_sec,
                min_live_sec=batch_under_sec)
        elif self.groq is None:
            self.log("[dual] niente chiave Groq: solo anteprima locale, testo finale dal batch")

    # ---- API ----
    def start(self):
        self.local.start()
        self._groq("start")

    def feed(self, block):
        """Dal callback audio: ai due motori, che accodano e basta. Non deve mai bloccare."""
        self.local.feed(block)
        self._groq("feed", block)

    def snapshot(self):
        """SEMPRE dal locale: l'anteprima e' quella di oggi, parola per parola."""
        return self.local.snapshot()

    def stop(self, timeout=30):
        """(testo grezzo, lingua) da Groq se il live e' completo, altrimenti None (batch)."""
        try:
            self.local.stop(timeout)          # torna None subito e chiude il suo worker
        except Exception as e:
            self.log(f"[dual] stop locale: {e}")
        out = self._groq("stop", timeout)
        if isinstance(out, tuple) and len(out) == 2 and str(out[0]).strip():
            return out
        return None

    def cancel(self):
        try:
            self.local.cancel()
        except Exception as e:
            self.log(f"[dual] cancel locale: {e}")
        self._groq("cancel")

    # ---- comodita' del banco locale (test_live_local.py) ----
    def finish(self, timeout=30):
        return self.local.finish(timeout)

    def committed_words(self):
        return self.local.committed_words()

    # ---- interno ----
    def _groq(self, name, *a):
        """Il lato Groq non deve mai arrivare al chiamante: al massimo non c'e'."""
        if self.groq is None:
            return None
        try:
            return getattr(self.groq, name)(*a)
        except Exception as e:
            self.log(f"[dual] lato Groq ({name}): {e} — testo finale dal percorso batch")
            return None


LiveEngine = DualEngine        # wavetype.py costruisce live_engine.LiveEngine(...)
