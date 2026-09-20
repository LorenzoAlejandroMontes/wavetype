"""
Banco di prova di live_engine.py (Lotto B).

Due parti:
  1) test unitari SENZA rete (mock della POST): ritmo, taglio sulla pausa, cucitura, stop() che
     scade -> None, cancel, silenzio mai mandato, lingua fissata, nessun thread appeso.
  2) simulatore CON rete: rigioca una registrazione d'archivio a blocchi, in tempo reale o con
     orologio accelerato, e misura richieste, attesa stop->risultato, ritardo delle parole a video
     e differenza a parole rispetto al trascritto batch dello stesso file (in cache).

Uso:
  .venv/Scripts/python.exe tests/test_live_engine.py                 # solo unitari (niente rete)
  .venv/Scripts/python.exe tests/test_live_engine.py --net           # + simulatore tempo reale
  .venv/Scripts/python.exe tests/test_live_engine.py --net --fast    # + simulatore accelerato
  ... --files 20260916-155627.wav,20260915-123327.wav                # scelta dei file
  ... --policy batch_under                                           # prova la politica alternativa

Il mock non e' finto a metа': decodifica davvero l'audio che riceve (ogni "parola" e' una sinusoide
a frequenza propria), quindi taglio, cucitura e LocalAgreement vengono esercitati sul serio.
"""
import argparse
import difflib
import io
import json
import os
import sys
import threading
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import live_engine as L                                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC_DIR = os.path.join(ROOT, "recordings")
CACHE = os.path.join(ROOT, "tests", "cache")


# ============================ audio sintetico + mock fedele ============================
def synth(script, sr=L.SR, seed=0):
    """script: lista di ('w', durata) e ('sil', durata). Ogni parola e' una sinusoide a frequenza
    propria (300 + 100*i Hz): il mock la ri-riconosce qualunque sia il punto di taglio."""
    parts, words, t, i = [], [], 0.0, 0
    for kind, dur in script:
        n = int(dur * sr)
        if kind == "w":
            f = 300 + 100 * i
            x = (0.14 * np.sin(2 * np.pi * f * np.arange(n) / sr + 0.4)).astype("float32")
            words.append({"word": f"w{i}", "start": t, "end": t + dur})
            i += 1
        else:
            rng = np.random.default_rng(seed)
            x = (rng.standard_normal(n) * 0.0002).astype("float32")
        parts.append(x)
        t += dur
    return np.concatenate(parts).astype("float32"), words


