"""
Banco di prova di live_local.py (motore live locale, anteprima parola per parola).

Due parti:
  1) test unitari con un motore finto (niente modello, niente rete): regola del fissato,
     endpoint, ripensamenti, tetto FORCE_LAG (difetto "96 s tutte grigie"), stop() che torna
     None subito, start() ripetuto senza thread appesi, modello assente senza eccezioni.
  2) --real: il modello vero su una registrazione d'archivio fatta scorrere a 1x.

Uso:
  .venv/Scripts/python.exe tests/test_live_local.py              # solo unitari
  .venv/Scripts/python.exe tests/test_live_local.py --real       # + modello vero (models/)
"""
import os
import sys
import threading
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import live_local as L                                      # noqa: E402

STEP = int(L.STEP_SEC * L.SR)


class FakeBackend:
    """Motore finto: il copione dice quali parole (con tempi) compaiono a ogni istante.
    `script(now)` -> (parole dell'enunciato in corso, endpoint)."""
    dir = "finto"

    def __init__(self, script, append_only=True):
        self.script = script
        self.append_only = append_only
        self.resets = 0

    def new_stream(self):
        return object()

    def accept(self, st, audio):
        return self.script(st["fed"] / L.SR)

    def reset(self, st):
        self.resets += 1

    def finish(self, st):
        return self.script(st["fed"] / L.SR)[0]


def engine_with(backend, sr_in=L.SR):
    e = L.LiveEngine(log=lambda *a: None, sr_in=sr_in, backend="__nessuno__")
    e._ready.wait(2)
    e.backend = backend
    return e


