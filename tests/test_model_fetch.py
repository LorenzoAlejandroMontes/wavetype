"""
tests/test_model_fetch.py — download del modello live (model_fetch.py), senza rete vera.

Un server finto (opener al posto di urllib.request.urlopen) serve un piccolo tar.bz2 costruito qui,
con la stessa forma dell'archivio vero (una cartella in cima con tokens/encoder/decoder/joiner).
  - download intero -> spacchettato al suo posto, archivio cancellato, on_ready chiamato;
  - connessione caduta a meta' -> si riprende con Range dal byte giusto (anche fra due avvii);
  - server che ignora la Range -> si riparte da zero, file giusto;
  - misura sbagliata / archivio rotto -> buttato, dopo MAX_BAD tentativi stato "error";
  - HTTP 404 -> errore subito, niente tentativi all'infinito;
  - due start() -> un thread solo; un'altra istanza col lock -> si aspetta, nessun download doppio;
  - modello gia' presente -> nessuna richiesta;
  - wavetype: col download in corso le parole restano spente, a fine download si accendono.
"""
import io
import os
import sys
import tarfile
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import model_fetch as mf  # noqa: E402

RESULTS = []
NAME = "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-320ms-int8-2099-01-01"
URL = "https://example.invalid/asr-models/" + NAME + ".tar.bz2"
FILES = {"tokens.txt": b"a 0\nb 1\n", "encoder.int8.onnx": os.urandom(300_000),
         "decoder.int8.onnx": os.urandom(40_000), "joiner.int8.onnx": os.urandom(20_000),
         "test_wavs/it.wav": b"RIFF" + b"\0" * 100}


def case(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, None))
        print(f"ok   {fn.__name__}")
    except Exception as e:
        RESULTS.append((fn.__name__, e))
        print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    return fn


def make_archive(files=FILES, top=NAME):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for rel, data in files.items():
            ti = tarfile.TarInfo(f"{top}/{rel}")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


BLOB = make_archive()


class Resp:
    """Risposta finta: status, headers, read(n). `cut` = dopo tanti byte la connessione cade."""

    def __init__(self, body, status=200, headers=None, cut=None, gate=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.pos = 0
        self.cut = cut
        self.gate = gate

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if self.gate is not None:
            self.gate.wait(5)
        if self.cut is not None and self.pos >= self.cut:
            raise ConnectionResetError("caduta finta")
        end = len(self.body) if n < 0 else self.pos + n
        if self.cut is not None:
            end = min(end, self.cut)
        b = self.body[self.pos:end]
        self.pos += len(b)
        return b

    def close(self):
        pass


class Server:
    """Opener finto: registra le Range ricevute; `plan` = lista di funzioni (range_start) -> Resp."""

    def __init__(self, blob=BLOB, plan=None, honor_range=True):
        self.blob = blob
        self.plan = list(plan or [])
        self.honor_range = honor_range
        self.ranges = []
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        rng = req.get_header("Range")
        self.ranges.append(rng)
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        if self.plan:
            return self.plan.pop(0)(start)
        if rng and self.honor_range:
            return Resp(self.blob[start:], 206,
                        {"Content-Range": f"bytes {start}-{len(self.blob) - 1}/{len(self.blob)}"})
        return Resp(self.blob, 200)


def fx_for(d, server, size=len(BLOB), **kw):
    sleeps = []
    f = mf.Fetcher(d, url=URL, size=size, name=NAME, log=lambda m: None, opener=server,
                   sleep=sleeps.append, **kw)
    f.sleeps = sleeps
    return f


def assert_model_in_place(d):
    target = os.path.join(d, NAME)
    for rel, data in FILES.items():
        with open(os.path.join(target, rel), "rb") as fh:
            assert fh.read() == data, rel
    assert not os.path.exists(os.path.join(d, NAME + ".tar.bz2.partial")), "archivio rimasto"
    left = [n for n in os.listdir(d) if n.startswith(".") and "unpack" in n]
    assert not left, f"cartelle temporanee rimaste: {left}"


@case
def test_download_intero_e_spacchettato():
    with tempfile.TemporaryDirectory() as d:
        ready = []
        srv = Server()
        f = fx_for(d, srv, on_ready=lambda: ready.append(1))
        f.run()
        assert f.snapshot()["state"] == "ready", f.snapshot()
        assert_model_in_place(d)
        assert ready == [1]
        assert srv.ranges == [None]
        assert mf.model_ok(os.path.join(d, NAME))


@case
def test_caduta_a_meta_riprende_con_range():
    with tempfile.TemporaryDirectory() as d:
        cut = len(BLOB) // 3
        srv = Server(plan=[lambda s: Resp(BLOB, 200, cut=cut)])
        f = fx_for(d, srv)
        f.run()
        assert f.snapshot()["state"] == "ready", f.snapshot()
        assert srv.ranges == [None, f"bytes={cut}-"], srv.ranges
        assert f.sleeps == [mf.BACKOFF[0]], f.sleeps
        assert_model_in_place(d)


@case
def test_ripresa_fra_due_avvii():
    """Un avvio precedente ha lasciato il .partial: il nuovo chiede solo il resto."""
    with tempfile.TemporaryDirectory() as d:
        have = len(BLOB) // 2
        with open(os.path.join(d, NAME + ".tar.bz2.partial"), "wb") as fh:
            fh.write(BLOB[:have])
        srv = Server()
        f = fx_for(d, srv)
        f.run()
        assert srv.ranges == [f"bytes={have}-"], srv.ranges
        assert f.snapshot()["state"] == "ready"
        assert_model_in_place(d)


@case
def test_server_che_ignora_la_range_riparte_da_zero():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, NAME + ".tar.bz2.partial"), "wb") as fh:
            fh.write(BLOB[:1000])
        srv = Server(honor_range=False)
        f = fx_for(d, srv)
        f.run()
        assert srv.ranges == ["bytes=1000-"]
        assert f.snapshot()["state"] == "ready", f.snapshot()
        assert_model_in_place(d)


