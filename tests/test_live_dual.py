"""
Banco di prova di live_dual.py (motore doppio: anteprima locale + testo finale da Groq).

Due parti:
  1) test unitari SENZA rete e SENZA modello locale: economia sotto i 30 s, recupero dei pezzi
     sopra i 30 s, ogni finestra della guardia che scatta, 429 e cooldown, file dei contatori
     (riavvio, cambio giorno, file corrotto), lato Groq che esplode senza arrivare al chiamante,
     stop() che rispetta il suo tempo.
  2) --real: una registrazione lunga vera, motore locale vero e Groq vero, end to end.
     Costa richieste Groq (una decina): il riferimento batch viene dalla cache.

Uso:
  .venv/Scripts/python.exe tests/test_live_dual.py                 # solo unitari (niente rete)
  .venv/Scripts/python.exe tests/test_live_dual.py --real          # + registrazione vera
  ... --file 20260914-143848.wav

Il mock di Groq e' quello di test_live_engine.py: decodifica davvero l'audio che riceve, quindi
taglio, cucitura e recupero vengono esercitati sul serio.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import live_dual as D                                       # noqa: E402
import live_engine as L                                     # noqa: E402
import test_live_engine as E                                # noqa: E402  (synth, MockGroq, ...)

REC_DIR = os.path.join(ROOT, "recordings")
QUIET = lambda *_a, **_k: None                              # noqa: E731


# ---------- pezzi finti ----------
class StubLocal:
    """Motore locale finto: registra i feed e risponde con un'anteprima fissa. Nessun modello,
    nessun thread: qui si prova il doppio, non il locale (che ha il suo banco)."""

    def __init__(self, snap=None):
        self.fed = 0
        self.started = self.stopped = self.cancelled = 0
        self.snap = snap or {"committed": "anteprima locale", "tentative": "in corso", "rev": 7}

    def start(self):
        self.started += 1

    def feed(self, block):
        self.fed += len(block)

    def snapshot(self):
        return dict(self.snap)

    def stop(self, timeout=30):
        self.stopped += 1
        return None

    def cancel(self):
        self.cancelled += 1


class BoomGroq:
    """Lato Groq che esplode dappertutto: il chiamante non deve accorgersene."""

    def start(self):
        raise RuntimeError("boom start")

    def feed(self, block):
        raise RuntimeError("boom feed")

    def snapshot(self):
        raise RuntimeError("boom snapshot")

    def stop(self, timeout=30):
        raise RuntimeError("boom stop")

    def cancel(self):
        raise RuntimeError("boom cancel")


_TMP = []


def tmp_usage():
    """File dei contatori in una cartella temporanea: i test non toccano groq_usage.json vero."""
    d = tempfile.mkdtemp(prefix="wavetype-usage-")
    _TMP.append(d)
    return os.path.join(d, "groq_usage.json")


def mkguard(clock=None, day=None, path=None, log=QUIET):
    return D.Guard(log=log, clock=clock or E.FakeClock(), path=path or tmp_usage(), day=day)


def dual(http_post=None, pace=0.5, guard_obj=None, local=None, **kw):
    """DualEngine con locale finto e guardia isolata: resta vero solo il lato Groq."""
    return D.DualEngine("chiave", log=QUIET, sr_in=L.SR, http_post=http_post, pace=pace,
                        guard_obj=guard_obj or mkguard(), local=local or StubLocal(), **kw)


def speech(n, word=1.2):
    """n parole da `word` secondi. Massimo 40 parole distinte: oltre, il decoder del mock
    andrebbe sopra la frequenza di Nyquist e non le riconoscerebbe piu'."""
    return [("w", word)] * n


# ============================ economia: sotto e sopra i 30 s ============================
def test_sotto_30s_zero_richieste_live():
    """Il caso di quasi tutte le dettature (mediana 8,3 s): l'anteprima la fa il locale, il testo
    finale il batch, e il live non spende NIENTE. Prima si sarebbe pagato lo stesso audio due
    volte."""
    a, _ = E.synth(speech(16))                       # 19,2 s
    m = E.MockGroq()
    eng = dual(http_post=m)
    eng.start()
    E.feed_stream(eng, a, L.SR, speed=20.0)
    E.pump(eng, 1.0)
    assert m.calls == [], f"mandate {len(m.calls)} richieste sotto i 30 s"
    assert eng.stop(timeout=10) is None, "sotto i 30 s il testo finale deve venire dal batch"
    assert m.calls == [], "nemmeno la richiesta finale si deve spendere"


