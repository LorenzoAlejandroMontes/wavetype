"""
model_fetch.py — scarica da solo il modello dell'anteprima live quando manca.

Perche': le parole accanto al cursore (live_local.py) vengono da Nemotron in streaming, ~650 MB
spacchettati. Ne' l'installer ne' un checkout del sorgente lo contengono, quindi un utente nuovo
non vedeva mai le parole live. Qui l'app se lo prende in sottofondo al primo avvio e, appena e'
pronto, wavetype.py accende le parole senza riavviare. La dettatura non aspetta mai questo modulo.

Fonte ufficiale: release GitHub k2-fsa/sherpa-onnx, tag asr-models (misurato il 23/09: il server
risponde 206 a una Range, Content-Range .../475272949).

Passi (un thread, mai due insieme):
  1. MODELS_DIR/<archivio>.partial, ripreso con HTTP Range se un avvio precedente si e' interrotto;
     se il server ignora la Range (200) si riparte da zero senza sporcare il file.
  2. dimensione esatta in byte, poi bz2 (che ha il suo CRC per blocco) spacchettato in una cartella
     temporanea accanto, e rinominata al suo posto con os.replace: find_model non vede mai un
     modello a meta'.
  3. archivio cancellato.
Errori di rete: si riprova con attesa crescente (BACKOFF), senza fine: il PC puo' restare offline
per ore. Errori che riprovare non aggiusta (HTTP 4xx, disco pieno, archivio rotto tre volte):
stato "error" e basta, l'app resta quella di sempre senza parole.

Due istanze (sorgente + eseguibile puntano a cartelle diverse, ma due avvii dello stesso possono
sovrapporsi un attimo): un lock di Windows su MODELS_DIR/.nemotron.lock (msvcrt.locking), che il
sistema rilascia da solo se il processo muore. Chi non lo ottiene aspetta e controlla.

Da riga di comando (prova vera, scarica davvero):
  python model_fetch.py [cartella]
"""
import glob
import os
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request

NAME = "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-320ms-int8-2026-06-11"
ARCHIVE = NAME + ".tar.bz2"
URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/" + ARCHIVE
SIZE = 475272949                 # byte dell'archivio (Content-Range misurato il 23/09)
UNPACKED = 690_000_000           # ~684 MB spacchettato (copia di riferimento), piu' margine
NEEDED = ("tokens.txt", "encoder*.onnx", "decoder*.onnx", "joiner*.onnx")
CHUNK = 1 << 20                  # 1 MB per lettura: progresso fluido, poche chiamate
TIMEOUT = 30                     # secondi di silenzio del socket prima di considerarlo caduto
BACKOFF = (5, 15, 45, 120, 300)  # attese fra un tentativo e l'altro; poi resta sull'ultima
MAX_BAD = 3                      # archivi scaricati interi ma rotti/di misura sbagliata: poi basta
LOCK_POLL = 5.0                  # chi aspetta un'altra istanza ricontrolla ogni tanto
USER_AGENT = "Wavetype (model fetch)"


class Fatal(Exception):
    """Errore che riprovare non aggiusta."""


class _Counting:
    """File in lettura che conta i byte letti: il progresso dello spacchettamento."""

    def __init__(self, f, on_read):
        self.f = f
        self.on_read = on_read

    def read(self, n=-1):
        b = self.f.read(n)
        self.on_read(len(b))
        return b