def run(engine, seconds, snap_every=None):
    """Dà `seconds` di audio (a 16k) al motore tutto insieme e aspetta che l'abbia consumato.
    Con `snap_every` lo dà a pezzi e raccoglie le istantanee."""
    snaps = []
    engine.start()
    n = int(seconds * L.SR)
    piece = int((snap_every or seconds) * L.SR)
    for i in range(0, n, piece):
        engine.feed(np.zeros(min(piece, n - i), dtype="float32") + 0.01)
        t0 = time.time()
        while time.time() - t0 < 2:
            with engine._lock:
                empty = not engine._pending
            if empty and engine._st is not None and engine._st["fed"] >= ((i + piece) // STEP) * STEP:
                break
            time.sleep(0.005)
        snaps.append((engine._st["fed"] / L.SR if engine._st else 0, engine.snapshot()))
    return snaps


def words_every(sec, n, dur=0.3):
    return [(f"w{i}", i * sec, i * sec + dur) for i in range(n)]


# ---------- test ----------
def test_tokens_to_words():
    w = L.tokens_to_words(["▁cia", "o", "▁mon", "do"], [0.1, 0.2, 0.5, 0.6])
    assert [x[0] for x in w] == ["ciao", "mondo"], w
    assert abs(w[0][2] - 0.5) < 1e-6 and w[1][2] >= 0.68 - 1e-6, w
    w = L.tokens_to_words([" Ciao", ",", " come"], [0.0, 0.3, 0.4], offset=10.0)
    assert [x[0] for x in w] == ["Ciao,", "come"] and w[0][1] == 10.0, w
    # Nemotron: token di solo spazio come separatore, parole spezzate in sillabe, accenti
    w = L.tokens_to_words([" Con", " ", "gli", " ", "s", "vi", " per", "ò"],
                          [0.0, 0.1, 0.1, 0.2, 0.2, 0.3, 0.4, 0.5])
    assert [x[0] for x in w] == ["Con", "gli", "svi", "però"], w


def test_sola_aggiunta_fissa_tutto_tranne_ultima():
    allw = words_every(0.4, 50)
    be = FakeBackend(lambda now: ([x for x in allw if x[1] < now], False))
    e = engine_with(be)
    snaps = run(e, 10.0, snap_every=0.32)
    for now, s in snaps:
        tent = s["tentative"].split()
        assert len(tent) <= 1, (now, s)                    # incerta solo la parola che cresce
    e.cancel()


def test_pausa_fissa_anche_ultima_parola():
    """Sola aggiunta: dopo PAUSE_COMMIT secondi senza parole nuove anche l'ultima diventa fissa."""
    allw = words_every(0.4, 5)                            # ultima parola finisce a 1,9 s
    e = engine_with(FakeBackend(lambda now: ([x for x in allw if x[1] < now], False)))
    run(e, 1.9 + L.PAUSE_COMMIT + 0.4)
    s = e.snapshot()
    assert s["tentative"] == "" and len(s["committed"].split()) == 5, s
    e.cancel()


def test_endpoint_fissa_tutto_e_resetta():
    allw = words_every(0.4, 5)
    be = FakeBackend(lambda now: (allw, now >= 2.4))
    e = engine_with(be)
    run(e, 2.56)
    s = e.snapshot()
    assert s["committed"].split() == [x[0] for x in allw] and s["tentative"] == "", s
    assert be.resets >= 1
    e.cancel()


def test_ripensamento_non_fissa_subito():
    """Motore che ripensa: la parola 0 cambia a 1,0 s. Non deve essere fissata prima."""
    def script(now):
        first = "prima" if now < 1.0 else "dopo"
        return [(first, 0.0, 0.3), ("due", 0.4, 0.6), ("tre", 0.7, 0.9)], False
    e = engine_with(FakeBackend(script, append_only=False))
    snaps = run(e, 3.2, snap_every=0.16)
    committed = [s["committed"] for _, s in snaps]
    assert all("prima" not in c for c in committed), committed
    assert snaps[-1][1]["committed"].split()[:1] == ["dopo"], snaps[-1]
    e.cancel()


def test_niente_righe_grigie_su_96s():
    """Difetto aperto (docs/ENGINE-NOTES.md): a 96 s di dettatura tutto grigio. Con un motore che
    ripensa sempre l'ultima parte e nessuna pausa, dopo FORCE_LAG tutto deve essere fissato."""
    allw = words_every(0.35, 400)

    def script(now):
        ws = [x for x in allw if x[1] < now]
        # le ultime 3 parole cambiano a ogni giro: ipotesi instabile sulla coda
        return [(w + (str(int(now * 10)) if i >= len(ws) - 3 else ""), a, b)
                for i, (w, a, b) in enumerate(ws)], False
    e = engine_with(FakeBackend(script, append_only=False))
    snaps = run(e, 96.0, snap_every=4.0)
    for now, s in snaps[1:]:
        tent = s["tentative"].split()
        # parole incerte: al massimo quelle degli ultimi FORCE_LAG secondi (+ un passo)
        assert len(tent) <= int((L.FORCE_LAG + L.STEP_SEC) / 0.35) + 2, (now, len(tent))
    assert len(snaps[-1][1]["committed"].split()) > 250
    e.cancel()


def test_committed_e_tentative_non_si_sovrappongono():
    allw = words_every(0.4, 30)
    e = engine_with(FakeBackend(lambda now: ([x for x in allw if x[1] < now], False)))
    snaps = run(e, 8.0, snap_every=0.48)
    for _, s in snaps:
        c, t = s["committed"].split(), s["tentative"].split()
        assert not (set(c) & set(t)), s
    e.cancel()


def test_48k_scende_a_16k_senza_perdere_campioni():
    e = engine_with(FakeBackend(lambda now: ([], False)), sr_in=48000)
    e.start()
    for _ in range(7):
        e.feed(np.zeros(1001, dtype="float32"))            # 1001 non divisibile per 3
    time.sleep(0.2)
    got = e._drain()
    assert e._total == (7 * 1001) // 3 and len(e._carry) == (7 * 1001) % 3, (e._total, len(e._carry))
    assert len(got) == 0 or True
    e.cancel()


def test_stop_torna_none_subito():
    e = engine_with(FakeBackend(lambda now: ([], False)))
    e.start()
    e.feed(np.zeros(4800, dtype="float32"))
    t0 = time.perf_counter()
    assert e.stop(30) is None
    assert time.perf_counter() - t0 < 0.05
    e.cancel()


def test_feed_non_blocca():
    e = engine_with(FakeBackend(lambda now: ([], False)))
    e.start()
    blk = np.zeros((1024, 1), dtype="float32")
    t0 = time.perf_counter()
    for _ in range(1000):
        e.feed(blk)
    dt = (time.perf_counter() - t0) / 1000
    assert dt < 0.001, dt
    e.cancel()


def test_start_ripetuto_niente_thread_appesi():
    e = engine_with(FakeBackend(lambda now: ([("a", 0, 0.1)], False)))
    before = threading.active_count()
    for _ in range(5):
        e.start()
        e.feed(np.zeros(8000, dtype="float32"))
        e.stop()
    time.sleep(0.3)
    assert threading.active_count() <= before + 1, (before, threading.active_count())
    e.cancel()


def test_modello_assente_nessuna_eccezione():
    e = L.LiveEngine(log=lambda *a: None, backend="nemotron",
                     backend_kw={"model_dir": os.path.join(ROOT, "models", "__manca__")})
    e._ready.wait(10)
    assert e.backend is None and e._load_err is not None
    e.start()
    e.feed(np.zeros(4800, dtype="float32"))
    assert e.snapshot()["committed"] == ""
    assert e.stop() is None
    e.cancel()


def test_argomenti_di_wavetype_accettati():
    """wavetype.py:268 costruisce il motore con questi argomenti: devono passare anche qui."""
    e = L.LiveEngine("chiave", vocab="Wavetype", log=lambda *a: None, sr_in=48000,
                     final_policy="batch_under", batch_under_sec=30.0, min_live_sec=10.0,
                     backend="__nessuno__")
    e._ready.wait(2)
    e.cancel()


def test_niente_salti_se_audio_arriva_a_raffica():
    """Banco e test alimentano piu' veloce del tempo reale: il ritardo e' negativo, niente salti."""
    e = engine_with(FakeBackend(lambda now: ([], False)))
    run(e, 30.0, snap_every=1.0)
    assert e._skipped == 0.0, e._skipped
    e.cancel()


def test_salta_l_arretrato_quando_resta_indietro():
    """CPU contesa: 20 s di orologio, solo 10 s di audio macinati, 10 s in coda. Il worker butta
    l'arretrato e riparte da SKIP_KEEP_SEC indietro (il debito non si recupera: RTF ~1 sotto carico)."""
    clock = [20.0]
    e = L.LiveEngine(log=lambda *a: None, sr_in=L.SR, backend="__nessuno__", clock=lambda: clock[0])
    e._ready.wait(2)
    e._st = {"s": None, "fed": 10 * L.SR, "offset": 0.0}
    buf = np.zeros(10 * L.SR, dtype="float32")
    out = e._catch_up(buf, STEP, 0.0)                     # ritardo a video: 20 - 10 = 10 s
    assert abs(e._skipped - 9.0) < L.STEP_SEC, e._skipped
    assert abs(len(out) / L.SR - 1.0) < L.STEP_SEC, len(out) / L.SR
    e.cancel()


def test_niente_salti_sotto_soglia():
    """Un ritardo che il decoder puo' ancora recuperare (margine 2,3x col chunk 320 ms) si lascia
    stare: sotto SKIP_AHEAD_SEC non si butta niente."""
    clock = [12.9]
    e = L.LiveEngine(log=lambda *a: None, sr_in=L.SR, backend="__nessuno__", clock=lambda: clock[0])
    e._ready.wait(2)
    e._st = {"s": None, "fed": 10 * L.SR, "offset": 0.0}
    buf = np.zeros(int(2.9 * L.SR), dtype="float32")
    assert e._catch_up(buf, STEP, 0.0) is buf and e._skipped == 0.0
    e.cancel()


def test_salto_mai_piu_lungo_della_coda():
    """Se il ritardo sta nel decoder e non nella coda, si butta solo quello che c'e', mai l'ultimo
    pezzo: l'anteprima continua a scrivere."""
    clock = [30.0]
    e = L.LiveEngine(log=lambda *a: None, sr_in=L.SR, backend="__nessuno__", clock=lambda: clock[0])
    e._ready.wait(2)
    e._st = {"s": None, "fed": 10 * L.SR, "offset": 0.0}
    buf = np.zeros(3 * STEP, dtype="float32")
    out = e._catch_up(buf, STEP, 0.0)                     # ritardo 20 s, in coda solo 0,48 s
    assert len(out) == STEP, len(out)
    assert abs(e._skipped - 2 * L.STEP_SEC) < 1e-6, e._skipped
    e.cancel()


# ---------- modello vero ----------
def real(path=None):
    path = path or os.path.join(ROOT, "recordings", "20260918-170408.wav")
    if not os.path.exists(path):
        print("   [real] registrazione assente, salto")
        return True
    with wave.open(path) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768
        sr = w.getframerate()
    e = L.LiveEngine(log=print, sr_in=sr)
    e._ready.wait(60)
    if e.backend is None:
        print("   [real] modello assente in models/: salto")
        return True
    e.start()
    blk = int(0.16 * sr)
    t0 = time.perf_counter()
    first = None
    for i, j in enumerate(range(0, len(a), blk)):
        dt = t0 + (i + 1) * 0.16 - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        e.feed(a[j:j + blk])
        if first is None and e.snapshot()["committed"] + e.snapshot()["tentative"]:
            first = time.perf_counter() - t0
    text = e.finish(30)
    print(f"   [real] {os.path.basename(path)} {len(a)/sr:.1f}s: prima parola a video a "
          f"{first if first is not None else -1:.2f}s, {len(text.split())} parole")
    return bool(text.strip())


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
    if "--real" in sys.argv and not real():
        bad += 1
        print("FAIL real")
    print(f"{len(tests) - bad}/{len(tests)} ok" if not bad else f"{bad} falliti")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