def test_oltre_30s_recupera_i_pezzi_e_cuce():
    a, words = E.synth(speech(20) + [("sil", 1.2)] + speech(12) + [("sil", 1.2)] + speech(6))
    m = E.MockGroq()
    eng = dual(http_post=m)
    eng.start()
    E.feed_stream(eng, a, L.SR, speed=20.0)
    res = eng.stop(timeout=20)
    assert res is not None, "sopra i 30 s il testo finale deve arrivare dal live"
    got, want = res[0].split(), [w["word"] for w in words]
    assert got == want, f"cucitura sbagliata: {len(got)} parole contro {len(want)}"
    assert m.calls, "nessuna richiesta"
    assert m.calls[0]["dur"] >= 29.0, \
        f"il recupero non ha preso i pezzi gia' chiusi: prima richiesta di {m.calls[0]['dur']:.1f}s"


def test_anteprima_sempre_dal_locale():
    loc = StubLocal()
    eng = dual(http_post=E.MockGroq(), local=loc)
    a, _ = E.synth(speech(30))
    eng.start()
    E.feed_stream(eng, a, L.SR, speed=20.0)
    snap = eng.snapshot()
    assert snap == loc.snap, snap
    assert loc.fed > 0 and loc.started == 1
    eng.stop(timeout=15)


def test_feed_non_blocca():
    a, _ = E.synth(speech(30))
    eng = dual(http_post=E.MockGroq(delay=0.4), pace=0.2)
    eng.start()
    worst, n = 0.0, int(0.05 * L.SR)
    for i in range(0, len(a), n):
        t0 = time.perf_counter()
        eng.feed(a[i:i + n])
        worst = max(worst, time.perf_counter() - t0)
        time.sleep(0.004)
    eng.cancel()
    assert worst < 0.005, f"feed() ha bloccato {worst * 1000:.2f} ms (callback audio: inaccettabile)"


def test_argomenti_di_wavetype_accettati():
    """wavetype.py costruisce il motore con questi argomenti: devono passare anche qui."""
    e = D.DualEngine("chiave", vocab="Wavetype", log=QUIET, sr_in=48000, backend="__nessuno__",
                     final_policy="batch_under", batch_under_sec=30.0, min_live_sec=10.0,
                     guard_obj=mkguard())
    e.local._ready.wait(2)
    e.cancel()
    assert hasattr(D, "load_backend") and D.LiveEngine is D.DualEngine


# ============================ la guardia, finestra per finestra ============================
def test_guardia_richieste_al_minuto():
    c = E.FakeClock()
    g = mkguard(clock=c)
    for _ in range(13):
        g.note_request(1.0)
        c.advance(1.0)
    assert g.allow(10.0) is True, g.state()
    g.note_request(1.0)
    assert g.allow(10.0) is False and g.trips[-1][0] == "richieste/min", g.trips
    c.advance(61.0)                                   # la finestra scorre: si riparte
    assert g.allow(10.0) is True, g.state()


def test_guardia_richieste_al_giorno_e_cambio_data():
    p, giorno = tmp_usage(), ["2026-09-19"]
    json.dump({"2026-09-19": {"req": D.G_RPD, "audio": 10.0}}, open(p, "w", encoding="utf-8"))
    g = mkguard(path=p, day=lambda: giorno[0])
    assert g.allow(10.0) is False and g.trips[-1][0] == "richieste/giorno", g.trips
    giorno[0] = "2026-09-20"                          # mezzanotte: i contatori del giorno sono suoi
    assert g.allow(10.0) is True, g.state()