@case
def test_partial_piu_grande_del_vero_si_butta():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, NAME + ".tar.bz2.partial"), "wb") as fh:
            fh.write(b"x" * (len(BLOB) + 10))
        srv = Server()
        f = fx_for(d, srv)
        f.run()
        assert srv.ranges == [None], srv.ranges
        assert_model_in_place(d)


@case
def test_misura_sbagliata_dopo_tre_volte_errore():
    """Il server manda piu' byte di quelli attesi: archivio buttato, poi basta (niente loop)."""
    with tempfile.TemporaryDirectory() as d:
        srv = Server()
        f = fx_for(d, srv, size=len(BLOB) - 7)
        f.run()
        s = f.snapshot()
        assert s["state"] == "error", s
        assert srv.calls == mf.MAX_BAD, srv.calls
        assert not os.path.exists(os.path.join(d, NAME))
        assert not os.path.exists(os.path.join(d, NAME + ".tar.bz2.partial"))


@case
def test_archivio_rotto_non_finisce_al_suo_posto():
    with tempfile.TemporaryDirectory() as d:
        junk = os.urandom(len(BLOB))
        srv = Server(blob=junk)
        f = fx_for(d, srv)
        f.run()
        assert f.snapshot()["state"] == "error", f.snapshot()
        assert "illeggibile" in f.snapshot()["error"], f.snapshot()
        assert not os.path.exists(os.path.join(d, NAME))
        assert not [n for n in os.listdir(d) if "unpack" in n]


@case
def test_archivio_senza_file_del_modello():
    with tempfile.TemporaryDirectory() as d:
        blob = make_archive({"README.md": b"niente modello"})
        f = fx_for(d, Server(blob=blob), size=len(blob))
        f.run()
        assert f.snapshot()["state"] == "error"
        assert not os.path.exists(os.path.join(d, NAME))


@case
def test_404_errore_subito():
    import urllib.error

    def nf(_s):
        raise urllib.error.HTTPError(URL, 404, "Not Found", {}, None)
    with tempfile.TemporaryDirectory() as d:
        srv = Server(plan=[nf])
        f = fx_for(d, srv)
        f.run()
        assert f.snapshot()["state"] == "error"
        assert "404" in f.snapshot()["error"]
        assert srv.calls == 1 and f.sleeps == []


@case
def test_rete_giu_riprova_con_attesa_crescente():
    import urllib.error

    def down(_s):
        raise urllib.error.URLError("getaddrinfo failed")
    with tempfile.TemporaryDirectory() as d:
        srv = Server(plan=[down] * 7)
        f = fx_for(d, srv)
        f.run()
        assert f.snapshot()["state"] == "ready"
        assert f.sleeps == [5, 15, 45, 120, 300, 300, 300], f.sleeps


@case
def test_modello_gia_presente_nessuna_richiesta():
    with tempfile.TemporaryDirectory() as d:
        t = os.path.join(d, NAME)
        os.makedirs(t)
        for n in ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"):
            open(os.path.join(t, n), "wb").close()
        ready = []
        srv = Server()
        f = fx_for(d, srv, on_ready=lambda: ready.append(1))
        f.run()
        assert srv.calls == 0 and ready == [1] and f.snapshot()["state"] == "ready"


@case
def test_cartella_a_meta_viene_rifatta():
    """Un modello incompleto (manca il joiner) non conta: si scarica e lo si sostituisce."""
    with tempfile.TemporaryDirectory() as d:
        t = os.path.join(d, NAME)
        os.makedirs(t)
        open(os.path.join(t, "tokens.txt"), "wb").close()
        f = fx_for(d, Server())
        f.run()
        assert f.snapshot()["state"] == "ready"
        assert_model_in_place(d)


@case
def test_due_start_un_thread_solo():
    with tempfile.TemporaryDirectory() as d:
        gate = threading.Event()
        srv = Server(plan=[lambda s: Resp(BLOB, 200, gate=gate)])
        f = fx_for(d, srv)
        assert f.start() is True
        assert f.start() is True                         # il thread di prima, non un secondo
        t1 = f._thread
        assert mf.fetcher(d, url=URL) is mf.fetcher(d)   # stessa cartella -> stesso Fetcher
        gate.set()
        f.join(10)
        assert f._thread is t1 and srv.calls == 1, srv.calls
        assert f.snapshot()["state"] == "ready"
        assert f.start() is False                        # pronto: niente da fare
        assert_model_in_place(d)


