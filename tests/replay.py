"""
Banco di prova: rigioca le registrazioni d'archivio (recordings/*.wav) attraverso LE STESSE
funzioni dell'app (wavetype.silence_check, wavetype.groq_transcribe, wavetype.format_text) e stampa per
ogni file durata, rms, decisione della guardia, lunghezza del grezzo (RAW) e del formattato (OUT),
latenza. Serve a vedere PRIMA di toccare il cuore della registrazione se qualcosa e' cambiato.

    .venv\\Scripts\\python.exe tests\\replay.py                      # ultime 15 registrazioni
    .venv\\Scripts\\python.exe tests\\replay.py --limit 5
    .venv\\Scripts\\python.exe tests\\replay.py --save tests/baseline.json
    ...modifica il codice...
    .venv\\Scripts\\python.exe tests\\replay.py --compare tests/baseline.json

Le risposte di Groq finiscono in tests/cache/ (chiave = hash del file + funzione + hash del
prompt): rigiocare due volte le stesse registrazioni non costa nemmeno una richiesta. Cache e
baseline stanno in .gitignore: contengono testo personale.
Limiti Groq del piano gratis condivisi tra le sessioni (20 richieste/min): tra due richieste
vere c'e' una pausa (--sleep, default 3 s) e i 429 li ritenta gia' groq_retry di wavetype.py.
"""
import argparse
import difflib
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)              # wavetype.py legge groq_key.txt / vocab.txt / skin.txt in modo relativo
sys.path.insert(0, ROOT)

import numpy as np          # noqa: E402
import wavetype                # noqa: E402  (importarlo NON avvia nulla: solo 3 letture di file)

CACHE = os.path.join(ROOT, "tests", "cache")
GAIN_MAX = 40.0             # tetto del gain in wavetype._process
GAIN_OUT = 0.10             # rms bersaglio del gain


def _sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()[:32]