def test_guardia_audio_ora():
    c = E.FakeClock()
    g = mkguard(clock=c)
    for _ in range(9):                                # 9 x 600 s = 5.400 s: sotto
        g.note_request(600.0)
        c.advance(60.0)
    assert g.allow(10.0) is True, g.state()
    g.note_request(600.0)                             # 6.000 s >= 5.760
    assert g.allow(10.0) is False and g.trips[-1][0] == "audio/ora", g.trips
    c.advance(3601.0)                                 # passata l'ora, la finestra e' vuota
    assert g.allow(10.0) is True, g.state()


def test_guardia_audio_al_giorno():
    p = tmp_usage()
    json.dump({"2026-09-19": {"req": 5, "audio": float(D.G_ASD)}}, open(p, "w", encoding="utf-8"))
    g = mkguard(path=p, day=lambda: "2026-09-19")
    assert g.allow(10.0) is False and g.trips[-1][0] == "audio/giorno", g.trips


def test_guardia_tetto_per_dettatura():
    c = E.FakeClock()
    g = mkguard(clock=c)
    for _ in range(D.G_PER_DICT):
        g.note_request(5.0)
        c.advance(10.0)                               # sparse: le altre finestre restano larghe
    assert g.allow(10.0) is False, g.state()
    assert g.trips[-1][0] == "richieste in questa dettatura", g.trips
    g.start()                                         # dettatura nuova: il tetto si azzera
    assert g.allow(10.0) is True, g.state()


def test_guardia_429_cooldown_e_due_in_dieci_minuti():
    c = E.FakeClock()
    g = mkguard(clock=c)
    g.note_429(None)
    assert g.allow(10.0) is False and g.trips[-1][0] == "429", g.trips
    c.advance(D.COOL_429 + 1)
    assert g.allow(10.0) is True, g.state()
    g.note_429("7")                                   # secondo 429 dentro i 10 minuti: un'ora giu'
    c.advance(D.COOL_429 + 1)
    assert g.allow(10.0) is False, g.state()
    c.advance(D.OFF_429)
    assert g.allow(10.0) is True, g.state()


def test_guardia_429_rispetta_retry_after():
    c = E.FakeClock()
    g = mkguard(clock=c)
    g.note_429("180")                                 # Groq dice 180 s: valgono piu' dei nostri 60
    c.advance(D.COOL_429 + 1)
    assert g.allow(10.0) is False, g.state()
    c.advance(180.0)
    assert g.allow(10.0) is True, g.state()


def test_guardia_riconcilia_con_gli_header():
    """Le richieste fatte da altro con la stessa chiave si vedono solo dall'header."""
    g = mkguard()
    g.note_request(10.0)
    g.note_headers({"x-ratelimit-remaining-requests": "1200",
                    "X-RateLimit-Limit-Requests": "2000"})
    assert g.state()["rpd"] == 800, g.state()
    g.note_headers({"x-ratelimit-remaining-requests": "19", "x-ratelimit-limit-requests": "20"})
    assert g.state()["rpd"] == 800, "l'header del minuto non deve toccare il conto del giorno"


# ============================ la guardia dentro la dettatura ============================
def _dettatura_lunga(m, g, secondi_parola=1.2, parole=32):
    a, _ = E.synth(speech(parole, secondi_parola))
    eng = dual(http_post=m, guard_obj=g)
    eng.start()
    E.feed_stream(eng, a, L.SR, speed=20.0)
    return eng, eng.stop(timeout=20)


def test_guardia_scattata_manda_al_batch():
    """Guardia gia' oltre soglia: nemmeno una richiesta live, e il testo finale viene dal batch."""
    p = tmp_usage()
    json.dump({"2026-09-19": {"req": D.G_RPD, "audio": 0.0}}, open(p, "w", encoding="utf-8"))
    g = mkguard(path=p, day=lambda: "2026-09-19", clock=time.monotonic)
    m = E.MockGroq()
    _, res = _dettatura_lunga(m, g)
    assert m.calls == [], f"{len(m.calls)} richieste con la guardia gia' scattata"
    assert res is None, "con la guardia scattata stop() deve tornare None (batch)"