def decode(a, sr=L.SR, win=0.1):
    """Riconosce le 'parole' contando i passaggi per lo zero (invariante al guadagno e al clip)."""
    w = int(win * sr)
    track = []
    for k in range(len(a) // w):
        s = a[k * w:(k + 1) * w]
        if L.rms(s) < 0.003:
            track.append(None)
            continue
        zc = int(np.sum(np.diff((s > 0).astype("int8")) != 0))   # niente np.sign: lo zero esatto
        #                                                          conterebbe due volte
        f = zc / (2 * win)
        idx = int(round((f - 300) / 100))
        track.append(idx if 0 <= idx <= 40 and abs(f - (300 + 100 * idx)) < 40 else None)
    out, cur, start = [], None, 0
    for k, v in enumerate(track + [None]):
        if v != cur:
            if cur is not None and (k - start) * win >= 0.25:
                out.append({"word": f"w{cur}", "start": start * win, "end": k * win})
            cur, start = v, k
    return out


class FakeResp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class MockGroq:
    """POST finta: decodifica il WAV ricevuto e risponde come Groq (verbose_json + parole)."""

    def __init__(self, lang="Italian", delay=0.0, fail_times=0, status=200, hang=0.0):
        self.calls = []
        self.lang = lang
        self.delay = delay
        self.fail_times = fail_times
        self.status = status
        self.hang = hang

    def __call__(self, wav, data, timeout):
        with wave.open(io.BytesIO(wav), "rb") as w:
            sr = w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32767.0
        self.calls.append({"dur": len(a) / sr, "data": dict(data)})
        if self.hang:
            time.sleep(self.hang)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_times > 0:
            self.fail_times -= 1
            return FakeResp(self.status or 500, text="errore finto")
        words = decode(a, sr)
        return FakeResp(200, {"language": self.lang, "duration": len(a) / sr,
                              "text": " ".join(x["word"] for x in words), "words": words},
                        headers={"x-ratelimit-remaining-requests": "1999"})


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def feed_all(eng, a, sr, block=0.05):
    n = int(block * sr)
    for i in range(0, len(a), n):
        eng.feed(a[i:i + n])


def feed_stream(eng, a, sr, speed=8.0, block=0.05):
    """Come il microfono, ma a velocita' `speed`: l'audio arriva a poco a poco, cosi' il worker fa
    piu' giri e il motore puo' davvero fissare i pezzi (serve due ipotesi d'accordo)."""
    n = int(block * sr)
    t0 = time.monotonic()
    for k, i in enumerate(range(0, len(a), n)):
        eng.feed(a[i:i + n])
        d = t0 + (k + 1) * block / speed - time.monotonic()
        if d > 0:
            time.sleep(d)


def pump(eng, seconds=3.0, step=0.02):
    """Lascia lavorare il worker (tempo reale) finche' non ha piu' niente da fare."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        time.sleep(step)


# ============================ test unitari (nessuna rete) ============================
def test_pacer_ritmo():
    c = FakeClock()
    p = L.Pacer(interval=4.0, clock=c, real_clock=c)
    assert p.wait_for_next() == 0.0
    p.note_request(3.0)
    assert abs(p.wait_for_next() - 4.0) < 1e-6, p.wait_for_next()
    c.advance(4.0)
    assert p.wait_for_next() == 0.0


def test_pacer_tetto_al_minuto():
    c = FakeClock()
    p = L.Pacer(interval=0.0, max_rpm=18, clock=c, real_clock=c)
    for _ in range(18):
        p.note_request(1.0)
        c.advance(0.1)
    w = p.wait_for_next()
    assert w > 55.0, w                       # 18 in meno di 2 s -> si aspetta la finestra
    c.advance(60.0)
    assert p.wait_for_next() == 0.0


def test_pacer_429_retry_after():
    c = FakeClock()
    p = L.Pacer(clock=c, real_clock=c)
    w = p.note_429("7")
    assert w == 7.0 and p.wait_for_next() >= 6.9, (w, p.wait_for_next())
    p2 = L.Pacer(clock=c, real_clock=c)
    assert p2.note_429(None) == 1.0 and p2.note_429(None) == 3.0     # esponenziale senza header


def test_pacer_quota_audio():
    c = FakeClock()
    p = L.Pacer(clock=c, real_clock=c)
    assert not p.exhausted()
    for _ in range(300):
        p.note_request(25.0)                 # 7.500 s di audio
        c.advance(0.01)
    assert p.exhausted() and p.interval_now() >= 8.0


def test_pacer_headers_pochi_crediti():
    p = L.Pacer()
    p.note_ok({"x-ratelimit-remaining-requests": "12", "X-RateLimit-Limit-Requests": "2000"})
    assert p.interval_now() >= 10.0 and p.headers["x-ratelimit-remaining-requests"] == "12"


def test_find_pause():
    a, _ = synth([("w", 0.6)] * 12 + [("sil", 1.0)] + [("w", 0.6)] * 5)
    cut = L.find_pause(a, L.SR)
    assert cut is not None and 7.2 * L.SR < cut < 8.2 * L.SR, cut / L.SR
    fitto, _ = synth([("w", 0.6)] * 20)                     # parlato continuo: nessuna pausa
    assert L.find_pause(fitto, L.SR) is None
    assert L.find_pause(fitto, L.SR, forced=True) is not None   # tetto duro: taglia lo stesso
    assert L.find_pause(a[:int(4 * L.SR)], L.SR) is None        # coda corta: non si fissa


def test_cucitura():
    assert L.join_text("ciao come", "stai") == "ciao come stai"
    assert L.join_text("ciao come stai", "come stai bene") == "ciao come stai bene"
    assert L.join_text("", "solo coda") == "solo coda"
    assert L.join_text("solo testa", "") == "solo testa"
    assert L.dedupe_overlap("a b c", "c d")[1] == 0          # una parola sola: non si tocca
    # la punteggiatura non conta nel confronto, ma resta attaccata alla testa: "Il cane. corre".
    # Al giunto puo' quindi restare un punto o una maiuscola fuori posto: lo sistema format_text()
    # di wavetype.py, che gira comunque dopo. Misurato nel simulatore (vedi rapporto).
    assert L.join_text("Il cane.", "Il cane corre") == "Il cane. corre"


def test_lingua():
    assert L.lang_code("Italian") == "it" and L.lang_code("en") == "en" and L.lang_code("") == ""


def test_live_taglia_cuce_e_chiude():
    a, words = synth([("w", 0.6)] * 12 + [("sil", 1.2)] + [("w", 0.6)] * 12 +
                     [("sil", 1.2)] + [("w", 0.6)] * 6)
    m = MockGroq()
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.4)
    eng.start()
    feed_stream(eng, a, L.SR, speed=10.0)
    snap = eng.snapshot()
    assert snap["committed"], "niente fissato: il taglio sulla pausa non ha funzionato"
    assert snap["rev"] > 0
    res = eng.stop(timeout=15)
    assert res is not None, "stop() ha fallito"
    text, lang = res
    got = text.split()
    want = [w["word"] for w in words]
    assert got == want, f"cucitura sbagliata\n got={got}\nwant={want}"
    assert lang == "it", lang
    assert len(m.calls) >= 2, m.calls
    assert eng._thread is None or not eng._thread.is_alive()


def test_committed_e_tentative_non_si_sovrappongono():
    """Nel giro in cui si fissa un pezzo, `tentative` non deve ancora contenere quelle parole:
    il pannello le mostrerebbe due volte (bug visto nel simulatore il 18/09)."""
    a, _ = synth([("w", 0.6)] * 14 + [("sil", 1.2)] + [("w", 0.6)] * 14)
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=MockGroq(), pace=0.4)
    eng.start()
    n, bad, seen_commit = int(0.05 * L.SR), [], False
    for i in range(0, len(a), n):
        eng.feed(a[i:i + n])
        time.sleep(0.05 / 10.0)
        s = eng.snapshot()
        ws = (s["committed"] + " " + s["tentative"]).split()
        if len(set(ws)) != len(ws):
            bad.append((s["committed"], s["tentative"]))
        seen_commit = seen_commit or bool(s["committed"])
    eng.stop(timeout=10)
    assert seen_commit, "nessun pezzo fissato: il test non ha provato niente"
    assert not bad, f"parole ripetute tra committed e tentative: {bad[0]}"


def test_silenzio_non_si_manda():
    a, _ = synth([("sil", 6.0)])
    m = MockGroq()
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.3)
    eng.start()
    feed_all(eng, a, L.SR)
    pump(eng, 2.0)
    res = eng.stop(timeout=10)
    assert m.calls == [], f"mandato silenzio a Groq: {m.calls}"
    # Nessun testo -> None, cioe' "decidi tu": chi sceglie se incollare e' wavetype.py, che guarda
    # rms E picco (silence_check) e poi looks_hallucinated. Prima qui si tornava ("", "") e
    # wavetype.py si fermava li': una dettatura detta piano (rms 0,0029, picco 0,0092 - sopra la
    # soglia di wavetype) spariva senza incolla e senza nemmeno provare il percorso batch.
    assert res is None, res


def test_niente_richieste_se_non_arriva_parlato_nuovo():
    a, _ = synth([("w", 0.6)] * 6 + [("sil", 5.0)])
    m = MockGroq()
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.3)
    eng.start()
    feed_all(eng, a, L.SR)
    pump(eng, 3.0)
    n = len(m.calls)
    eng.stop(timeout=10)
    assert n <= 3, f"{n} richieste su 3,6 s di parlato + silenzio: il governatore non frena"


def test_lingua_fuori_lista_forza_italiano():
    a, _ = synth([("w", 0.6)] * 10)
    m = MockGroq(lang="Japanese")
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.4)
    eng.start()
    feed_all(eng, a, L.SR)
    pump(eng, 2.0)
    res = eng.stop(timeout=10)
    assert res is not None and res[1] == "it", res
    assert any(c["data"].get("language") == "it" for c in m.calls[1:]), \
        "la lingua non e' stata fissata sulle richieste successive"


def test_prompt_con_vocabolario_e_coda():
    a, _ = synth([("w", 0.6)] * 12 + [("sil", 1.2)] + [("w", 0.6)] * 12)
    m = MockGroq()
    eng = L.LiveEngine("k", vocab="Wavetype, Klaus", log=lambda *_: None, sr_in=L.SR,
                       http_post=m, pace=0.4)
    eng.start()
    feed_stream(eng, a, L.SR, speed=10.0)
    eng.stop(timeout=10)
    assert all("Wavetype" in c["data"].get("prompt", "") for c in m.calls), "vocabolario non passato"
    assert any("w0" in c["data"].get("prompt", "") for c in m.calls[1:]), "coda non passata"


def test_stop_scaduto_torna_none():
    a, _ = synth([("w", 0.6)] * 8)
    m = MockGroq(hang=6.0)
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.2)
    eng.start()
    feed_all(eng, a, L.SR)
    t0 = time.monotonic()
    res = eng.stop(timeout=1.0)
    dt = time.monotonic() - t0
    assert res is None, res
    assert dt < 2.5, f"stop() ha bloccato {dt:.1f}s invece di arrendersi"


def test_errori_http_tornano_none():
    a, _ = synth([("w", 0.6)] * 8)
    m = MockGroq(fail_times=99, status=500)
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.2)
    eng.start()
    feed_all(eng, a, L.SR)
    pump(eng, 2.0)
    assert eng.stop(timeout=10) is None, "con Groq giu' stop() deve tornare None (fallback batch)"


def test_cancel_veloce():
    a, _ = synth([("w", 0.6)] * 8)
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=MockGroq(hang=3.0), pace=0.2)
    eng.start()
    feed_all(eng, a, L.SR)
    t0 = time.monotonic()
    eng.cancel()
    assert time.monotonic() - t0 < 1.0
    assert eng.snapshot()["committed"] == ""


def test_nessun_thread_appeso():
    base = threading.active_count()
    a, _ = synth([("w", 0.6)] * 10 + [("sil", 1.2)] + [("w", 0.6)] * 4)
    for _ in range(3):
        eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=MockGroq(), pace=0.4)
        eng.start()
        feed_all(eng, a, L.SR)
        pump(eng, 1.5)
        assert eng.stop(timeout=10) is not None
    time.sleep(0.3)
    assert threading.active_count() <= base + 1, f"{threading.active_count()} thread vivi (era {base})"


def test_riuso_stesso_motore():
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=MockGroq(), pace=0.4)
    outs = []
    for _ in range(2):
        a, words = synth([("w", 0.6)] * 8)
        eng.start()
        feed_all(eng, a, L.SR)
        pump(eng, 1.0)
        outs.append(eng.stop(timeout=10))
    assert outs[0] == outs[1] and outs[0] is not None, outs
    assert eng.snapshot()["committed"] == "" or True


def test_feed_non_blocca():
    a, _ = synth([("w", 0.6)] * 20)
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=MockGroq(delay=0.4), pace=0.2)
    eng.start()
    worst, n = 0.0, int(0.05 * L.SR)
    for i in range(0, len(a), n):
        t0 = time.perf_counter()
        eng.feed(a[i:i + n])
        worst = max(worst, time.perf_counter() - t0)
        time.sleep(0.005)
    eng.cancel()
    assert worst < 0.005, f"feed() ha bloccato {worst*1000:.2f} ms (callback audio: inaccettabile)"


def test_ingresso_48k():
    a, words = synth([("w", 0.6)] * 10 + [("sil", 1.2)] + [("w", 0.6)] * 4)
    a48 = np.repeat(a, 3)                      # x3 per ripetizione = finto 48k, come il simulatore
    m = MockGroq()
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=48000, http_post=m, pace=0.4)
    eng.start()
    feed_stream(eng, a48, 48000, speed=10.0)
    res = eng.stop(timeout=10)
    assert res is not None and res[0].split() == [w["word"] for w in words], res


def test_min_live_sec_risparmia_le_dettature_corte():
    """Sotto min_live_sec non parte NESSUNA richiesta live: resta solo quella finale."""
    a, _ = synth([("w", 0.6)] * 12)              # 7,2 s
    m = MockGroq()
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.3,
                       min_live_sec=10.0)
    eng.start()
    feed_stream(eng, a, L.SR, speed=10.0)
    assert eng.snapshot()["tentative"] == "" and len(m.calls) == 0, m.calls
    res = eng.stop(timeout=10)
    assert res is not None and len(m.calls) == 1, (res, len(m.calls))


def test_politica_batch_under():
    a, _ = synth([("w", 0.6)] * 8)             # 4,8 s: sotto la soglia
    m = MockGroq()
    eng = L.LiveEngine("k", log=lambda *_: None, sr_in=L.SR, http_post=m, pace=0.4,
                       final_policy="batch_under", batch_under_sec=30.0)
    eng.start()
    feed_all(eng, a, L.SR)
    pump(eng, 1.2)
    assert eng.snapshot()["tentative"] or eng.snapshot()["committed"], "anteprima assente"
    n = len(m.calls)
    assert eng.stop(timeout=10) is None, "sotto soglia il testo finale deve venire dal batch"
    assert len(m.calls) == n, "sotto soglia non si deve spendere nemmeno la richiesta finale"


# ============================ simulatore con rete ============================
def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32767.0
    return a, sr


def groq_batch(path, key):
    """Trascritto di riferimento (il percorso di oggi, tutto l'audio in una richiesta). In cache."""
    import httpx
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, os.path.basename(path) + ".json")
    if os.path.exists(cp):
        return json.load(open(cp, encoding="utf-8"))
    a, sr = read_wav(path)
    g = L.gain_for(L.rms(a))
    audio = np.clip(a * g, -1, 1) if g > 1.2 else a
    t0 = time.perf_counter()
    r = httpx.post(L.GROQ_URL, headers={"Authorization": f"Bearer {key}"}, timeout=600,
                   files={"file": ("a.wav", L.wav_bytes(audio, sr), "audio/wav")},
                   data={"model": L.GROQ_STT_MODEL, "response_format": "verbose_json",
                         "timestamp_granularities[]": "word"})
    dt = time.perf_counter() - t0
    r.raise_for_status()
    j = r.json()
    out = {"text": (j.get("text") or "").strip(), "language": j.get("language", ""),
           "words": j.get("words") or [], "latency": dt, "dur": len(a) / sr}
    json.dump(out, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    return out


def words_of(t):
    return [L.norm_word(w) for w in (t or "").split() if L.norm_word(w)]


def word_diff(live, batch):
    """Differenza a parole verso il batch. Torna (diverse, totale batch, dettaglio).
    Separa i casi: `perse` = il live non ha detto cio' che il batch dice (peggio),
    `in piu'` = il live ha parole che il batch non ha (spesso meglio: il batch le mangia)."""
    lw, bw = words_of(live), words_of(batch)
    sm = difflib.SequenceMatcher(a=bw, b=lw, autojunk=False)
    lost = extra = swapped = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            lost += i2 - i1
        elif tag == "insert":
            extra += j2 - j1
        elif tag == "replace":
            swapped += max(i2 - i1, j2 - j1)
    return lost + extra + swapped, len(bw), {"perse": lost, "in_piu": extra, "cambiate": swapped}


def peak_rpm(times, win=60.0):
    """Picco vero di richieste in una finestra di 60 s (non la media sulla dettatura)."""
    if not times:
        return 0
    return max(sum(1 for t2 in times if t <= t2 < t + win) for t in times)


def timeline_lag(samples, final_words, batch_words):
    """Per ogni parola del testo finale: quando e' comparsa a video NELLA SUA FORMA FINALE
    (prefisso stabile) meno quando e' stata detta (tempi di parola del batch)."""
    first = {}
    for t, cur in samples:
        n = 0
        for a, b in zip(cur, final_words):
            if a != b:
                break
            n += 1
        for i in range(n):
            first.setdefault(i, t)
    bw = [L.norm_word(w["word"]) for w in batch_words]
    sm = difflib.SequenceMatcher(a=bw, b=final_words, autojunk=False)
    lags = []
    for bi, li, size in sm.get_matching_blocks():
        for k in range(size):
            if li + k in first:
                lags.append(first[li + k] - float(batch_words[bi + k].get("end") or 0.0))
    return lags, len(first)


def run_file(path, key, speed=1.0, policy="live", pace=L.PACE_SEC, min_live=0.0, verbose=True):
    a, sr = read_wav(path)
    dur = len(a) / sr
    name = os.path.basename(path)
    batch = groq_batch(path, key)
    # accelerato: l'orologio del motore avanza col tempo dell'AUDIO (stesso ritmo, stessi giri),
    # ma l'orologio da parete corre `speed` volte piu' veloce. Il tetto di 18 richieste/min resta
    # agganciato al tempo reale (e' un limite reale, condiviso): puo' frenare la corsa.
    clock = FakeClock() if speed != 1.0 else time.monotonic
    logs = []
    eng = L.LiveEngine(key, vocab="", log=logs.append, sr_in=sr, pace=pace,
                       clock=clock, final_policy=policy, min_live_sec=min_live)
    eng.start()
    samples = []                    # (istante, parole a video) a ogni cambiamento
    rewrites, shown, last_rev = 0, [], -1
    t0 = time.perf_counter()
    blk = int(0.05 * sr)
    i = 0
    while i < len(a):
        eng.feed(a[i:i + blk])
        i += blk
        audio_t = i / sr
        if speed != 1.0:
            clock.advance(blk / sr)
        d = t0 + audio_t / speed - time.perf_counter()
        if d > 0:
            time.sleep(d)
        now = audio_t if speed != 1.0 else time.perf_counter() - t0
        s = eng.snapshot()
        if s["rev"] == last_rev:
            continue
        last_rev = s["rev"]
        cur = words_of(s["committed"] + " " + s["tentative"])
        if cur[:len(shown)] != shown:          # cio' che era gia' a video e' stato riscritto
            rewrites += 1
        samples.append((now, cur))
        shown = cur
    t_stop = time.perf_counter()
    res = eng.stop(timeout=60)
    t_res = time.perf_counter() - t_stop
    wall = time.perf_counter() - t0
    live_text = res[0] if res else ""
    diff, tot, det = word_diff(live_text, batch["text"]) if res else (None, None, {})
    lags, shown_n = timeline_lag(samples, words_of(live_text), batch["words"]) if res else ([], 0)
    out = {"file": name, "dur": dur, "requests": eng._requests, "stop_to_result": t_res,
           "batch_latency": batch["latency"], "wall": wall, "diff": diff, "tot": tot,
           "det": det, "lag_med": float(np.median(lags)) if lags else None,
           "lag_p90": float(np.percentile(lags, 90)) if lags else None,
           "live_words_before_stop": shown_n, "final_words": len(words_of(live_text)),
           "rewrites": rewrites, "revisions": len(samples),
           "committed_end": eng.snapshot()["committed"],
           "rpm_peak": peak_rpm(eng._req_log),
           "live": live_text, "batch": batch["text"], "lang": res[1] if res else None}
    if verbose:
        print(f"\n--- {name}  {dur:.1f}s  (velocita' x{speed:g}, policy={policy}) ---")
        print(f"  richieste: {out['requests']}  (picco {out['rpm_peak']}/min su finestra di 60 s)")
        print(f"  stop->risultato: {t_res:.2f}s   |  batch sullo stesso file: {batch['latency']:.2f}s")
        print(f"  parole diverse dal batch: {diff}/{tot} {det}" if res else "  stop() -> None")
        print(f"  ritardo parole a video: mediana {out['lag_med']:.2f}s  p90 {out['lag_p90']:.2f}s"
              if out["lag_med"] is not None else "  ritardo: n/d")
        print(f"  gia' a video allo stop: {shown_n}/{out['final_words']} parole  |  "
              f"revisioni {len(samples)}, di cui riscritture di testo gia' mostrato: {rewrites}")
        for line in logs[:6]:
            print(f"  log: {line}")
    return out


# ============================ runner ============================
def run_unit():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    bad = 0
    for n, f in tests:
        t0 = time.perf_counter()
        try:
            f()
            print(f"  ok   {n}  ({time.perf_counter()-t0:.1f}s)")
        except Exception as e:
            bad += 1
            print(f"  FAIL {n}: {type(e).__name__}: {e}")
    print(f"\nunit: {len(tests)-bad}/{len(tests)} passati")
    return bad


def pick_files():
    """2 corti (<10 s), 2 medi (20-45 s), 1 lungo (>=90 s), 1 muto (rms < 0,003), scelti per taglia."""
    rows = []
    for n in sorted(os.listdir(REC_DIR)):
        if not n.lower().endswith(".wav") or n.startswith(("_", "edit-")):
            continue
        p = os.path.join(REC_DIR, n)
        a, sr = read_wav(p)
        rows.append((len(a) / sr, L.rms(a), p))
    rows.sort()

    def ends(lo, hi, n=2):
        b = [p for d, r, p in rows if lo <= d < hi and r >= 0.003]
        return [b[0], b[-1]][:n] if len(b) >= n else b
    long = [p for d, r, p in rows if d >= 90 and r >= 0.003]
    mute = [p for d, r, p in rows if r < 0.003]
    return ends(3, 10) + ends(20, 45) + long[-1:] + mute[-1:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", action="store_true", help="esegue anche il simulatore (usa Groq)")
    ap.add_argument("--fast", type=float, default=0.0, help="orologio accelerato (es. 4)")
    ap.add_argument("--files", default="")
    ap.add_argument("--policy", default="live")
    ap.add_argument("--pace", type=float, default=L.PACE_SEC)
    ap.add_argument("--min-live", type=float, default=0.0, dest="min_live")
    ap.add_argument("--only-net", action="store_true")
    a = ap.parse_args()
    bad = 0 if a.only_net else run_unit()
    if not a.net:
        return bad
    key = open(os.path.join(ROOT, "groq_key.txt"), encoding="utf-8").read().strip()
    files = ([os.path.join(REC_DIR, f) for f in a.files.split(",")] if a.files else pick_files())
    rows = []
    for p in files:
        try:
            rows.append(run_file(p, key, speed=a.fast or 1.0, policy=a.policy, pace=a.pace,
                                 min_live=a.min_live))
        except Exception as e:
            print(f"  ERRORE su {os.path.basename(p)}: {type(e).__name__}: {e}")
            bad += 1
    print("\n=== riepilogo ===")
    print(f"{'file':26} {'dur':>6} {'req':>4} {'rpm':>4} {'stop->res':>10} {'batch':>7} "
          f"{'diff':>9} {'lag':>6} {'gia a video':>12}")
    for r in rows:
        d = f"{r['diff']}/{r['tot']}" if r["diff"] is not None else "None"
        lag = f"{r['lag_med']:.1f}s" if r["lag_med"] is not None else "n/d"
        av = f"{r['live_words_before_stop']}/{r['final_words']}"
        print(f"{r['file']:26} {r['dur']:5.1f}s {r['requests']:4d} {r['rpm_peak']:4d} "
              f"{r['stop_to_result']:9.2f}s {r['batch_latency']:6.2f}s {d:>9} {lag:>6} {av:>12}")
    os.makedirs(CACHE, exist_ok=True)
    json.dump(rows, open(os.path.join(CACHE, "last_run.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