def cached(kind, key, fn):
    """(valore, era_in_cache). Il valore va serializzato in JSON."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{kind}-{key}.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))["v"], True
        except Exception:
            pass
    v = fn()
    try:
        json.dump({"v": v}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        print(f"   [cache] non salvata: {e}")
    return v, False


def pregain(a):
    """L'archivio contiene l'audio DOPO il gain (wavetype._process: gain = clip(0.10/rms, 1, 40),
    applicato solo se > 1.2). Per rigiocare la guardia serve il segnale com'era: se l'rms del
    file e' sotto 0.0833 il gain era per forza al tetto di 40 (con un gain minore l'uscita
    sarebbe 0.10), quindi si divide per 40; sopra, il gain valeva <= 1.2 e il file e' gia' grezzo.
    Misurato: i 3 file di silenzio in archivio danno cosi' 0.00068 / 0.00052 / 0.00005 contro
    0.0007 / 0.0005 / 0.0001 letti in wavetype.log per le stesse tre registrazioni."""
    rms = float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
    if rms and rms < GAIN_OUT / 1.2:
        return a / GAIN_MAX, GAIN_MAX
    return a, 1.0


def words(t):
    return (t or "").split()


def ratio(a, b):
    """Somiglianza a parole tra due testi (1.0 = identici)."""
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, words(a), words(b)).ratio()


def recordings(limit, only):
    d = os.path.join(ROOT, wavetype.REC_DIR)
    if not os.path.isdir(d):
        return []
    wavs = [os.path.join(d, n) for n in os.listdir(d)
            if n.lower().endswith(".wav") and not n.startswith(("_", "edit-"))]
    if only:
        keep = {os.path.basename(x) for x in only}
        wavs = [w for w in wavs if os.path.basename(w) in keep]
    wavs.sort(key=os.path.getmtime, reverse=True)       # le piu' recenti
    return sorted(wavs[:limit])                          # poi in ordine cronologico


def run_one(path, args):
    a, sr = wavetype.read_wav(path)
    raw_audio, gain_undone = pregain(a)
    silent, rms, peak = wavetype.silence_check(raw_audio, sr)
    row = {"file": os.path.basename(path), "dur": round(len(a) / max(sr, 1), 2),
           "rms_file": round(float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0, 5),
           "rms": round(rms, 5), "peak": round(peak, 5), "gain_undone": gain_undone,
           "silent": bool(silent), "raw": "", "out": "", "lat": 0.0, "cached": True}
    if args.guard_only:
        return row
    if silent and not args.force:
        row["note"] = "guardia: silenzio, niente STT"
        return row
    lat = 0.0                       # latenza vera delle chiamate, senza le pause anti-429
    fsha = _file_sha(path)
    stt_key = _sha(fsha, "stt", wavetype.GROQ_STT_MODEL, wavetype.VOCAB)
    t0 = time.perf_counter()
    res, hit = cached("stt", stt_key, lambda: list(wavetype.groq_transcribe(path)))
    lat += time.perf_counter() - t0
    if not hit:
        row["cached"] = False
        time.sleep(args.sleep)
    text, lang = (list(res) + ["", ""])[:2]
    row["raw"] = text
    row["lang"] = lang
    if text and not args.no_format:
        fmt_key = _sha("fmt", wavetype.GROQ_LLM_MODEL, wavetype.build_prompt(text, lang))
        t0 = time.perf_counter()
        out, hit2 = cached("fmt", fmt_key, lambda: wavetype.format_text(text, lang))
        lat += time.perf_counter() - t0
        if not hit2:
            row["cached"] = False
            time.sleep(args.sleep)
        row["out"] = out
    row["lat"] = round(lat, 2)
    return row


def print_row(r):
    dec = "SILENZIO" if r["silent"] else "parlato "
    tag = "" if r["cached"] else " *"
    print(f"{r['file']:24s} {r['dur']:7.1f}s  rms {r['rms']:.5f}  picco {r['peak']:.5f}  "
          f"{dec}  RAW {len(r['raw']):5d}  OUT {len(r['out']):5d}  {r['lat']:5.2f}s{tag}")
    if r.get("note"):
        print(f"{'':24s} {r['note']}")


def compare(rows, base_path, thr):
    base = json.load(open(base_path, encoding="utf-8"))["files"]
    changed = 0
    print(f"\n--- confronto con {base_path} (soglia {thr}) ---")
    for r in rows:
        b = base.get(r["file"])
        if not b:
            print(f"{r['file']:24s} NUOVO (non nella baseline)")
            continue
        if b["silent"] != r["silent"]:
            changed += 1
            print(f"{r['file']:24s} GUARDIA CAMBIATA: {b['silent']} -> {r['silent']}")
        rr, ro = ratio(b["raw"], r["raw"]), ratio(b["out"], r["out"])
        if rr < thr or ro < thr:
            changed += 1
            print(f"{r['file']:24s} RAW {rr:.2f} OUT {ro:.2f}  <- sotto soglia")
            print(f"   prima RAW: {b['raw'][:100]!r}")
            print(f"   ora   RAW: {r['raw'][:100]!r}")
            print(f"   prima OUT: {b['out'][:100]!r}")
            print(f"   ora   OUT: {r['out'][:100]!r}")
        else:
            print(f"{r['file']:24s} RAW {rr:.2f} OUT {ro:.2f}  ok")
    miss = [n for n in base if n not in {r['file'] for r in rows}]
    if miss:
        print(f"({len(miss)} file della baseline non rigiocati stavolta)")
    print(f"--- {changed} regressioni oltre soglia su {len(rows)} file ---")
    return changed


def main():
    ap = argparse.ArgumentParser(description="Rigioca le registrazioni d'archivio nell'app.")
    ap.add_argument("--limit", type=int, default=15, help="quante registrazioni, dalle piu' recenti")
    ap.add_argument("--files", nargs="*", default=None, help="nomi di file specifici in recordings/")
    ap.add_argument("--save", metavar="baseline.json", help="salva il risultato come riferimento")
    ap.add_argument("--compare", metavar="baseline.json", help="confronta con un riferimento")
    ap.add_argument("--threshold", type=float, default=0.90, help="somiglianza minima RAW/OUT")
    ap.add_argument("--sleep", type=float, default=3.0, help="pausa tra due richieste vere a Groq")
    ap.add_argument("--no-format", action="store_true", help="solo trascrizione, niente formattazione")
    ap.add_argument("--force", action="store_true", help="trascrivi anche cio' che la guardia scarta")
    ap.add_argument("--guard-only", action="store_true",
                    help="solo la decisione della guardia: nessuna richiesta a Groq, costo zero")
    args = ap.parse_args()

    files = recordings(args.limit, args.files)
    if not files:
        print(f"nessuna registrazione in {wavetype.REC_DIR}/")
        return 1
    print(f"{len(files)} registrazioni · motore {'Groq' if wavetype.USE_GROQ else 'LOCALE'} "
          f"· guardia rms<{wavetype.SILENCE_RMS} e picco<{wavetype.SILENCE_PEAK} · * = richiesta vera")
    rows = []
    for p in files:
        try:
            r = run_one(p, args)
        except Exception as e:
            r = {"file": os.path.basename(p), "dur": 0, "rms": 0, "rms_file": 0, "peak": 0,
                 "silent": False, "raw": "", "out": "", "lat": 0.0, "cached": True,
                 "note": f"ERRORE: {e}"}
        rows.append(r)
        print_row(r)
    n_sil = sum(1 for r in rows if r["silent"])
    print(f"\ntotale {len(rows)} · scartate dalla guardia {n_sil} · parlato {len(rows) - n_sil}")
    if args.save:
        json.dump({"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "files": {r["file"]: r for r in rows}},
                  open(args.save, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"baseline salvata in {args.save} (testo personale: resta locale)")
    if args.compare:
        return 1 if compare(rows, args.compare, args.threshold) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