def test_guardia_che_scatta_a_meta_ferma_le_richieste():
    """Scatta DOPO la prima richiesta (l'ora si riempie li'): niente altre richieste live,
    e il testo finale passa al batch invece di restare monco."""
    g = mkguard(clock=time.monotonic)
    g.note_request(D.G_ASH - 30.0)                     # a 30 s dal tetto dell'ora
    m = E.MockGroq()
    _, res = _dettatura_lunga(m, g)
    assert len(m.calls) == 1, f"{len(m.calls)} richieste: la guardia non ha fermato il live"
    assert res is None, "dopo lo scatto il testo finale deve venire dal batch"
    assert any(t[0] == "audio/ora" for t in g.trips), g.trips


def test_429_di_groq_spegne_il_live_della_dettatura():
    g = mkguard(clock=time.monotonic)
    m = E.MockGroq(fail_times=99, status=429)
    _, res = _dettatura_lunga(m, g)
    assert len(m.calls) == 1, f"{len(m.calls)} richieste dopo un 429: si doveva fermare alla prima"
    assert res is None, res
    assert g.state()["off_for"] > 0, g.state()


def test_errori_http_tornano_none():
    g = mkguard(clock=time.monotonic)
    m = E.MockGroq(fail_times=99, status=500)
    _, res = _dettatura_lunga(m, g)
    assert res is None, "con Groq giu' il testo finale deve venire dal batch"


# ============================ file dei contatori ============================
def test_contatori_sopravvivono_al_riavvio():
    p = tmp_usage()
    g1 = mkguard(path=p, day=lambda: "2026-09-19")
    for _ in range(3):
        g1.note_request(20.0)
    g2 = mkguard(path=p, day=lambda: "2026-09-19")     # come dopo un riavvio dell'app
    assert g2.state()["rpd"] == 3 and abs(g2.state()["asd"] - 60.0) < 1e-6, g2.state()


def test_file_contatori_corrotto_non_ferma_niente():
    p = tmp_usage()
    open(p, "w", encoding="utf-8").write("{{ questo non e' json")
    g = mkguard(path=p, day=lambda: "2026-09-19")
    assert g.state()["rpd"] == 0
    assert g.allow(10.0) is True
    g.note_request(12.0)
    riletto = json.load(open(p, encoding="utf-8"))     # riscritto sano
    assert riletto["2026-09-19"]["req"] == 1 and riletto["2026-09-19"]["audio"] == 12.0, riletto


def test_file_contatori_assente_o_non_scrivibile():
    g = mkguard(path=os.path.join(tempfile.mkdtemp(), "manca", "groq_usage.json"))
    assert g.allow(10.0) is True
    g.note_request(10.0)                               # cartella inesistente: log e avanti
    assert g.state()["rpd"] == 1, g.state()


def test_minimo_fatturato_dieci_secondi():
    g = mkguard()
    g.note_request(2.0)
    assert g.state()["asd"] == 10.0, g.state()


def test_note_batch_request_conta_sulla_guardia_di_processo():
    """Le richieste del percorso batch di wavetype.py consumano gli stessi tetti."""
    g = mkguard(clock=time.monotonic)
    old = D._GUARD
    try:
        D._GUARD = g
        D.note_batch_request(45.0)
        D.note_batch_request(1.0)
        assert g.state()["rpd"] == 2 and g.state()["asd"] == 55.0, g.state()
    finally:
        D._GUARD = old


# ============================ robustezza ============================
def test_lato_groq_che_esplode_non_arriva_al_chiamante():
    loc = StubLocal()
    eng = D.DualEngine(log=QUIET, local=loc, groq=BoomGroq(), guard_obj=mkguard())
    eng.start()
    eng.feed(np.zeros(100, dtype="float32"))
    assert eng.snapshot() == loc.snap
    assert eng.stop(timeout=1) is None
    eng.cancel()
    assert loc.started == 1 and loc.stopped == 1 and loc.cancelled == 1


def test_senza_chiave_groq_resta_solo_anteprima():
    loc = StubLocal()
    eng = D.DualEngine(None, log=QUIET, local=loc, guard_obj=mkguard())
    eng.start()
    eng.feed(np.zeros(100, dtype="float32"))
    assert eng.groq is None and eng.stop(timeout=1) is None and eng.snapshot() == loc.snap