@case
def test_altra_istanza_col_lock_si_aspetta():
    """Un'altra istanza ha il lock e sta scaricando: noi non scarichiamo; quando il modello compare
    siamo pronti anche noi."""
    with tempfile.TemporaryDirectory() as d:
        other = mf._FileLock(os.path.join(d, ".nemotron.lock"))
        assert other.acquire()
        srv = Server()
        f = fx_for(d, srv)

        def other_finishes(_secs):                       # mentre aspettiamo, l'altra finisce
            t = os.path.join(d, NAME)
            os.makedirs(t, exist_ok=True)
            for n in ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"):
                open(os.path.join(t, n), "wb").close()
        f.sleep = other_finishes
        try:
            f.run()
        finally:
            other.release()
        assert srv.calls == 0, srv.calls
        assert f.snapshot()["state"] == "ready"


@case
def test_lock_libero_dopo_la_fine():
    with tempfile.TemporaryDirectory() as d:
        f = fx_for(d, Server())
        f.run()
        lk = mf._FileLock(os.path.join(d, ".nemotron.lock"))
        assert lk.acquire(), "il lock e' rimasto preso"
        lk.release()


@case
def test_progresso_leggibile_durante_il_download():
    with tempfile.TemporaryDirectory() as d:
        seen = []

        class Slow(Resp):
            def read(self, n=-1):
                seen.append(f.snapshot())
                return super().read(4096)
        srv = Server(plan=[lambda s: Slow(BLOB, 200)])
        f = fx_for(d, srv)
        f.run()
        dl = [s for s in seen if s["state"] == "downloading"]
        assert dl and dl[-1]["done"] > 0 and dl[-1]["total"] == len(BLOB), dl[-1:]
        assert all(a["done"] <= b["done"] for a, b in zip(dl, dl[1:])), "progresso all'indietro"


@case
def test_finestra_primo_avvio_riga_live():
    """first_run: la riga sotto "KEY SAVED" dice i MB, poi UNPACKING, poi READY; in errore o
    senza download non c'e' (la dettatura va comunque, niente da dire)."""
    import first_run as fr
    view = lambda snap: fr.KeyWindow(os.devnull, dry=True, live_status=(lambda: snap)).live_view()
    total = 475272949
    assert view({"state": "downloading", "done": 120_000_000, "total": total}) == \
        ("downloading", 120_000_000 / total, "120 / 475 MB")
    assert view({"state": "extracting", "done": total // 2, "total": total})[2] == fr.LIVE_UNPACK
    assert view({"state": "ready", "done": total, "total": total}) == ("ready", 1.0, fr.LIVE_READY)
    assert view({"state": "error", "done": 5, "total": total, "error": "HTTP 404"}) is None
    assert fr.KeyWindow(os.devnull, dry=True).live_view() is None


@case
def test_wavetype_parole_spente_finche_il_modello_non_arriva():
    """wavetype.live_fetch_start: modello assente -> engine_ready() False (card senza parole),
    download partito; a download finito live_preload accende le parole senza riavvio."""
    import wavetype as wt
    import live_local
    with tempfile.TemporaryDirectory() as d:
        old = (live_local.MODELS_DIR, wt.live["engine_err"], wt.live["fetch"],
               live_local.load_backend)
        started, loaded = [], []

        class FakeFetcher:
            def __init__(self, models_dir, log=None, on_ready=None):
                self.on_ready = on_ready

            def start(self):
                started.append(1)
                assert not wt.engine_ready(), "parole accese prima del modello"

            def snapshot(self):
                return {"state": "downloading", "done": 1, "total": 2, "error": None}
        try:
            live_local.MODELS_DIR = d
            wt.live["engine_err"] = None
            wt.live["fetch"] = None
            live_local.load_backend = lambda *a, **k: loaded.append(1)
            real = mf.fetcher
            mf.fetcher = lambda models_dir, **kw: FakeFetcher(models_dir, **kw)
            try:
                fx = wt.live_fetch_start()
            finally:
                mf.fetcher = real
            if fx is None:                               # sherpa-onnx assente in questo ambiente
                print("     (sherpa_onnx non installato: live_fetch_start non scarica, giusto)")
                return
            assert started == [1]
            assert wt.live["fetch"] is fx
            assert not wt.engine_ready()
            fx.on_ready()                                # il download e' finito
            assert loaded == [1]
            assert wt.engine_ready(), "parole ancora spente dopo il modello"
        finally:
            (live_local.MODELS_DIR, wt.live["engine_err"], wt.live["fetch"],
             live_local.load_backend) = old


if __name__ == "__main__":
    ok = sum(1 for _, e in RESULTS if e is None)
    print(f"{ok}/{len(RESULTS)} ok")
    sys.exit(0 if ok == len(RESULTS) else 1)