class Fetcher:
    """Un download alla volta per cartella. Stato leggibile da qualunque thread con snapshot()."""

    def __init__(self, models_dir, url=URL, size=SIZE, name=NAME, log=print,
                 opener=None, sleep=time.sleep, backoff=BACKOFF, on_ready=None):
        self.dir = models_dir
        self.url = url
        self.size = size
        self.name = name
        self.log = log
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.backoff = backoff
        self.on_ready = on_ready
        self.partial = os.path.join(models_dir, os.path.basename(url) + ".partial")
        self.target = os.path.join(models_dir, name)
        self._lock = threading.Lock()
        self._thread = None
        self._state = "idle"
        self._done = 0
        self._total = size
        self._error = None
        self._stop = False

    # ---- stato ----
    def _set(self, state=None, done=None, error=None):
        with self._lock:
            if state is not None:
                self._state = state
            if done is not None:
                self._done = done
            if error is not None or state not in (None, "error"):
                self._error = error

    def snapshot(self):
        """{"state": idle|downloading|extracting|ready|error, "done": byte, "total": byte,
        "error": testo o None}. In "extracting" done/total contano l'archivio letto."""
        with self._lock:
            return {"state": self._state, "done": self._done, "total": self._total,
                    "error": self._error}

    def present(self):
        return model_ok(self.target)

    # ---- API ----
    def start(self):
        """Parte in sottofondo se il modello manca e nessun thread e' gia' al lavoro.
        True = un thread e' al lavoro (nuovo o di prima)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            if self._state == "ready":
                return False
            self._thread = threading.Thread(target=self.run, name="model-fetch", daemon=True)
            self._thread.start()
            return True

    def join(self, timeout=None):
        t = self._thread
        if t is not None:
            t.join(timeout)

    def stop(self):
        """Per i test: il thread esce al prossimo pezzo (il .partial resta, si riprende)."""
        self._stop = True

    def run(self):
        """Corpo del thread (sincrono: i test lo chiamano direttamente)."""
        try:
            os.makedirs(self.dir, exist_ok=True)
            if self.present():
                self._ready("gia' presente")
                return
            lock = _FileLock(os.path.join(self.dir, ".nemotron.lock"))
            while not lock.acquire():
                if self.present():                       # l'altra istanza ha finito
                    self._ready("scaricato da un'altra istanza")
                    return
                if self._stop:
                    return
                self._set("downloading", done=_size(self.partial))
                self.sleep(LOCK_POLL)
            try:
                if self.present():
                    self._ready("gia' presente")
                    return
                self._fetch_and_unpack()
            finally:
                lock.release()
        except Fatal as e:
            self._set("error", error=str(e))
            self.log(f"[modello] download fermo: {e} — l'app funziona, senza parole live")
        except Exception as e:                           # mai un'eccezione fuori dal thread
            self._set("error", error=f"{type(e).__name__}: {e}")
            self.log(f"[modello] errore inatteso: {type(e).__name__}: {e}")

    def _ready(self, why):
        self._set("ready", done=self.size)
        self.log(f"[modello] live pronto ({why}): {self.target}")
        if self.on_ready:
            try:
                self.on_ready()
            except Exception as e:
                self.log(f"[modello] on_ready: {e}")

    # ---- interno ----
    def _fetch_and_unpack(self):
        bad = 0
        attempt = 0
        t0 = time.perf_counter()
        start_have = _size(self.partial)
        self.log(f"[modello] manca il modello live: lo scarico ({self.size / 1e6:.0f} MB, "
                 f"ripresa da {start_have / 1e6:.0f} MB) in {self.dir}")
        while not self._stop:
            try:
                self._check_space()
                self._download()
                if self._stop:
                    return
                got = _size(self.partial)
                if got != self.size:
                    raise _Bad(f"dimensione {got} invece di {self.size}")
                dt = time.perf_counter() - t0
                self.log(f"[modello] scaricato in {dt:.0f}s "
                         f"({(got - start_have) / 1e6 / max(dt, 0.001):.1f} MB/s), spacchetto")
                t1 = time.perf_counter()
                self._unpack()
                self.log(f"[modello] spacchettato in {time.perf_counter() - t1:.0f}s")
                _remove(self.partial)
                self._ready("scaricato")
                return
            except _Bad as e:                            # archivio intero ma sbagliato: da capo
                bad += 1
                _remove(self.partial)
                self.log(f"[modello] archivio da buttare ({e}), {bad}/{MAX_BAD}")
                if bad >= MAX_BAD:
                    raise Fatal(f"archivio sbagliato {bad} volte: {e}")
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code not in (408, 416, 429):
                    raise Fatal(f"HTTP {e.code} da {self.url}")
                if e.code == 416:                        # Range oltre la fine: .partial sporco
                    _remove(self.partial)
                self._retry(attempt, f"HTTP {e.code}")
                attempt += 1
            except (urllib.error.URLError, OSError, EOFError) as e:
                if isinstance(e, OSError) and getattr(e, "errno", None) == 28:     # ENOSPC
                    raise Fatal("disco pieno")
                self._retry(attempt, f"{type(e).__name__}: {e}")
                attempt += 1

    def _retry(self, attempt, why):
        wait = self.backoff[min(attempt, len(self.backoff) - 1)]
        self.log(f"[modello] rete: {why} — riprovo fra {wait}s (ripresa da "
                 f"{_size(self.partial) / 1e6:.0f} MB)")
        self._set("downloading", done=_size(self.partial))
        self.sleep(wait)

    def _check_space(self):
        need = (self.size - _size(self.partial)) + UNPACKED
        free = shutil.disk_usage(self.dir).free
        if free < need:
            raise Fatal(f"spazio libero {free / 1e6:.0f} MB, ne servono {need / 1e6:.0f}")

    def _download(self):
        have = _size(self.partial)
        if have > self.size:                             # piu' grande del vero: non e' lui
            _remove(self.partial)
            have = 0
        self._set("downloading", done=have)
        if have == self.size:
            return
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        resp = self.opener(urllib.request.Request(self.url, headers=headers), timeout=TIMEOUT)
        try:
            status = getattr(resp, "status", None) or resp.getcode()
            mode = "ab"
            if have and status != 206:                   # Range ignorata: il corpo parte da 0
                self.log(f"[modello] il server non riprende (HTTP {status}): da capo")
                have, mode = 0, "wb"
            elif have:
                cr = resp.headers.get("Content-Range", "") if resp.headers else ""
                if cr and not cr.startswith(f"bytes {have}-"):
                    raise _Bad(f"Content-Range inatteso: {cr}")
            elif status != 200:
                raise urllib.error.URLError(f"HTTP {status}")
            self._set(done=have)
            with open(self.partial, mode) as f:
                while not self._stop:
                    b = resp.read(CHUNK)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
                    if have > self.size:
                        raise _Bad(f"il server manda piu' di {self.size} byte")
                    self._set(done=have)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if not self._stop and have < self.size:          # connessione chiusa prima della fine
            raise urllib.error.URLError(f"connessione chiusa a {have}/{self.size} byte")

    def _unpack(self):
        self._set("extracting", done=0)
        tmp = os.path.join(self.dir, f".{self.name}.unpack-{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        read = [0]

        def on_read(n):
            read[0] += n
            self._set(done=min(read[0], self.size))

        try:
            with open(self.partial, "rb") as raw:
                with tarfile.open(fileobj=_Counting(raw, on_read), mode="r|bz2") as tar:
                    tar.extractall(tmp, filter="data")
            src = os.path.join(tmp, self.name)
            if not os.path.isdir(src):                   # archivio senza la cartella in cima
                src = tmp
            if not model_ok(src):
                raise _Bad("nell'archivio mancano i file del modello")
            if os.path.isdir(self.target):               # resto di un tentativo a meta'
                shutil.rmtree(self.target, ignore_errors=True)
            os.replace(src, self.target)
        except (tarfile.TarError, EOFError, ValueError) as e:
            raise _Bad(f"archivio illeggibile: {e}")
        except OSError as e:
            if getattr(e, "errno", None) == 28:
                raise Fatal("disco pieno durante lo spacchettamento")
            raise _Bad(f"spacchettamento fallito: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _Bad(Exception):
    """Archivio scaricato ma da buttare (misura, contenuto)."""


class _FileLock:
    """Lock fra processi su un file (Windows: msvcrt.locking, rilasciato dal sistema se il
    processo muore; altrove fcntl). acquire() non blocca: False = ce l'ha un altro."""

    def __init__(self, path):
        self.path = path
        self.f = None

    def acquire(self):
        f = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.f = f
        return True

    def release(self):
        f, self.f = self.f, None
        if f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


def model_ok(d):
    """La cartella ha tutti i file che NemotronBackend apre (live_local.py:146-155)?"""
    return os.path.isdir(d) and all(glob.glob(os.path.join(d, n)) for n in NEEDED)


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---- un Fetcher per cartella, per tutto il processo ----
_fetchers = {}
_fetchers_lock = threading.Lock()


def fetcher(models_dir, **kw):
    """Lo stesso Fetcher per la stessa cartella: due chiamate non fanno due download."""
    key = os.path.normcase(os.path.abspath(models_dir))
    with _fetchers_lock:
        if key not in _fetchers:
            _fetchers[key] = Fetcher(models_dir, **kw)
        return _fetchers[key]


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                           "models")
    fx = Fetcher(d)
    fx.start()
    while fx._thread.is_alive():
        s = fx.snapshot()
        print(f"\r{s['state']:12} {s['done'] / 1e6:6.0f} / {s['total'] / 1e6:.0f} MB", end="", flush=True)
        time.sleep(1)
    print("\n", fx.snapshot())