def test_stop_rispetta_il_timeout():
    m = E.MockGroq(hang=6.0)
    a, _ = E.synth(speech(32))
    eng = dual(http_post=m, pace=0.2)
    eng.start()
    E.feed_all(eng, a, L.SR)
    E.pump(eng, 0.4)
    t0 = time.monotonic()
    res = eng.stop(timeout=1.0)
    dt = time.monotonic() - t0
    assert res is None and dt < 2.5, f"stop() ha bloccato {dt:.1f}s"


def test_niente_thread_appesi():
    import threading
    base = threading.active_count()
    for _ in range(3):
        m = E.MockGroq()
        eng, res = _dettatura_lunga(m, mkguard(clock=time.monotonic))
        assert res is not None, "il testo finale doveva arrivare dal live"
    time.sleep(0.3)
    assert threading.active_count() <= base + 1, \
        f"{threading.active_count()} thread vivi (era {base})"


# ============================ registrazione vera (Groq vero) ============================
def real(name=None, speed=1.0):
    """End to end: audio d'archivio lungo, modello locale vero, Groq vero. Stampa solo numeri:
    il testo dettato e' personale e non si cita."""
    path = os.path.join(REC_DIR, name or "20260914-143848.wav")
    if not os.path.exists(path):
        print("   [real] registrazione assente, salto")
        return 0
    key = open(os.path.join(ROOT, "groq_key.txt"), encoding="utf-8").read().strip()
    a, sr = E.read_wav(path)
    dur = len(a) / sr
    batch = E.groq_batch(path, key)                 # riferimento: dalla cache se c'e' gia'
    g = D.Guard(log=print, path=tmp_usage())
    eng = D.DualEngine(key, log=print, sr_in=sr, guard_obj=g)
    eng.local._ready.wait(120)
    if eng.local.backend is None:
        print("   [real] modello locale assente in models/: salto")
        return 0
    print(f"   [real] {os.path.basename(path)}: {dur:.1f}s a {speed}x")
    eng.start()
    E.feed_stream(eng, a, sr, speed=speed, block=0.05)
    prev = eng.snapshot()
    t0 = time.perf_counter()
    res = eng.stop(timeout=30)
    dt = time.perf_counter() - t0
    req = getattr(eng.groq, "_requests", 0)
    st = g.state()
    if res is None:
        print(f"   [real] stop() -> None in {dt:.2f}s ({req} richieste live): sarebbe il batch")
        return 1
    diff, tot, det = E.word_diff(res[0], batch["text"])
    pre_diff, pre_tot, _ = E.word_diff(prev["committed"] + " " + prev["tentative"], batch["text"])
    print(f"   [real] richieste live {req} | audio fatturato {st['asd']:.0f}s (batch: "
          f"{max(10.0, dur):.0f}s) | stop->testo {dt:.2f}s (batch misurato "
          f"{batch['latency']:.2f}s) | parole finali {len(res[0].split())} "
          f"(batch {tot}) | uguali {100 * (1 - diff / max(1, tot)):.1f}% {det} | "
          f"lingua {res[1]}")
    print(f"   [real] anteprima locale allo stop: {100 * (1 - pre_diff / max(1, pre_tot)):.1f}% "
          f"uguale al batch | header Groq {g.headers}")
    return 0


# ============================ runner ============================
def run_unit():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    bad = 0
    for n, f in tests:
        t0 = time.perf_counter()
        try:
            f()
            print(f"  ok   {n}  ({time.perf_counter() - t0:.1f}s)")
        except Exception as e:
            bad += 1
            print(f"  FAIL {n}: {type(e).__name__}: {e}")
    print(f"\nunit: {len(tests) - bad}/{len(tests)} passati")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="registrazione vera + Groq vero")
    ap.add_argument("--file", default="")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--only-real", action="store_true")
    a = ap.parse_args()
    bad = 0 if a.only_real else run_unit()
    if a.real or a.only_real:
        bad += real(a.file or None, speed=a.speed)
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
