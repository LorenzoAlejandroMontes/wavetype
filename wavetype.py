"""
Wavetype - press a hotkey, talk, get clean text in the active app (Windows).

Hold nothing, press WIN+CTRL once to start (high beep), press again to stop (low beep):
the audio is transcribed, cleaned up (fillers removed, punctuation, lists) and pasted
into whatever field has focus. ESC cancels at any stage.

Two engines: Groq (whisper-large-v3-turbo + gpt-oss-120b) when a key is present in
groq_key.txt, local fallback otherwise (faster-whisper + Ollama). While you speak, a
small card next to the caret shows the words as they are recognised.

Architecture: tkinter UI on the main thread (always-on-top card that never steals focus);
a worker thread polls the keys, records, transcribes and pastes, so the UI never blocks.

Code comments are in Italian - the author's language. Issues and PRs in English are welcome.
"""
import os
import re
import sys
import time
import shutil
import threading
import wave
import winsound
import json
import urllib.request
import httpx
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFilter
import ctypes
from ctypes import wintypes

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import sounddevice as sd
import win32gui
import win32con
import win32api
import win32clipboard
import win32event
import winerror
import keyboard
import math
# Motore dell'anteprima live. "local" = live_local.py (Nemotron in streaming sulla CPU: parola a
# video in 0,40 s di mediana, nessuna richiesta di rete); "groq" = live_engine.py (pezzi rimandati
# a Groq, bocciato alla prova del 19/09). Per tornare a Groq basta cambiare questa riga.
LIVE_ENGINE = "local"
try:
    if LIVE_ENGINE == "local":
        import live_local as live_engine   # stesso contratto di live_engine (docs/ENGINE-NOTES.md)
    else:
        import live_engine             # motore live (Lotto B): trascrive mentre parli
    import live_panel                  # pannello accanto al cursore (Lotto C)
    import caret as caret_mod          # dov'e' il cursore di testo
    import context as ctx_mod          # cosa c'e' scritto PRIMA del cursore
except Exception as _le:               # un modulo che non c'e' non deve impedire la dettatura
    live_engine = live_panel = caret_mod = ctx_mod = None
    _LIVE_IMPORT_ERR = _le
try:
    import edit_chips                  # i tre comandi di Edit Mode (Grammar, English, Slack)
except Exception:                      # senza: Edit con istruzione libera, come prima
    edit_chips = None
try:
    from faster_whisper import WhisperModel
except Exception as _e:
    # Il motore locale (faster_whisper/PyAV) puo' fallire l'import se una DLL nativa
    # viene bloccata da Windows (Smart App Control / criterio di controllo applicazioni).
    # Non deve impedire l'avvio: con Groq il locale serve solo da fallback offline.
    WhisperModel = None
    _WHISPER_IMPORT_ERR = _e

_singleton = None   # handle del mutex tenuto vivo per tutta la sessione

# ---- CONFIG ----
MIN_SEC = 0.3
# Nessun tetto sulla durata: si registra finché tieni premuto (limite solo la RAM).
# --- Guardia silenzio (Whisper inventa "Thank you." / "Grazie." sul silenzio) ---
# Soglie misurate su wavetype.log, 174 registrazioni con rms nel log (script una tantum):
#   28 blocchi hanno rms <= 0,0015 e il loro trascritto e' SEMPRE un'allucinazione ("Thank you."
#   11 volte, "Grazie." 6, "." 3, "Bye." 2, "Grazie a tutti." 1, "I" 1, ...); il parlato vero piu'
#   basso mai visto e' 0,0066 ("All right.", 21/08) e la mediana del parlato e' 0,045.
#   0,003 sta in mezzo con margine 2x da entrambi i lati: su quel campione scarta 28 silenzi su 28
#   e 0 dettature vere su 143.
SILENCE_RMS = 0.003           # rms dell'intera registrazione (PRIMA del gain)
SILENCE_PEAK = 0.005          # rms della finestra piu' forte: una parola breve dentro una lunga
SILENCE_WIN = 0.3             # pausa alza questo e non l'rms globale -> non viene scartata.
                              # 0,005 = 3x sopra il picco misurato sulle 3 registrazioni mute in
                              # archivio (max 0,00168) e sotto la piu' bassa parola vera vista.
SILENCE_HOP = 0.1
# Rete di sicurezza dopo la trascrizione (testo-tipo del silenzio + audio debole). Il tetto e'
# piu' alto della guardia ma resta sotto il parlato normale (mediana 0,045; il piu' basso 0,0066).
HALLU_RMS = 0.010
HALLU_PEAK = 0.040            # nessuna parola detta a voce normale sta sotto questo picco
REC_SR = 48000           # cattura a rate nativo (il resample driver a 16k corrompe l'audio)
SR = 16000
GROQ_STT_TIMEOUT = 600   # STT: generoso, scala con la durata dell'audio (registrazioni lunghe)
GROQ_LLM_TIMEOUT = 120   # formattazione/edit: solo testo, veloce ma con margine per trascritti lunghi
MODEL = "small"          # veloce (~2-3s) e adeguato sul parlato chiaro; la velocita' e' prioritaria
COMPUTE = "int8"
LANG = None              # auto-detect (IT/EN/ES): l'audio pulito a 48k lo rende affidabile
CPU_THREADS = 8                     # core fisici: piu' thread = contesa e piu' lento su questa CPU

# --- Groq (cloud gratis, veloce): STT + formattazione. Fallback locale se assente/offline. ---
def _load_groq_key():
    try:
        k = open("groq_key.txt", encoding="utf-8").read().strip()
        return k if k.startswith("gsk_") else None
    except Exception:
        return None

GROQ_KEY = _load_groq_key()
USE_GROQ = GROQ_KEY is not None
GROQ_STT_MODEL = "whisper-large-v3-turbo"
GROQ_LLM_MODEL = "openai/gpt-oss-120b"
GROQ_LLM_MAX_OUT = 8192   # tetto esplicito dei token di risposta. Senza, vale il default del
#                           modello: 3072 compresi i token di ragionamento (misurato il 22/09 su un
#                           dettato di 7 min: 3070 bruciati a ragionare, risposta tagliata a meta').
GROQ_REASONING = "low"    # gpt-oss ragiona prima di rispondere: su una pulizia di testo non serve.
#                           Stesso testo: 3070 token e 10,4 s prima, 29 token e 1,5 s dopo (22/09).
FMT_CHUNK_CHARS = 1500    # sopra tanti caratteri il testo si formatta a pezzi, tagliati a fine
#                           frase: nessun pezzo puo' avvicinarsi al tetto di risposta.
FMT_MIN_RATIO = 0.60      # parole in uscita / parole in entrata sotto cui l'LLM ha riassunto invece
#                           di pulire: li' vince il dettato grezzo. Su 155 dettature reali lunghe
#                           almeno 15 parole: mediana 0,96, quinto percentile 0,79, e l'unica sotto
#                           la soglia e' quella tagliata del 22/09 (0,58). Sul singolo pezzo il
#                           rapporto oscilla di piu' (un pezzo tutto ripetizioni scende a 0,68 pur
#                           essendo pulito bene), quindi qui la soglia sta sotto quel percentile:
#                           deve prendere il riassunto vero, non la pulizia riuscita.
FMT_RATIO_MIN_WORDS = 60  # sotto questa lunghezza il rapporto non dice niente: togliere gli
#                           intercalari da una frase corta la accorcia di molto, ed e' giusto cosi'.
FMT_PARALLEL = 4          # pezzi mandati insieme: un dettato lungo non deve costare la somma delle
#                           attese (4 pezzi in fila ~6 s, insieme ~2 s). Groq accetta 20 richieste
#                           al minuto, quindi quattro in volo non toccano il limite.

# formattazione LLM: Groq se disponibile, altrimenti Ollama locale. Toggle AI on = formatta.
USE_LLM = True           # con Groq e' veloce -> acceso di default
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
LLM_MODEL = "qwen2.5:7b"     # 7b distingue prosa-vs-lista in modo affidabile (3b no); ~3-8s su CPU
LLM_TIMEOUT = 40
LANG_NAMES = {"it": "Italiano", "en": "Inglese", "es": "Spagnolo",
              "fr": "Francese", "de": "Tedesco", "pt": "Portoghese"}
LLM_PROMPT = (
    "Ripulisci il testo dettato a voce qui sotto. Rispondi SOLO con il testo finale, scritto "
    "ESCLUSIVAMENTE in {lang}. Niente spiegazioni, niente virgolette.\n"
    "- correggi errori di trascrizione e grammatica, incluse parole sbagliate ma foneticamente "
    "simili (omofoni), usando il contesto; punteggiatura e maiuscole corrette;\n"
    "- rimuovi gli intercalari riempitivi (allora, ehm, cioè, tipo, like, este);\n"
    "- 'ok' NON è un intercalare: è una parola detta (spesso una risposta o una conferma) e va "
    "sempre tenuta, anche a inizio frase;\n"
    "- NON aggiungere MAI parole, nomi o contenuti non detti dall'utente: correggi solo ciò che è stato detto;\n"
    "- SE E SOLO SE l'utente elenca chiaramente più voci, formatta come lista: titolo opzionale con "
    "i due punti, poi ogni voce su una riga con '- '. Un discorso normale resta un paragrafo, NON una lista;\n"
    "- non tradurre, non togliere informazioni;\n"
    "- se il testo è vuoto o incomprensibile, rispondi esattamente EMPTY.\n"
    "{vocab}"
    "\nTesto:\n{text}"
)


def _load_vocab():
    try:
        return [l.strip() for l in open("vocab.txt", encoding="utf-8")
                if l.strip() and not l.lstrip().startswith("#")]
    except Exception:
        return []

VOCAB_TERMS = _load_vocab()
VOCAB = ", ".join(VOCAB_TERMS)


def build_prompt(text, lang):
    langname = LANG_NAMES.get(lang, lang or "la lingua dell'input")
    vocab = f"- preserva questi termini/nomi se compaiono: {VOCAB};\n" if VOCAB else ""
    return LLM_PROMPT.format(lang=langname, vocab=vocab, text=text)
BEAM = 1                 # beam_size=1 -> decodifica piu' veloce (dettatura, non serve beam largo)
HISTORY = "wavetype_history.txt"
WAV_OUT = "last_rec.wav"
REC_DIR = "recordings"        # archivio: ogni registrazione salvata qui, mai sovrascritta
REC_KEEP_DAYS = 7             # le registrazioni piu' vecchie di cosi' vengono cancellate
GROQ_MAX_BYTES = 20 * 1024 * 1024   # oltre questa soglia l'audio va spezzato (Groq rifiuta ~25MB)
CHUNK_SEC = 540               # durata dei pezzi quando si spezza: 9 min ~= 17MB; col margine di
                              # ricerca della pausa il pezzo piu' lungo resta sotto il tetto Groq
GROQ_TRIES = 3                # tentativi su errore di rete/5xx prima di arrendersi
REC_REMIND_SEC = 300          # promemoria sonoro: "stai ancora registrando" ogni N secondi
LOG_HEARTBEAT = False         # battito dell'HUD nel log: solo per debug (riempie il file)
CONTEXT_ON = True             # cursore a meta' di una frase gia' scritta: il dettato continua la
                              # frase (minuscola e spazio) invece di aprirne una nuova. False =
                              # come prima, sempre maiuscola
HUD_ON = False                # interruttore dell'HUD (cane/faccina/pallina). False = mai a schermo, ne' da
#                               fermo ne' all'avvio: la card e' l'unico indicatore.
#                               True lo rimette com'era. Rete di sicurezza con False: se registri o
#                               elabori e nessuna card e' a schermo (card non disponibile, fallback
#                               tkinter), l'HUD torna solo per quel tempo, mai alla cieca.
VK_ESC, VK_F13 = 0x1B, 0x7C

# --- Anteprima live (motore live_engine + pannello live_panel) ---
LIVE = True                   # False = comportamento identico a prima, in ogni ramo
# Con LIVE_ENGINE = "local" il testo incollato viene SEMPRE dal percorso di oggi (Groq batch +
# format_text): live_local.stop() torna None (live_local.py:316). L'anteprima parte dalla prima
# parola di ogni dettatura, corta o lunga, perche' non costa richieste. Le due soglie qui sotto
# valgono solo per il motore "groq".
LIVE_BATCH_UNDER = 30.0       # [groq] sotto questa durata il testo finale viene dal percorso di oggi:
#                               misurato dal Lotto B, sotto i 30 s il live non e' piu' veloce
#                               del batch (103 s: 3,85 s live contro 6,77 s batch; sotto i 10 s
#                               il batch e' gia' a 1,56 s di mediana). Il live resta anteprima.
LIVE_MIN_SEC = 10.0           # [groq] nessuna richiesta live prima di tanto parlato: sotto i 10 s
#                               l'anteprima non fa in tempo a servire e la richiesta e' regalata
#                               al limite Groq (20/min, condiviso).
LIVE_STOP_TIMEOUT = 30        # attesa massima del testo live allo stop, poi si usa il batch
LIVE_FINAL_HOLD = 0.9         # dopo l'incolla la card resta in "inserted" (testo finale, barrati
#                               da 0,6 s = live_panel.T_HOLD, conteggio parole) e poi va
LIVE_CANCEL_HOLD = 0.6        # ESC: la card mostra "cancelled" per tanto, poi esce
LIVE_OFFLINE_HOLD = 2.5       # rete giu' e niente testo: "offline" resta abbastanza da leggerlo
LIVE_RECOVERED_HOLD = 1.4     # recupero incollato: le parole compaiono tutte in una volta solo
#                               qui (nessuna anteprima prima), quindi la card resta un filo piu'
#                               di una dettatura (0,9 s) prima di chiudersi
LIVE_RECOVER_HOLD = 2.5       # recupero a vuoto (niente in archivio, testo non tornato): la card
#                               resta abbastanza da leggere cosa fare
EDIT_DONE_HOLD = 1.2          # Edit incollato: la pillola ("GRAMMAR FIXED") resta tanto, poi va
EDIT_NOTHING_HOLD = 2.5       # comando Edit detto senza selezione: "nothing selected" da leggere
LIVE_PAUSE_SEC = 3.0          # tanto silenzio (niente voce e niente parole nuove) -> "paused"
LIVE_VOICE_RMS = SILENCE_PEAK # rms di un blocco audio sopra cui e' voce: stessa soglia della guardia
#                               silenzio (sopra i silenzi misurati, sotto la parola vera piu' bassa)
LEVEL_DB_FLOOR = -60.0        # livello per la card: rms in dBFS mappato su 0..1. -60 dB = rms 0,001
LEVEL_DB_CEIL = -20.0         # (sotto i silenzi misurati, 0,00168), -20 dB = rms 0,1 (sopra i picchi
#                               del parlato nel log: 0,06-0,19 sono gia' pieno)
LOOP_IDLE = 0.055             # passo del loop principale come sempre...
LOOP_LIVE = 0.030             # ...e piu' fitto col pannello aperto (a 55 ms il barrato va a scatti)
# ----------------

model = None
ui = {"state": "idle"}          # idle | rec | proc — letto dalla UI
flags = {"quit": False, "cancel": False, "dismiss": False}
rec = {"held": False, "frames": [], "hwnd": 0, "stream": None,
       "edit": False, "sel": "", "t0": 0.0, "remind": 1, "chip": None,
       "ctx": None}   # testo che precede il cursore, letto all'inizio della dettatura
insert_jobs = []   # (hwnd, testo) da incollare sul THREAD PRINCIPALE (win32 da thread fresco -> segfault)


def log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        with open("wavetype.log", "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


def beep(freq, ms=90):
    try:
        winsound.Beep(freq, ms)
    except Exception:
        pass


def beep_silence():
    """Doppio bip grave: "non ho sentito niente, non ho incollato nulla"."""
    beep(330, 120)
    beep(247, 180)


def input_device_name():
    """Nome del microfono che Windows dara' alla prossima registrazione (default di sistema).
    Nel log a ogni avvio: se il default e' finito sulle cuffie Bluetooth spente, si vede."""
    try:
        return sd.query_devices(kind="input")["name"]
    except Exception as e:
        return f"?({e})"


def set_mic_max():
    """Porta il livello input del microfono al 100% (piu' segnale prima della cattura).
    Gira su un thread suo: COM si apre e si chiude qui, e ogni puntatore COM viene rilasciato qui
    dentro. Prima c'era cast(iface, POINTER(...)): due puntatori allo stesso oggetto con un solo
    riferimento, quindi un Release di troppo; il garbage collector lo faceva poi sul thread
    principale e l'eseguibile moriva con access violation (visto 2 avvii su 3 a cache COM vuota,
    crash.log del 23/09: Release in __del__ durante il GC)."""
    import gc
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        comtypes = None
    mic = iface = vol = None
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        mic = AudioUtilities.GetMicrophone()
        iface = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = iface.QueryInterface(IAudioEndpointVolume)      # AddRef vero, non un cast
        before = vol.GetMasterVolumeLevelScalar()
        vol.SetMasterVolumeLevelScalar(1.0, None)
        log(f"[mic] livello {before:.2f} -> 1.00")
    except Exception as e:
        log(f"[mic] impossibile alzare il livello: {e}")
    finally:
        mic = iface = vol = None
        gc.collect()                      # i cicli con dentro oggetti COM si chiudono su QUESTO thread
        if comtypes is not None:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def _down(vk):
    return bool(win32api.GetAsyncKeyState(vk) & 0x8000)


def chord_down():
    ctrl = _down(win32con.VK_CONTROL)
    win = _down(win32con.VK_LWIN) or _down(win32con.VK_RWIN)
    return ctrl and win


def swallow_win():
    # colpetto di F13 (tasto neutro) mentre Win e' premuto: impedisce l'apertura del menu Start
    win32api.keybd_event(VK_F13, 0, 0, 0)
    win32api.keybd_event(VK_F13, 0, win32con.KEYEVENTF_KEYUP, 0)


# ---------- anteprima live ----------
# Durante una dettatura le parole compaiono in un pannello accanto al cursore mentre parli; alla
# fine si incolla una volta sola, come sempre. In Edit Mode la stessa card si apre nella forma a
# tre comandi (live_panel.open_edit) e mostra l'istruzione mentre la dici. L'audio intero continua
# ad accumularsi in rec["frames"] e ad andare in archivio prima di qualunque rete: a ogni errore
# (motore o pannello) si ricade sul percorso di oggi e non si perde niente.
# Regola dura: le chiamate di finestra win32 stanno tutte nel loop principale (live_tick); il
# thread worker si limita a lasciare un ordine in live["cmds"].
live = {"engine": None, "panel": None, "on": False, "open": False, "expect_paste": False,
        "final": False, "close_at": 0.0, "close_pasted": True, "ui": False, "cmds": [],
        "phase": "", "ending": False, "level": 0.0, "voice_t": 0.0, "text_t": 0.0, "rev": None,
        "engine_err": None, "edit": False, "recover": False, "hwnd": 0}
REC_PHASES = ("listening", "live", "paused")   # fasi decise dal loop principale mentre registri


def card_ready():
    """La card si puo' aprire? (interruttore, moduli, UI alpha in piedi). Non dipende dal motore:
    e' l'unico indicatore durante la dettatura, anche se il motore non c'e'."""
    return bool(LIVE and live_panel is not None and live["ui"])


def engine_ready():
    """Il motore delle parole e' utilizzabile? Il locale non vuole la chiave Groq, quello Groq si'.
    Se il modello locale non si e' caricato, la card resta ma senza parole (dettatura di sempre)."""
    if live_engine is None or live["engine_err"] is not None:
        return False
    return LIVE_ENGINE == "local" or USE_GROQ


def live_ready():
    """Compatibilita': il pannello live ha chi lo disegna."""
    return card_ready()


def live_make_engine():
    if LIVE_ENGINE == "local":           # niente rete: la chiave Groq non gli serve e non la riceve
        return live_engine.LiveEngine(log=log, sr_in=REC_SR)
    return live_engine.LiveEngine(
        GROQ_KEY, vocab=VOCAB, log=log, sr_in=REC_SR,
        final_policy="batch_under", batch_under_sec=LIVE_BATCH_UNDER, min_live_sec=LIVE_MIN_SEC)


def live_preload():
    """All'avvio, in sottofondo: carica il modello locale (~750 MB, qualche secondo) cosi' la prima
    dettatura non aspetta. Il modello resta in cache nel processo (live_local.load_backend) e il
    motore creato alla prima dettatura lo ritrova pronto. Se fallisce: log e anteprima parole
    spenta, la dettatura e l'incolla restano quelli di sempre."""
    if not (LIVE and live_engine is not None and LIVE_ENGINE == "local"):
        return
    t0 = time.perf_counter()
    try:
        live_engine.load_backend()
        log(f"[live] modello locale pronto in {time.perf_counter() - t0:.1f}s (precaricato)")
    except Exception as e:
        live["engine_err"] = e
        log(f"[live] modello locale non caricato ({e}) — niente parole in anteprima, "
            "dettatura normale")


def level_from_rms(r):
    """rms di un blocco audio -> livello 0..1 per la card (scala in dB: il mic e' basso)."""
    if not r or r <= 0:
        return 0.0
    db = 20.0 * math.log10(r)
    return float(min(1.0, max(0.0, (db - LEVEL_DB_FLOOR) / (LEVEL_DB_CEIL - LEVEL_DB_FLOOR))))


def rec_phase(now, last_act, pause_sec=LIVE_PAUSE_SEC):
    """Fase della card mentre registri. `last_act` = ultimo segno di parlato (voce nel mic o
    parole nuove dal motore), 0 = ancora niente."""
    if not last_act:
        return "listening"
    return "paused" if now - last_act >= pause_sec else "live"


def live_phase(p):
    """Chiede alla card la fase `p` (la esegue il loop principale). Solo se la card e' della
    dettatura in corso e la fase cambia."""
    if live["open"] and p != live["phase"]:
        live["phase"] = p
        live["cmds"].append(("phase", p))


def live_start(hwnd, sel_words=None):
    """Apre la card in "listening" e avvia il motore. Non solleva mai.
    `sel_words` (numero) = Edit Mode: card a tre comandi, il motore trascrive l'istruzione."""
    live["on"] = live["open"] = live["expect_paste"] = live["ending"] = False
    live["phase"] = ""
    live["level"] = live["voice_t"] = live["text_t"] = 0.0
    live["rev"] = None
    live["edit"] = sel_words is not None
    live["recover"] = False
    live["hwnd"] = hwnd
    if not card_ready():
        return
    live["open"] = True
    if live["edit"]:
        live["cmds"].append(("open_edit", (hwnd, sel_words)))
    else:
        live["cmds"].append(("open", hwnd))
    live_phase("listening")
    if not engine_ready():
        return
    try:
        if live["engine"] is None:
            live["engine"] = live_make_engine()
        live["engine"].start()
        live["on"] = True
    except Exception as e:
        log(f"   [live] motore non parte ({e}) — card senza parole, dettatura normale")
        live["on"] = False


def live_recover_start(hwnd, dur=0.0):
    """Win+Ctrl+R: apre la card del recupero in "recovering" con la durata dell'audio.
    Niente motore: le parole arrivano in un colpo solo alla fine (live_show_final)."""
    live["on"] = live["open"] = live["expect_paste"] = live["ending"] = False
    live["phase"] = ""
    live["level"] = live["voice_t"] = live["text_t"] = 0.0
    live["rev"] = None
    live["edit"] = False
    live["recover"] = True
    live["hwnd"] = hwnd
    if not card_ready():
        return
    live["open"] = True
    live["cmds"].append(("open_recover", (hwnd, float(dur or 0.0))))
    live_phase("recovering")


def live_close(pasted):
    """Ordina la chiusura del pannello (la esegue il loop principale). Se la card sta gia'
    mostrando un esito (annullato, offline) non la si chiude prima del tempo."""
    if live["open"] and not live["ending"]:
        live["open"] = False
        live["cmds"].append(("close", bool(pasted)))


def live_stop_engine():
    """Motore fermo. Si ferma anche quando live["on"] e' gia' False: se feed() e' esploso nel
    callback audio l'interruttore si spegne li', ma il thread del motore resta vivo.
    cancel() su un motore gia' fermo non fa nulla."""
    live["on"] = False
    eng = live["engine"]
    if eng is not None:
        try:
            eng.cancel()
        except Exception as e:
            log(f"   [live] cancel: {e}")


def live_cancel():
    """Recupero, silenzio, troppo corto: motore fermo e pannello via, senza incolla."""
    live_stop_engine()
    live_close(False)


def live_end(phase, hold):
    """Fine senza incolla con un esito da leggere ("cancelled" su ESC, "offline" con la rete giu'):
    motore fermo, la card mostra la fase per `hold` secondi e poi esce."""
    live_stop_engine()
    if live["open"] and not live["ending"]:
        live_phase(phase)
        live["ending"] = True
        live["cmds"].append(("close_in", float(hold)))


def live_result(timeout=LIVE_STOP_TIMEOUT):
    """(testo grezzo, lingua) dal motore live, oppure None = usa il percorso di oggi.
    Con il motore locale e' sempre None (live_local.stop). Qualunque sia l'esito il motore resta
    fermo: se feed() era esploso a meta' dettatura l'interruttore e' gia' spento ma il thread no,
    e va chiuso qui (misurato: senza questo resta appeso fino alla dettatura successiva)."""
    on, live["on"] = live["on"], False
    eng = live["engine"]
    if eng is None:
        return None
    if not on:
        try:
            eng.cancel()
        except Exception as e:
            log(f"   [live] cancel: {e}")
        return None
    try:
        out = eng.stop(timeout)
    except Exception as e:
        log(f"   [live] stop: {e}")
        return None
    if not isinstance(out, tuple) or len(out) != 2:
        return None
    return out


def live_show_final(text):
    """Mostra nel pannello il testo definitivo: gli intercalari tolti si vedono barrati un attimo.
    Non ritarda mai l'incolla: il pannello si chiude da solo dopo (live_pasted).
    Il flag si alza QUI e non quando il loop principale esegue l'ordine: l'incolla e' accodato un
    attimo dopo e live_pasted() leggerebbe ancora False, chiudendo il pannello nello stesso frame."""
    if live["open"] and text:
        live["final"] = True
        live["cmds"].append(("final", text))


def live_pasted():
    """Incolla avvenuto (loop principale): la card passa a "inserted" (il conteggio parole lo
    ricava lei dal testo finale gia' ricevuto), resta LIVE_FINAL_HOLD e poi si chiude.
    Se intanto e' partita un'altra dettatura, il pannello a schermo e' SUO e non si tocca:
    l'incolla che sta passando e' di quella prima (misurato: senza questo il pannello della
    nuova dettatura si chiudeva mentre l'utente stava ancora parlando)."""
    if live["open"] and not rec["held"]:
        if live["edit"]:                # Edit: la card e' gia' in "done" con la pillola del chip
            live["close_pasted"] = True
            live["close_at"] = time.perf_counter() + EDIT_DONE_HOLD
            return
        live_phase("recovered" if live["recover"] else "inserted")
        live["close_pasted"] = True
        live["close_at"] = time.perf_counter() + (LIVE_RECOVERED_HOLD if live["recover"]
                                                  else LIVE_FINAL_HOLD)


def edit_result(text, chip):
    """Edit riuscito: la card passa a "done" con le parole del testo nuovo e il chip (None =
    istruzione libera, pillola REWRITTEN). L'incolla poi la chiude (live_pasted)."""
    if live["open"] and live["edit"] and not live["ending"]:
        live["cmds"].append(("result", (len(text.split()), chip)))
        live_phase("done")
        live["expect_paste"] = True


def live_nothing():
    """Comando Edit detto senza testo selezionato: la card diventa quella di Edit e dice
    "nothing selected" (con l'istruzione su come si fa), niente incolla."""
    live_stop_engine()
    if live["open"] and not live["ending"]:
        live["edit"] = True
        live["cmds"].append(("open_edit", (live["hwnd"], 0)))
        live["phase"] = "listening"
        live_end("nothing", EDIT_NOTHING_HOLD)


# ---------- tasti 1-3 in Edit Mode ----------
# Solo mentre registri un'istruzione Edit: il tasto sceglie il comando e chiude la registrazione,
# senza parlare. Il tasto viene intercettato (non arriva all'app sotto); fuori da Edit l'hook non
# esiste proprio. Aggancio e sgancio li fa il worker, un solo thread.
edit_keys = {"hooks": []}


def _chip_key(key):
    """Callback dell'hook (thread di `keyboard`): annota il chip, il worker ferma la registrazione.
    Torna False = il tasto non arriva all'app."""
    try:
        c = edit_chips.by_key(key) if edit_chips is not None else None
        if c is not None and rec["held"] and rec["edit"] and not rec["chip"]:
            rec["chip"] = c.id
            if live["open"]:
                live["cmds"].append(("chip", c.key))
    except Exception:
        pass
    return False


def edit_keys_set(on):
    """Aggancia (on) o sgancia gli hook dei tasti 1, 2, 3. Non solleva mai."""
    if on and not edit_keys["hooks"] and edit_chips is not None:
        try:
            for c in edit_chips.CHIPS:
                edit_keys["hooks"].append(
                    keyboard.on_press_key(c.key, lambda e, k=c.key: _chip_key(k), suppress=True))
        except Exception as e:
            log(f"   [edit] tasti 1-3 non disponibili: {e}")
            on = False
    if not on:
        hooks, edit_keys["hooks"] = edit_keys["hooks"], []
        for h in hooks:
            try:
                keyboard.unhook(h)
            except Exception:
                pass


def live_panel_off(e):
    """Il pannello ha sollevato: si logga una volta e la dettatura continua senza pannello."""
    log(f"   [live] pannello disattivato per questa dettatura: {e}")
    p, live["panel"] = live["panel"], None
    live["open"] = live["ending"] = False
    live["close_at"] = 0.0
    del live["cmds"][:]
    try:
        if p is not None:
            p.destroy()
    except Exception:
        pass


def live_panel_open():
    """True se c'e' un pannello a schermo (decide anche il passo del loop principale)."""
    p = live["panel"]
    try:
        return p is not None and p.is_open()
    except Exception:
        return False


def hud_want(active, recent, booting):
    """L'HUD va a schermo? Con HUD_ON spento solo come rete di sicurezza: mentre registri o elabori
    senza card (chi chiama esce prima se la card c'e'). Da fermo e all'avvio mai."""
    if not HUD_ON:
        return bool(active)
    always_vis = SKIN in ("face", "orb") and not flags["dismiss"]
    return bool(always_vis or active or recent or booting)


def live_panel_holds_hud():
    """True mentre la card live e' a schermo con il suo indicatore: l'HUD va nascosto."""
    try:
        p = live["panel"]
        return p is not None and p.holds_hud()
    except Exception:
        return False


# ---------- stile della card (Win+Ctrl+T) ----------
def next_style(cur, styles):
    """Lo stile dopo `cur` nel giro (uno sconosciuto riparte dal primo)."""
    styles = list(styles)
    if not styles:
        return cur
    return styles[(styles.index(cur) + 1) % len(styles)] if cur in styles else styles[0]


def style_cycle():
    """Win+Ctrl+T: stamp -> glyph -> signal -> stamp. Salva la scelta e la fa vedere subito
    sopra il cursore (anteprima) o, se c'e' una dettatura a schermo, sulla card stessa."""
    if live_panel is None:
        log("   [stile] card non disponibile (moduli live assenti)")
        return None
    try:
        new = next_style(live_panel.load_style(), live_panel.STYLES)
        live_panel.save_style(new)
    except Exception as e:
        log(f"   [stile] non cambiato: {e}")
        return None
    log(f"   [stile] {new}")
    if card_ready():
        live["cmds"].append(("style", new))
    return new


def _panel():
    """Il pannello (creato alla prima occorrenza, con lo stile salvato). Solo loop principale."""
    p = live["panel"]
    if p is None:
        p = live["panel"] = live_panel.LivePanel(log=log)
        p.set_style(live_panel.load_style())
    return p


def live_tick():
    """Un frame del pannello. SOLO dal loop principale: le finestre win32 vivono li'."""
    p = live["panel"]
    try:
        while live["cmds"]:
            what, arg = live["cmds"].pop(0)
            if what == "open":
                p = _panel()
                live["final"] = False
                live["close_at"] = 0.0
                p.open(arg)
            elif what == "open_edit":
                p = _panel()
                live["final"] = False
                live["close_at"] = 0.0
                p.open_edit(*arg)
            elif what == "open_recover":
                p = _panel()
                live["final"] = False
                live["close_at"] = 0.0
                p.open_recover(*arg)
            elif what == "style":
                p = _panel()
                p.set_style(arg)
                if not live["open"]:        # nessuna dettatura a schermo: anteprima sul cursore
                    p.preview_style(arg)
            elif p is None:
                continue
            elif what == "phase":
                p.set_phase(arg)
            elif what == "close":
                p.close(pasted=arg)
            elif what == "close_in":
                live["close_pasted"] = False
                live["close_at"] = time.perf_counter() + arg
            elif what == "final":
                live["final"] = True
                p.update({"committed": arg, "tentative": "", "rev": -1})
            elif what == "chip":
                p.set_chip(arg)
            elif what == "result":
                p.set_result(*arg)
        if p is None or not p.is_open():
            return
        now = time.perf_counter()
        if live["close_at"] and now >= live["close_at"]:
            live["close_at"] = 0.0
            live["open"] = live["ending"] = False
            p.close(pasted=live["close_pasted"])
        if live["on"]:
            snap = live["engine"].snapshot()
            if snap.get("rev") != live["rev"]:
                live["rev"] = snap.get("rev")
                if snap.get("committed") or snap.get("tentative"):
                    live["text_t"] = now    # parole nuove: si sta parlando
            p.update(snap)
        if live["open"]:
            held = rec["held"]
            p.set_level(live["level"] if held else 0.0)
            if held and live["phase"] in REC_PHASES and not live["ending"] and not live["edit"]:
                ph = rec_phase(now, max(live["voice_t"], live["text_t"]))
                if ph != live["phase"]:
                    live["phase"] = ph
                    p.set_phase(ph)
        p.tick()
    except Exception as e:
        live_panel_off(e)


def audio_cb(indata, frames, tinfo, status):
    if status:
        log(f"[audio] {status}")
    rec["frames"].append(indata.copy())   # nessun limite: si accumula finché si registra
    if live["open"]:                      # livello per la card: un rms per blocco, niente di piu'.
        try:                              # Il loop principale lo legge (float: scrittura atomica)
            r = float(np.sqrt(np.mean(np.square(indata, dtype="float64"))))
            live["level"] = level_from_rms(r)
            if r >= LIVE_VOICE_RMS:
                live["voice_t"] = time.perf_counter()
        except Exception:
            pass
    if live["on"]:                        # anteprima live: solo accodare, mai bloccare qui dentro
        try:
            live["engine"].feed(indata)
        except Exception:
            live["on"] = False            # niente log nel callback audio: tocca il disco


def rules_clean(text):
    """Pulizia minima a regole (fallback quando l'LLM non c'e'): trim, maiuscola, spazi."""
    t = " ".join(text.split()).strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def llm_format(text, lang):
    """Chiede a Ollama (qwen locale) di pulire+formattare nella lingua data. None se non risponde."""
    try:
        payload = {
            "model": LLM_MODEL,
            "prompt": build_prompt(text, lang),
            "stream": False,
            "options": {"temperature": 0.2},
        }
        req = urllib.request.Request(
            OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = (data.get("response") or "").strip().strip('"').strip()
        return out or None
    except Exception as e:
        log(f"   [llm] non disponibile ({e}) — uso le regole")
        return None


def lang_code(lang):
    """Groq restituisce il nome esteso ('Italian'), ma l'API accetta solo il codice ISO ('it'):
    senza questa conversione un pezzo successivo al primo riceve 400 unsupported language."""
    l = (lang or "").strip().lower()
    return {"italian": "it", "english": "en", "spanish": "es"}.get(l, l if len(l) == 2 else "")


FLAC_UPLOAD = True        # l'audio parte per Groq in FLAC: senza perdita (campioni identici, verificato
#                           andata e ritorno), pesa circa la meta' del WAV e l'upload e' il pezzo piu'
#                           lento (103 s: trascrizione da 5,1 a 2,7 s, testo identico). False = WAV.


def flac_bytes(pcm16, sr):
    """PCM 16 bit mono -> FLAC in memoria (PyAV, gia' nel venv con faster-whisper)."""
    import io
    import av
    buf = io.BytesIO()
    with av.open(buf, "w", format="flac") as c:
        st = c.add_stream("flac", rate=sr)
        st.layout = "mono"
        st.format = "s16"
        for i in range(0, len(pcm16), 4096):
            fr = av.AudioFrame.from_ndarray(pcm16[i:i + 4096].reshape(1, -1), format="s16", layout="mono")
            fr.sample_rate = sr
            fr.pts = i
            for pk in st.encode(fr):
                c.mux(pk)
        for pk in st.encode(None):
            c.mux(pk)
    return buf.getvalue()


def upload_audio(wav_path):
    """(nome, byte, mime) da mandare a Groq: FLAC se si puo', se no il WAV com'e' (PyAV bloccato,
    file strano, qualunque errore): la dettatura non si perde mai per colpa della compressione."""
    with open(wav_path, "rb") as f:
        raw = f.read()
    if FLAC_UPLOAD:
        try:
            with wave.open(wav_path, "rb") as w:
                if w.getsampwidth() != 2 or w.getnchannels() != 1:
                    raise ValueError("non e' PCM 16 bit mono")
                sr = w.getframerate()
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            blob = flac_bytes(pcm, sr)
            if blob and len(blob) < len(raw):
                return "a.flac", blob, "audio/flac"
        except Exception as e:
            log(f"   [flac] {e} — mando il WAV")
    return "a.wav", raw, "audio/wav"


def groq_transcribe(wav_path, force_lang=None, context=""):
    """STT via Groq (whisper-large-v3-turbo). Ritorna (testo, lingua).
    `context` = coda del pezzo precedente, quando l'audio e' spezzato."""
    data = {"model": GROQ_STT_MODEL, "response_format": "verbose_json"}
    bits = [b for b in (VOCAB, context) if b]
    if bits:
        data["prompt"] = " ".join(bits)  # bias di spelling verso nomi/gergo (+ contesto)
    code = lang_code(force_lang)
    if code:
        data["language"] = code
    r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                   headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=GROQ_STT_TIMEOUT,
                   files={"file": upload_audio(wav_path)}, data=data)
    r.raise_for_status()
    j = r.json()
    return (j.get("text") or "").strip(), j.get("language", "")


def groq_format(text, lang):
    """Formattazione via Groq (gpt-oss-120b). Ritorna (testo, tagliato): `tagliato` e' vero quando
    la risposta si e' fermata contro il tetto dei token, cioe' finisce a meta'."""
    r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=GROQ_LLM_TIMEOUT,
                   json={"model": GROQ_LLM_MODEL, "temperature": 0.1,
                         "max_completion_tokens": GROQ_LLM_MAX_OUT,
                         "reasoning_effort": GROQ_REASONING,
                         "messages": [{"role": "user", "content": build_prompt(text, lang)}]})
    r.raise_for_status()
    ch = r.json()["choices"][0]
    return (ch["message"]["content"] or "").strip(), ch.get("finish_reason") == "length"


def fmt_blocks(text, limit=FMT_CHUNK_CHARS):
    """Il testo a pezzi non piu' lunghi di `limit`, tagliati dopo un punto (o, se una frase e' piu'
    lunga del limite, dopo uno spazio): un pezzo corto non puo' esaurire il tetto di risposta."""
    t = text.strip()
    if len(t) <= limit:
        return [t] if t else []
    out, rest = [], t
    while len(rest) > limit:
        win = rest[:limit]
        cut = max(win.rfind(". "), win.rfind("! "), win.rfind("? "), win.rfind("\n"))
        cut = cut + 1 if cut > limit // 3 else win.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return [b for b in out if b]


def shrunk(src, out):
    """Vero se la pulizia ha accorciato tanto da aver riassunto: non e' piu' quello che ha detto."""
    w = len(src.split())
    return w >= FMT_RATIO_MIN_WORDS and len(out.split()) < FMT_MIN_RATIO * w


def fmt_one(b, lang, tag):
    """Un pezzo ripulito, o None se la risposta non e' fidata (tagliata dal tetto token, riassunta,
    errore). None qui non perde niente: chi chiama tiene il pezzo come lui l'ha detto."""
    try:
        out, cut = groq_retry(lambda: groq_format(b, lang), "fmt")
    except Exception as e:
        log(f"   [groq fmt] {tag} {e}")
        return None
    if cut:
        log(f"   [fmt] {tag} risposta tagliata dal tetto token: tengo il dettato")
        return None
    if out.strip().upper() == "EMPTY":
        return ""                         # pezzo senza parlato vero: sparisce, il resto vale
    if shrunk(b, out):
        log(f"   [fmt] {tag} riassunto ({len(b.split())} parole -> {len(out.split())}): "
            f"tengo il dettato")
        return None
    return out


def groq_format_all(text, lang):
    """Testo pulito e COMPLETO, o None se Groq e' muto. Un dettato lungo si spezza a fine frase e i
    pezzi partono insieme; il pezzo che torna male resta come l'ha detto lui. Mai una meta' scritta
    bene al posto del tutto (22/09: sette minuti tornati a meta', persi)."""
    parts = fmt_blocks(text)
    if not parts:
        return None
    if len(parts) == 1:
        return fmt_one(parts[0], lang, "")
    log(f"   [fmt] testo lungo ({len(text)} caratteri): {len(parts)} pezzi insieme")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=FMT_PARALLEL) as ex:
        outs = list(ex.map(lambda ib: fmt_one(ib[1], lang, f"pezzo {ib[0]}/{len(parts)}"),
                           list(enumerate(parts, 1))))
    if all(o is None for o in outs):
        return None                       # Groq muto su tutto: prova il locale, poi le regole
    keep, raw_kept = [], 0
    for b, o in zip(parts, outs):
        if o is None:                     # pezzo non fidato: le parole restano, la pulizia no
            keep.append(rules_clean(b))
            raw_kept += 1
        elif o:
            keep.append(o)
    if raw_kept:
        log(f"   [fmt] {raw_kept} pezzo/i su {len(parts)} incollati come detti")
        fmt_warn()
    if not keep:
        return "EMPTY"
    sep = chr(10) if any(chr(10) in k for k in keep) else " "
    return sep.join(keep)


def fmt_warn():
    """Due note calanti: il testo e' tutto li', ma incollato come detto, non ripulito."""
    threading.Thread(target=lambda: (beep(700, 80), beep(500, 130)), daemon=True).start()


def format_text(text, lang):
    if USE_LLM:
        if USE_GROQ:
            out = groq_format_all(text, lang)
            if out:
                return out
        out = llm_format(text, lang)
        if out and not shrunk(text, out):
            return out
        if out:
            log(f"   [llm] riassunto ({len(text.split())} parole -> {len(out.split())}): scartato")
        log("   [fmt] incollo il dettato com'e': tutte le parole ci sono, la pulizia no")
        fmt_warn()
    return rules_clean(text)


EDIT_PROMPT = (
    "Sei un editor di testo madrelingua. Ricevi un TESTO e un'ISTRUZIONE detta a voce. Applica "
    "l'istruzione al testo e rispondi SOLO col testo risultante, senza spiegazioni né virgolette.\n"
    "Se l'istruzione è una TRADUZIONE, segui queste regole:\n"
    "- NON tradurre parola per parola: rendi significato, intento, tono e sfumature dell'originale;\n"
    "- il risultato deve suonare COMPLETAMENTE NATURALE a un madrelingua della lingua d'arrivo, come "
    "se fosse stato scritto originariamente in quella lingua;\n"
    "- usa grammatica, espressioni e SINTASSI naturali della lingua d'arrivo; non forzare la struttura "
    "della frase originale;\n"
    "- adatta modi di dire, phrasal verb ed espressioni colloquiali a un equivalente naturale;\n"
    "- mantieni il registro (informale, professionale, ironico, emotivo...);\n"
    "- NON aggiungere informazioni, contesto o dettagli non presenti nell'originale;\n"
    "- a parità di rese, scegli la più naturale e semplice;\n"
    "- prima di rispondere chiediti: «un madrelingua direbbe davvero così?». Se no, riscrivi.\n"
    "Se l'istruzione NON è una traduzione, applicala normalmente mantenendo la lingua del testo.\n"
    "TESTO:\n\"\"\"{sel}\"\"\"\n\nISTRUZIONE:\n\"\"\"{instr}\"\"\""
)


def groq_edit(sel, instr):
    r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                   headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=GROQ_LLM_TIMEOUT,
                   json={"model": GROQ_LLM_MODEL, "temperature": 0.2,
                         "max_completion_tokens": GROQ_LLM_MAX_OUT,
                         "reasoning_effort": GROQ_REASONING,
                         "messages": [{"role": "user", "content": EDIT_PROMPT.format(sel=sel, instr=instr)}]})
    r.raise_for_status()
    ch = r.json()["choices"][0]
    if ch.get("finish_reason") == "length":   # meta' testo al posto della selezione = testo perso
        raise RuntimeError("risposta tagliata dal tetto token")
    return (ch["message"]["content"] or "").strip()


def edit_transform(sel, instr):
    """Il testo riscritto, o None se ne' Groq ne' il modello locale hanno risposto."""
    if USE_GROQ:
        try:
            return groq_retry(lambda: groq_edit(sel, instr), "edit")
        except Exception as e:
            log(f"   [groq edit] errore: {e}")
    try:
        payload = {"model": LLM_MODEL, "prompt": EDIT_PROMPT.format(sel=sel, instr=instr),
                   "stream": False, "options": {"temperature": 0.2}}
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            return (json.loads(r.read().decode("utf-8")).get("response") or "").strip()
    except Exception as e:
        log(f"   [edit locale] errore: {e}")
        return None                 # fallito: chi chiama NON rincolla la selezione facendo finta di nulla


def window_peak_rms(a, sr, win=SILENCE_WIN, hop=SILENCE_HOP):
    """rms della finestra da `win` secondi piu' forte della registrazione.
    Serve perche' l'rms dell'intera registrazione annacqua una parola breve dentro una lunga
    pausa: 'ok' di 0,3s dentro 60s di silenzio abbassa l'rms globale sotto la soglia."""
    h = max(1, int(hop * sr))
    k = max(1, int(round(win / hop)))          # quanti passi stanno in una finestra
    n = (len(a) // h) * h
    if n < h * k:                              # registrazione piu' corta di una finestra
        return float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
    # energia di ogni passo, a blocchi: su una registrazione da un'ora un cumsum float64
    # sull'intero array sarebbe oltre un giga di RAM
    parts, step = [], h * 1024
    for i in range(0, n, step):
        blk = a[i:min(i + step, n)].astype("float64")
        parts.append((blk * blk).reshape(-1, h).sum(axis=1))
    s = np.concatenate(parts)
    c = np.concatenate([[0.0], np.cumsum(s)])
    return float(np.sqrt(np.max(c[k:] - c[:-k]) / (k * h)))


def silence_check(a, sr):
    """(silenzio?, rms, picco) sull'audio GREZZO (prima del gain).
    Silenzio = rms globale sotto soglia E anche la finestra piu' forte sotto soglia: se una
    delle due e' alta c'e' voce e si trascrive. Volutamente prudente: meglio una dettatura
    inutile incollata che una parola vera buttata."""
    if len(a) == 0:
        return True, 0.0, 0.0
    rms = float(np.sqrt(np.mean(a ** 2)))
    peak = window_peak_rms(a, sr)
    return (rms < SILENCE_RMS and peak < SILENCE_PEAK), rms, peak


# Frasi che Whisper inventa quando non sente nulla (osservate in wavetype.log + le classiche dei
# titoli di coda). Bloccano solo se l'audio e' anche debole: "Grazie" detto davvero passa.
HALLUCINATIONS = {
    "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "thanks for watching!", "bye", "bye bye", "goodbye",
    "grazie", "grazie a tutti", "grazie mille", "grazie per la visione",
    "grazie per l'attenzione", "ciao a tutti", "i", "you", "so",
    "sottotitoli e revisione a cura di qtss",
    "sottotitoli creati dalla comunita amara.org",
}


def looks_hallucinated(text, rms, peak):
    """Rete di sicurezza dopo la trascrizione: testo identico a una frase-tipo del silenzio E
    audio debole. L'energia deve essere BASSA: se hai parlato forte, quello che hai detto passa."""
    t = " ".join((text or "").lower().split()).strip(" .!?…\"'-")
    t = t.replace("à", "a").replace("è", "e").replace("é", "e").replace("ì", "i").replace("ò", "o").replace("ù", "u")
    if not t or t not in HALLUCINATIONS:
        return False
    return rms < HALLU_RMS and peak < HALLU_PEAK


def downsample_48k_to_16k(a):
    """48k -> 16k (fattore 3) con media a blocchi = anti-alias semplice, senza dipendenze.
    Whisper lavora comunque a 16k: il file resta 1/3, niente tetto dimensione lato Groq."""
    n = (len(a) // 3) * 3
    if n == 0:
        return a.astype("float32")
    return a[:n].reshape(-1, 3).mean(axis=1).astype("float32")


def save_wav(path, a, sr):
    pcm = (np.clip(a, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def read_wav(path):
    """Legge un WAV mono 16 bit -> (array float32, sample rate)."""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32767.0
    return a, sr


EDIT_PREFIX = "edit-"              # audio di un'istruzione Edit: in archivio, ma non e' una dettatura


def archive_path(edit=False):
    """Percorso del nuovo file d'archivio (nome = data e ora, mai sovrascritto). L'istruzione di un
    Edit prende il prefisso EDIT_PREFIX: Win+Ctrl+R la salta, se no la incollerebbe come testo."""
    os.makedirs(REC_DIR, exist_ok=True)
    return os.path.join(REC_DIR, (EDIT_PREFIX if edit else "") + time.strftime("%Y%m%d-%H%M%S") + ".wav")


def prune_archive():
    """Cancella dall'archivio le registrazioni piu' vecchie di REC_KEEP_DAYS giorni."""
    if not os.path.isdir(REC_DIR):
        return
    limit = time.time() - REC_KEEP_DAYS * 86400
    try:
        for n in os.listdir(REC_DIR):
            p = os.path.join(REC_DIR, n)
            if n.lower().endswith(".wav") and os.path.getmtime(p) < limit:
                os.remove(p)
    except Exception as e:
        log(f"   [archivio] pulizia: {e}")


def last_recording():
    """L'ultima DETTATURA in archivio (esclusi i pezzi temporanei e le istruzioni Edit), o None."""
    if not os.path.isdir(REC_DIR):
        return None
    try:
        wavs = [os.path.join(REC_DIR, n) for n in os.listdir(REC_DIR)
                if n.lower().endswith(".wav") and not n.startswith(("_", EDIT_PREFIX))]
        return max(wavs, key=os.path.getmtime) if wavs else None
    except Exception:
        return None


def _retryable(e):
    """True se l'errore vale un altro tentativo: rete caduta, timeout, 429 o 5xx."""
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code == 429 or e.response.status_code >= 500
    return isinstance(e, (httpx.TransportError, OSError))


def _retry_after(e, default):
    """L'attesa chiesta da Groq quando manda 429 (header `retry-after`, secondi), o quella di
    default. Aspettare meno del dovuto vuol dire spendere i tentativi a vuoto: misurato il 22/09,
    il secchio dei token si ricarica in ~14 s, i tentativi ciechi duravano 0,6 e 1,8 s."""
    try:
        ra = float(e.response.headers.get("retry-after", ""))
        return max(default, min(ra + 0.3, GROQ_LLM_TIMEOUT / 2))
    except Exception:
        return default


def groq_retry(fn, what):
    """Esegue fn() ritentando gli errori transitori: la rete che cade non deve costare la dettatura."""
    for i in range(GROQ_TRIES):
        if flags["cancel"]:
            raise RuntimeError("annullato")
        try:
            return fn()
        except Exception as e:
            if not _retryable(e) or i == GROQ_TRIES - 1:
                raise
            wait = _retry_after(e, 0.6 * (3 ** i))
            log(f"   [groq {what}] tentativo {i + 1}/{GROQ_TRIES} fallito ({e}) — riprovo tra {wait:.1f}s")
            time.sleep(wait)


def split_points(a, sr, chunk_sec=CHUNK_SEC, search_sec=15.0):
    """Punti di taglio ogni ~chunk_sec, spostati sul momento piu' silenzioso entro +-search_sec:
    cosi' il taglio cade in una pausa e non a meta' di una parola."""
    step, search, win = int(chunk_sec * sr), int(search_sec * sr), int(0.2 * sr)
    cuts, pos = [], step
    # coda corta ammessa (>30s): meglio un pezzo piccolo che uno oltre il tetto accettato da Groq
    while len(a) - pos > 30 * sr:
        lo, hi = max(0, pos - search), min(len(a) - win, pos + search)
        cut = pos
        if hi > lo:
            c = np.cumsum(np.abs(a[lo:hi + win]))
            energy = c[win:] - c[:-win]          # energia di ogni finestra da 0.2s
            cut = lo + int(np.argmin(energy)) + win // 2
        cuts.append(cut)
        pos = cut + step
    return cuts


def groq_transcribe_long(a, sr, force_lang=None, chunk_sec=CHUNK_SEC):
    """Audio troppo grande per una richiesta sola: lo spezza sulle pause, trascrive i pezzi in
    ordine e ricuce. Ogni pezzo riceve la coda del precedente come contesto (frasi continue)."""
    bounds = [0] + split_points(a, sr, chunk_sec=chunk_sec) + [len(a)]
    n = len(bounds) - 1
    os.makedirs(REC_DIR, exist_ok=True)
    tmp = os.path.join(REC_DIR, "_chunk.wav")
    parts, lang, tail = [], force_lang or "", ""
    log(f"   [groq stt] audio grande ({len(a) / sr / 60:.1f} min): lo spezzo in {n} pezzi")
    for i in range(n):
        if flags["cancel"]:
            break
        save_wav(tmp, a[bounds[i]:bounds[i + 1]], sr)
        txt, lg = groq_retry(lambda: groq_transcribe(tmp, force_lang=lang or None, context=tail),
                             f"stt {i + 1}/{n}")
        lang = lang or lg
        if txt:
            parts.append(txt)
            tail = " ".join(txt.split()[-40:])   # coda = contesto per il pezzo successivo
        log(f"   [groq stt] pezzo {i + 1}/{n}: {len(txt)} char")
    try:
        os.remove(tmp)
    except Exception:
        pass
    return " ".join(parts).strip(), lang


def get_clipboard_text():
    try:
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        except Exception:
            return None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def set_clipboard_text(text):
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    win32clipboard.CloseClipboard()


def insert_text(hwnd, text):
    saved = get_clipboard_text()
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.05)
    set_clipboard_text(text)
    time.sleep(0.03)
    keyboard.send("ctrl+v")
    time.sleep(0.20)
    if saved is not None:
        try:
            set_clipboard_text(saved)
        except Exception:
            pass


def read_context(hwnd, token):
    """Cosa c'e' scritto prima del cursore, letto mentre parli (mai nel percorso dell'incolla)."""
    try:
        prev = ctx_mod.before_caret(hwnd)
    except Exception as e:
        log(f"   [ctx] non letto: {e}")
        return
    if rec["t0"] != token:             # nel frattempo e' partita un'altra dettatura: non e' mia
        return
    rec["ctx"] = prev
    if prev is not None and not ctx_mod.starts_sentence(prev):
        log(f"   [ctx] cursore a meta' frase, dopo: '{prev[-30:]}'")


def start_rec(edit=False, sel=""):
    rec["held"] = True
    rec["edit"] = edit
    rec["sel"] = sel
    flags["cancel"] = False
    rec["frames"] = []
    rec["t0"] = time.perf_counter()
    rec["remind"] = 1                  # promemoria sonoro: il prossimo scatta a REC_REMIND_SEC
    rec["chip"] = None                 # Edit: comando scelto col tasto 1-3 (None = lo dici)
    if not edit:                       # in edit mode l'hwnd e la selezione li ha gia' presi start_edit
        rec["hwnd"] = win32gui.GetForegroundWindow()
    ui["state"] = "rec"
    # prima dello stream: il motore vede l'audio dal primo blocco. In Edit la card a tre comandi
    # porta il numero di parole della selezione e scrive l'istruzione mentre la dici.
    live_start(rec["hwnd"], len(sel.split()) if edit else None)
    if not live["open"]:               # l'HUD torna (anche se nascosto con ESC) solo quando e' lui
        flags["dismiss"] = False       # l'indicatore: con la card a schermo resta dov'era
    rec["stream"] = sd.InputStream(samplerate=REC_SR, channels=1, dtype="float32", callback=audio_cb)
    rec["stream"].start()
    rec["ctx"] = None
    if CONTEXT_ON and ctx_mod is not None and not edit:
        threading.Thread(target=read_context, args=(rec["hwnd"], rec["t0"]), daemon=True).start()
    tag = "EDIT" if edit else "REC"
    log(f"\n=== {tag} — target: '{win32gui.GetWindowText(rec['hwnd'])}' | "
        f"mic: '{input_device_name()}' ===")


def copy_selection():
    """Copia il testo selezionato (Ctrl+C) e lo restituisce, ripristinando la clipboard."""
    saved = get_clipboard_text()
    try:
        set_clipboard_text("")
    except Exception:
        pass
    keyboard.send("ctrl+c")
    time.sleep(0.12)
    sel = (get_clipboard_text() or "").strip()
    if saved is not None:
        try:
            set_clipboard_text(saved)
        except Exception:
            pass
    return sel


VK_Q, VK_R, VK_T = 0x51, 0x52, 0x54
CHORD_KEYS = {"Q": VK_Q, "R": VK_R, "T": VK_T}   # Win+Ctrl+lettera = comando, non dettatura


def _letter_down(watch):
    for k, vk in (watch or {}).items():
        if _down(vk):
            return k
    return None


def _wait_modifiers_up(timeout=0.7, watch=None):
    """Aspetta che Win/Ctrl/Alt siano rilasciati (per un Ctrl+C non sporcato dai modificatori).
    Con `watch` ({lettera: vk}) si ferma e ritorna la lettera appena ne vede una premuta: era
    Win+Ctrl+lettera, un comando. Senza, ritorna None come sempre."""
    t = time.perf_counter()
    while (_down(win32con.VK_CONTROL) or _down(win32con.VK_LWIN) or _down(win32con.VK_RWIN)
           or _down(win32con.VK_MENU)) and time.perf_counter() - t < timeout:
        k = _letter_down(watch)
        if k:
            return k
        time.sleep(0.02)
    return _letter_down(watch)


def start_capture():
    """Win+Ctrl: se c'è testo selezionato -> Edit Mode (trasforma la selezione); altrimenti dettatura.
    Ritorna la lettera se nel frattempo e' arrivata Q/R/T (il comando lo esegue il worker) e in
    quel caso non registra nulla: prima la dettatura partiva, apriva la card e andava annullata."""
    hwnd = win32gui.GetForegroundWindow()
    k = _wait_modifiers_up(watch=CHORD_KEYS)
    if k:
        return k
    sel = copy_selection()
    rec["hwnd"] = hwnd
    if sel:
        log(f"   [edit] selezione ({len(sel)} char) — di' l'istruzione")
        start_rec(edit=True, sel=sel)
    else:
        start_rec(edit=False)
    return None


def stop_and_process():
    """Ferma la registrazione e lancia l'elaborazione su un thread separato (ESC resta reattivo)."""
    rec["held"] = False
    try:
        rec["stream"].stop()
        rec["stream"].close()
    except Exception:
        pass
    ui["state"] = "proc"
    live_phase("rewriting" if rec["edit"] else "formatting")   # dallo stop all'incolla
    threading.Thread(target=_process, args=(rec["frames"], rec["hwnd"], rec["edit"], rec["sel"],
                                            rec["chip"], rec["ctx"]), daemon=True).start()


_stt_net = threading.local()   # per thread: l'ultima stt() e' rimasta senza testo per la rete?


def stt_net_failed():
    """True se l'ultima stt() di QUESTO thread non ha testo perche' Groq e' fallito (rete, 5xx,
    429 dopo i ritentativi) e il locale non l'ha sostituito: la card mostra "offline"."""
    return bool(getattr(_stt_net, "failed", False))


DERAIL_CHUNK_SEC = 60    # pezzi corti per il secondo tentativo quando la trascrizione deraglia
DERAIL_MIN_RUN = 60      # parole di fila senza punteggiatura sotto cui non vale la pena sospettare
# parole italiane che esistono solo con l'accento: se compaiono tronche, il decoder sta perdendo pezzi
DERAIL_TRONCHE = re.compile(r"(?<![\w'’])(pu|pi|perch|gi|cos|citt|qualit|priorit|realt|verit|"
                            r"universit|attivit|possibilit|necessit|libert|societ|sar|avr|potr|dovr|"
                            r"vorr|sapr|andr|verr|met|cio)(?![\w'’])")
DERAIL_PUNCT = re.compile(r"[.,;:!?]")


def derail_score(text, min_run=DERAIL_MIN_RUN):
    """Whisper, su un audio lungo e parlato di fila, ogni tanto perde il filo: smette di mettere
    punteggiatura e insieme lascia cadere accenti e parole corte ("pu portare", "perch va").
    L'impronta e' la coppia: una corsa lunga di parole senza punteggiatura CON dentro parole
    italiane tronche. Ritorna le parole della corsa peggiore, 0 se il testo e' sano.
    Misurato su 296 dettature reali (22/09): scatta su 4, tutte e quattro vere."""
    ws, start, worst = text.split(), 0, 0
    for i, w in enumerate(ws + ["."]):
        if DERAIL_PUNCT.search(w):
            if i - start >= min_run and DERAIL_TRONCHE.search(" ".join(ws[start:i])):
                worst = max(worst, i - start)
            start = i + 1
    return worst


def stt(wav_path, a16, t0):
    """Trascrizione: Groq (con ritentativi, e a pezzi se l'audio e' grande), poi locale offline."""
    _stt_net.failed = False
    text, lang = "", LANG
    groq_err = False
    big = len(a16) * 2 > GROQ_MAX_BYTES        # 16 bit per campione -> byte del WAV
    if USE_GROQ:
        try:
            if big:
                text, lang = groq_transcribe_long(a16, SR)
            else:
                text, lang = groq_retry(lambda: groq_transcribe(wav_path), "stt")
            if lang and lang.lower() not in ("it", "en", "es", "italian", "english", "spanish"):
                log(f"   [lang] '{lang}' fuori da IT/EN/ES -> riprovo forzando italiano")
                if big:
                    text, lang = groq_transcribe_long(a16, SR, force_lang="it")
                else:
                    text, lang = groq_retry(lambda: groq_transcribe(wav_path, force_lang="it"), "stt")
            n = derail_score(text)
            if n and not flags["cancel"]:
                log(f"   [deragliata] {n} parole di fila senza punteggiatura e con accenti caduti: "
                    f"rifaccio a pezzi da {DERAIL_CHUNK_SEC}s")
                t2, l2 = groq_transcribe_long(a16, SR, force_lang=lang or None,
                                              chunk_sec=DERAIL_CHUNK_SEC)
                n2 = derail_score(t2)
                if t2 and not flags["cancel"] and n2 < n:
                    text, lang = t2, (lang or l2)
                    log(f"   [deragliata] recuperata: {n} -> {n2 if n2 else 'pulita'}")
                else:
                    log(f"   [deragliata] il secondo tentativo non e' meglio ({n2}): tengo il primo")
            log(f"   [groq/{lang}] {time.perf_counter()-t0:.2f}s | '{text}'")
        except Exception as e:
            groq_err = True
            log(f"   [groq stt] fallback locale: {e}")
    if not text and model is not None:
        segs, info = model.transcribe(wav_path, language=LANG, vad_filter=True, beam_size=BEAM,
                                      without_timestamps=True, condition_on_previous_text=False)
        text = " ".join(s.text for s in segs).strip()
        lang = info.language
        log(f"   [{MODEL}/{lang}] {time.perf_counter()-t0:.2f}s | '{text}'")
    elif not text:
        log("   [stt] nessuna trascrizione: Groq ko e motore locale non disponibile. "
            f"L'audio resta in {REC_DIR}: Win+Ctrl+R per riprovare.")
    _stt_net.failed = groq_err and not text
    return text, lang


def edit_instruction(text, chip=None):
    """(istruzione per EDIT_PROMPT, id del chip o None). Chip dal tasto, o riconosciuto nella
    frase detta ("correggi la grammatica" = Grammar): allora passa il testo del chip, se no la
    frase detta cosi' com'e' (istruzione libera)."""
    cid = chip
    if cid is None and edit_chips is not None:
        cid = edit_chips.match(text)
    instr = edit_chips.instruction(cid) if (cid and edit_chips is not None) else None
    return (instr or text), cid


def _finish(text, lang, hwnd, edit, sel, t0, chip=None, ctx=None):
    """Dal trascritto al testo incollato: edit mode, oppure pulizia+formattazione e storico."""
    if edit:                                    # EDIT MODE: text = istruzione vocale
        instr, cid = edit_instruction(text, chip)
        log(f"   [edit] istruzione: '{text}'" + (f" -> comando {cid}" if cid else ""))
        result = edit_transform(sel, instr)
        if flags["cancel"]:
            return
        if not result or not result.strip():    # Groq e locale falliti: la selezione resta com'e'
            log("   [edit] non riuscito: selezione non toccata. Riprova con Win+Ctrl sul testo selezionato.")
            if not rec["held"]:
                live_end("offline", LIVE_OFFLINE_HOLD)
            beep(400, 140)
            return
        if not rec["held"]:                     # una dettatura nuova ha gia' la card: non toccarla
            edit_result(result, cid)
        insert_jobs.append((hwnd, result))      # incolla sul thread principale
        log(f"   [edit] pronto ({time.perf_counter()-t0:.2f}s): '{result[:60]}'")
        return

    if edit_chips is not None and edit_chips.match(text):
        # "correggi la grammatica" detto senza testo selezionato: e' un comando Edit, non una
        # dettatura (0 su 223 dettature vere dello storico lo sono, misurato il 19/09)
        log(f"   [edit] comando '{text}' senza testo selezionato: non incollo")
        if not rec["held"]:
            live_nothing()
        beep(400, 140)
        return

    final = format_text(text, lang)  # Groq (o locale/regole) per pulizia+formattazione
    if flags["cancel"]:
        log("   [ESC] annullato, non incollo")
        return
    if final.strip().upper() == "EMPTY" or not final.strip():
        log("   [insert] EMPTY / vuoto, non incollo")
        return
    if final != text:
        log(f"   [fmt] '{final}'")
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{lang}\t"
                f"RAW:{text}\tOUT:{final.replace(chr(10), '\\n')}\n")
    out = final
    if CONTEXT_ON and ctx_mod is not None and ctx is not None:
        try:
            out = ctx_mod.fit(ctx, final, VOCAB_TERMS)
            if out != final:
                log(f"   [ctx] continua la frase: '{out[:40]}'")
        except Exception as e:
            log(f"   [ctx] non applicato: {e}")
    if not rec["held"]:                         # se e' gia' partita la dettatura dopo, il pannello
        live_show_final(out)                    # e il motore sono suoi: questa non glieli prende
        live["expect_paste"] = live["open"]     # l'incolla chiudera' il pannello (live_pasted)
    insert_jobs.append((hwnd, out))             # incolla sul thread principale (no segfault)
    log(f"   [insert] accodato ({time.perf_counter()-t0:.2f}s)")


def _process(frames, hwnd, edit=False, sel="", chip=None, ctx=None):
    t0 = time.perf_counter()
    try:
        if flags["cancel"]:
            return
        if edit and chip:               # Edit col tasto 1-3: niente da trascrivere, il comando c'e'
            live_stop_engine()
            log(f"   [edit] comando {chip} dal tasto")
            _finish("", LANG, hwnd, True, sel, t0, chip)
            return
        if not frames:
            return
        raw = np.concatenate(frames).reshape(-1)
        dur = len(raw) / REC_SR
        if dur < MIN_SEC:
            log(f"   [skip] {dur:.2f}s troppo corto")
            return
        silent, rms, peak = silence_check(raw, REC_SR)   # deciso sull'audio grezzo, prima del gain
        gain = float(np.clip(0.10 / rms, 1.0, 40.0)) if rms > 1e-5 else 1.0
        if gain > 1.2:
            raw = np.clip(raw * gain, -1.0, 1.0)
        log(f"   audio {dur:.1f}s @ {REC_SR}Hz | rms {rms:.4f} gain x{gain:.1f} picco {peak:.4f}")
        a16 = downsample_48k_to_16k(raw)        # 16k: file 1/3, nessun tetto durata
        path = WAV_OUT
        try:
            path = archive_path(edit)           # archivio: non si sovrascrive, si puo' recuperare
            save_wav(path, a16, SR)
            shutil.copyfile(path, WAV_OUT)
            prune_archive()
        except Exception as e:
            log(f"   [archivio] {e} — uso solo {WAV_OUT}")
            try:
                save_wav(WAV_OUT, a16, SR)
                path = WAV_OUT
            except Exception as e2:
                log(f"   [wav] impossibile salvare l'audio ({e2}) — mi fermo")
                return
        if flags["cancel"]:
            return
        if silent:      # solo rumore di fondo: Whisper qui inventa "Thank you." e lo incolla
            live_cancel()
            log(f"   [silenzio] rms {rms:.5f} < {SILENCE_RMS} e picco {peak:.5f} < {SILENCE_PEAK}: "
                f"non trascrivo e non incollo. Mic '{input_device_name()}'. Audio salvato in "
                f"{os.path.basename(path)}: Win+Ctrl+R lo rielabora comunque.")
            beep_silence()
            return

        got = live_result()             # testo gia' trascritto durante la dettatura, o None
        if got is None:
            text, lang = stt(path, a16, t0)
            if not text and stt_net_failed():   # rete giu': audio in archivio, la card lo dice
                live_end("offline", LIVE_OFFLINE_HOLD)
        else:
            text, lang = got
            if not text.strip():        # il motore live ha sentito solo silenzio
                log(f"   [live] nessun parlato riconosciuto: non incollo. Audio in "
                    f"{os.path.basename(path)}: Win+Ctrl+R lo rielabora comunque.")
                beep_silence()
                return
            log(f"   [live/{lang}] {time.perf_counter()-t0:.2f}s | '{text}'")
        if flags["cancel"] or not text:
            if not text:
                log("   [insert] vuoto (silenzio?)")
            return
        if looks_hallucinated(text, rms, peak):
            log(f"   [silenzio] trascritto '{text}' con audio debole (rms {rms:.5f}, picco "
                f"{peak:.5f}): allucinazione tipica, non incollo. Win+Ctrl+R per forzarlo.")
            beep_silence()
            return
        _finish(text, lang, hwnd, edit, sel, t0, ctx=ctx)
    finally:
        ui["state"] = "idle"
        if not live["expect_paste"] and not rec["held"]:
            live_cancel()               # niente incolla in arrivo (e nessuna dettatura nuova in
            #                             corso, se no si spegnerebbe il motore di quella): via


def archive_frames(frames, edit=False):
    """Salva in archivio dell'audio registrato senza elaborarlo: serve quando una registrazione
    viene interrotta, cosi' resta comunque recuperabile."""
    try:
        a = np.concatenate(frames).reshape(-1)
        p = archive_path(edit)
        save_wav(p, downsample_48k_to_16k(a), SR)
        log(f"   [archivio] registrazione interrotta salvata in {os.path.basename(p)}")
    except Exception as e:
        log(f"   [archivio] non salvata: {e}")


def reprocess_last(hwnd, path=None):
    """Win+Ctrl+R: rielabora l'ultima registrazione archiviata (rete tornata, Groq caduto,
    ESC premuto per sbaglio) e la incolla nella finestra attiva. Niente di detto va perso."""
    t0 = time.perf_counter()
    path = path or last_recording()
    if not path:
        log("   [recupero] nessuna registrazione in archivio")
        beep(400, 140)
        live_recover_start(hwnd)
        live_end("no_audio", LIVE_RECOVER_HOLD)
        return
    flags["cancel"] = False
    flags["dismiss"] = False
    ui["state"] = "proc"
    try:
        a16, sr = read_wav(path)
        dur = len(a16) / max(sr, 1)
        log(f"\n=== RECUPERO {os.path.basename(path)} ({dur:.1f}s) — "
            f"target: '{win32gui.GetWindowText(hwnd)}' ===")
        live_recover_start(hwnd, dur)
        box = []                       # il contesto si legge mentre la trascrizione lavora
        if CONTEXT_ON and ctx_mod is not None:
            threading.Thread(target=lambda: box.append(ctx_mod.before_caret(hwnd)),
                             daemon=True).start()
        text, lang = stt(path, a16, t0)
        rctx = box[0] if box else None
        if flags["cancel"] or not text:
            log("   [recupero] nessun testo")
            live_end("unrecovered", LIVE_RECOVER_HOLD)
            return
        _finish(text, lang, hwnd, False, "", t0, ctx=rctx)
        if not live["expect_paste"]:      # formattazione a vuoto (EMPTY): niente incolla, niente
            log("   [recupero] niente da incollare")   # esito -> la card resterebbe appesa
            live_end("unrecovered", LIVE_RECOVER_HOLD)
    except Exception as e:
        log(f"   [recupero] errore: {e}")
        live_end("unrecovered", LIVE_RECOVER_HOLD)
    finally:
        ui["state"] = "idle"


def abort_chord_rec():
    """Il chord Win+Ctrl ha appena avviato una registrazione ma era Win+Ctrl+lettera: annullala.
    Se stavi registrando davvero (oltre 1,5 s) l'audio non si butta: va in archivio."""
    if not rec["held"]:
        return
    started = time.perf_counter() - rec["t0"]
    rec["held"] = False
    live_cancel()
    try:
        rec["stream"].stop()
        rec["stream"].close()
    except Exception:
        pass
    frames, rec["frames"] = rec["frames"], []
    if started > 1.5 and frames:
        archive_frames(frames, rec["edit"])
    ui["state"] = "idle"


def chord_recover():
    """Win+Ctrl+R: rielabora l'ultima registrazione nella finestra attiva."""
    hw = rec["hwnd"] if rec["held"] else win32gui.GetForegroundWindow()
    target = last_recording()   # deciso PRIMA di archiviare, se no si recupera se' stessa
    abort_chord_rec()
    threading.Thread(target=reprocess_last, args=(hw, target), daemon=True).start()


def chord_style():
    """Win+Ctrl+T: prossimo stile della card."""
    abort_chord_rec()
    style_cycle()


def worker():
    """Thread separato: polling toggle + registrazione + trascrizione."""
    prev = False
    esc_prev = False
    r_prev = False
    t_prev = False
    while not flags["quit"]:
        now = chord_down()
        if now and _down(VK_Q):         # Win+Ctrl+Q -> esci del tutto
            flags["quit"] = True
            break
        r = now and _down(VK_R)         # Win+Ctrl+R -> rielabora l'ultima registrazione
        t = now and _down(VK_T)         # Win+Ctrl+T -> stile della card (stamp/glyph/signal)
        if (r and not r_prev) or (t and not t_prev):
            swallow_win()
            if r and not r_prev:
                chord_recover()
            else:
                chord_style()
            r_prev, t_prev, prev = r, t, now   # prev aggiornato: il toggle non deve scattare
            time.sleep(0.02)
            continue
        r_prev, t_prev = r, t
        edit_on = rec["held"] and rec["edit"]
        if edit_on != bool(edit_keys["hooks"]):     # tasti 1-3 solo mentre registri un Edit
            edit_keys_set(edit_on)
        if edit_on and rec["chip"]:     # tasto 1-3 premuto: stop, il comando e' deciso
            stop_and_process()
            edit_keys_set(False)
            prev = now
            time.sleep(0.02)
            continue
        if rec["held"] and time.perf_counter() - rec["t0"] > rec["remind"] * REC_REMIND_SEC:
            mins = rec["remind"] * REC_REMIND_SEC // 60     # promemoria: microfono ancora aperto
            rec["remind"] += 1
            log(f"   [rec] registrazione ancora attiva da {mins} min")
            threading.Thread(target=lambda: (beep(880, 70), beep(660, 70)), daemon=True).start()
        esc = _down(0x1B)               # ESC -> annulla la dettatura in corso (qualunque fase)
        if esc and not esc_prev:
            st = ui["state"]
            if st == "rec":
                rec["held"] = False
                flags["cancel"] = True
                try:
                    rec["stream"].stop()
                    rec["stream"].close()
                except Exception:
                    pass
                live_end("cancelled", LIVE_CANCEL_HOLD)
                ui["state"] = "idle"
                log("   [ESC] annullata")
            elif st == "proc":
                flags["cancel"] = True
                live_end("cancelled", LIVE_CANCEL_HOLD)
                log("   [ESC] elaborazione annullata")
            flags["dismiss"] = True        # ESC nasconde l'HUD in ogni skin (anche faccina/pallina)
        esc_prev = esc
        if now and not prev:            # Win+Ctrl -> dettatura, o Edit Mode se c'è selezione
            swallow_win()
            if not rec["held"]:
                k = start_capture()     # Q/R/T arrivate mentre Win+Ctrl era giu': comando
                if k == "Q":
                    flags["quit"] = True
                    break
                if k == "R":
                    chord_recover()
                    r_prev = True       # stessa pressione: non rieseguirlo al giro dopo
                elif k == "T":
                    chord_style()
                    t_prev = True
            else:
                stop_and_process()
        prev = now
        time.sleep(0.02)


# ---------- UI (thread principale) ----------
TRANSP = "#010203"            # colore-chiave -> sfondo trasparente (cane che fluttua, niente card)
TRANSP_RGB = (1, 2, 3)
BLUE = (46, 155, 238)         # onda "in ascolto" (dal prototipo)
BAR_GREY = (120, 128, 142)
HUD = 132                     # lato del cane in px
YC, BAND_H = 0.840, 0.070     # geometria barra (frazioni del quadrato, dal prototipo frozen)
WAVE_L, WAVE_W = 0.245, 0.510
COVER_L, COVER_W = 0.215, 0.570
NBARS = 16
CAPTIONS = {"idle": "In attesa", "rec": "In ascolto", "proc": "Sto pensando…"}
POSE_FOR = {"idle": 0, "rec": 1, "proc": 2}

SKINS = ["dog", "face", "orb"]     # cane · faccina che segue il mouse · pallina


def _load_skin():
    try:
        s = open("skin.txt", encoding="utf-8").read().strip()
        return s if s in SKINS else "dog"
    except Exception:
        return "dog"


def _save_skin(s):
    try:
        with open("skin.txt", "w", encoding="utf-8") as f:
            f.write(s)
    except Exception:
        pass


SKIN = _load_skin()


def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def _load_hud():
    """Carica le 3 pose ridimensionate + campiona il colore scuro della barra."""
    full = Image.open("assets/pose-1-attesa.png").convert("RGBA")
    W, H = full.size
    px = full.load()
    rs = gs = bs = n = 0
    for y in range(int((YC - BAND_H / 2) * H), int((YC + BAND_H / 2) * H)):
        for x in range(int(0.30 * W), int(0.70 * W)):
            r, g, b, a = px[x, y]
            if a > 200 and r < 60 and g < 60 and b < 60:
                rs += r; gs += g; bs += b; n += 1
    pill = (rs // n, gs // n, bs // n) if n else (22, 24, 30)
    poses = [Image.open(f).convert("RGBA").resize((HUD, HUD), Image.LANCZOS)
             for f in ("assets/pose-1-attesa.png", "assets/pose-2-registra.png", "assets/pose-3-elabora.png")]
    return poses, pill


def toggle_llm():
    global USE_LLM
    USE_LLM = not USE_LLM
    beep(1200 if USE_LLM else 500, 80)


def _key_rgb(rgba):
    """Taglio netto dell'alpha: pixel opachi tengono il colore, il resto -> chiave (trasparente)."""
    arr = np.asarray(rgba)
    rgb = arr[:, :, :3].copy()
    rgb[arr[:, :, 3] < 128] = TRANSP_RGB
    return Image.fromarray(rgb, "RGB")


def _sparkle(d, cx, cy, r, fill):
    d.polygon([(cx, cy - r), (cx + r * .3, cy - r * .3), (cx + r, cy), (cx + r * .3, cy + r * .3),
               (cx, cy + r), (cx - r * .3, cy + r * .3), (cx - r, cy), (cx - r * .3, cy - r * .3)], fill=fill)


def _draw_toggle(img, x0, y0, w, h, on, font):
    """Interruttore AI 'figo': pillola con gradiente blu->viola + sparkle + knob nitido."""
    r = h // 2
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    if on:
        top, bot = (124, 92, 255), (46, 155, 238)   # viola -> blu
        for yy in range(h):
            t = yy / (h - 1)
            pd.line([(0, yy), (w, yy)], fill=(int(top[0] + (bot[0] - top[0]) * t),
                                              int(top[1] + (bot[1] - top[1]) * t),
                                              int(top[2] + (bot[2] - top[2]) * t), 255))
    else:
        pd.rectangle([0, 0, w, h], fill=(150, 156, 168, 255))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    pill.putalpha(mask)
    img.alpha_composite(pill, (x0, y0))

    d = ImageDraw.Draw(img)
    kd = h - 6
    kx = (x0 + w - kd - 3) if on else (x0 + 3)
    ky = y0 + 3
    if on:
        d.text((x0 + 9, y0 + h / 2 - 1), "AI", anchor="lm", fill=(255, 255, 255, 255), font=font)
        _sparkle(d, x0 + 30, y0 + h / 2, 3.6, (255, 255, 255, 255))
    else:
        d.text((x0 + w - 22, y0 + h / 2 - 1), "AI", anchor="lm", fill=(255, 255, 255, 255), font=font)
    d.ellipse([kx - 1, ky - 1, kx + kd + 1, ky + kd + 1], fill=(206, 212, 220, 255))  # bordo knob
    d.ellipse([kx, ky, kx + kd, ky + kd], fill=(255, 255, 255, 255))


# ---------- motore grafico a canale alpha reale (UpdateLayeredWindow) ----------
_gdi = ctypes.windll.gdi32
_user = ctypes.windll.user32


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPPM", ctypes.c_int32), ("biYPPM", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


_user.GetDC.restype = wintypes.HDC
_user.GetDC.argtypes = [wintypes.HWND]
_user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user.UpdateLayeredWindow.restype = wintypes.BOOL
_user.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                      ctypes.POINTER(_SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                      wintypes.DWORD, ctypes.POINTER(_BLEND), wintypes.DWORD]
_gdi.CreateCompatibleDC.restype = wintypes.HDC
_gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi.CreateDIBSection.restype = wintypes.HBITMAP
_gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BMIH), wintypes.UINT,
                                  ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_gdi.SelectObject.restype = wintypes.HGDIOBJ
_gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi.DeleteDC.argtypes = [wintypes.HDC]


def paint_layered(hwnd, x, y, img):
    """Disegna un'immagine RGBA con vera trasparenza per-pixel (glass/glow/bordi lisci)."""
    w, h = img.size
    arr = np.asarray(img.convert("RGBA")).astype(np.uint16)
    al = arr[:, :, 3:4]
    pm = (arr[:, :, :3] * al // 255).astype(np.uint8)          # alpha pre-moltiplicato
    bgra = np.dstack([pm[:, :, 2], pm[:, :, 1], pm[:, :, 0], arr[:, :, 3].astype(np.uint8)])
    buf = bgra.tobytes()
    screen = _user.GetDC(None)
    mem = _gdi.CreateCompatibleDC(screen)
    bmi = _BMIH()
    bmi.biSize = ctypes.sizeof(_BMIH)
    bmi.biWidth = w
    bmi.biHeight = -h                                          # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    bits = ctypes.c_void_p()
    hbmp = _gdi.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, buf, len(buf))
    old = _gdi.SelectObject(mem, hbmp)
    blend = _BLEND(0, 0, 255, 1)                               # AC_SRC_OVER, AC_SRC_ALPHA
    size = _SIZE(w, h)
    ptd = wintypes.POINT(int(x), int(y))
    pts = wintypes.POINT(0, 0)
    _user.UpdateLayeredWindow(hwnd, screen, ctypes.byref(ptd), ctypes.byref(size),
                              mem, ctypes.byref(pts), 0, ctypes.byref(blend), 2)  # ULW_ALPHA
    _gdi.SelectObject(mem, old)
    _gdi.DeleteObject(hbmp)
    _gdi.DeleteDC(mem)
    _user.ReleaseDC(None, screen)


def ease_k(c, k):
    """Uno smorzamento scritto per il frame da 55 ms, applicato a k frame (k = 1 -> identico).
    Serve perche' col pannello aperto il loop gira a 30 ms: l'HUD deve muoversi alla STESSA
    velocita' di sempre, non piu' in fretta."""
    return c if k == 1.0 else 1.0 - (1.0 - c) ** k


def build_ui():
    poses, pill = _load_hud()
    W = H = HUD
    cx, cy = W / 2, H / 2
    sw = _user.GetSystemMetrics(0)
    sh = _user.GetSystemMetrics(1)
    WX, WY = sw // 2 - W // 2, sh - H - 52
    band, ycp = BAND_H * HUD, YC * HUD
    cx0, cx1 = COVER_L * HUD, (COVER_L + COVER_W) * HUD
    wx0, wx1 = WAVE_L * HUD, (WAVE_L + WAVE_W) * HUD
    a = {"cur": 0, "prev": 0, "fade": 1.0, "bars": [0.15] * NBARS, "amp": 0.3, "phase": 0.0,
         "closing": None, "vis": False, "last_active": time.perf_counter(), "boot": time.perf_counter(),
         "k": 1.0}      # k = quanti frame "da 55 ms" vale questo giro (1.0 = passo di sempre)

    def cycle_skin(delta):
        global SKIN
        SKIN = SKINS[(SKINS.index(SKIN) + delta) % len(SKINS)]
        _save_skin(SKIN)
        log(f"   [skin] {SKIN}")

    def mouse_dir():
        try:
            gx, gy = win32api.GetCursorPos()
            dx, dy = gx - (WX + W / 2), gy - (WY + H / 2)
            dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
            return dx / dist, dy / dist
        except Exception:
            return 0.0, 0.0

    def render_dog():
        st = ui["state"]
        tp = POSE_FOR[st]
        if tp != a["cur"]:
            a["prev"], a["cur"], a["fade"] = a["cur"], tp, 0.0
        if a["fade"] < 1.0:
            a["fade"] = min(1.0, a["fade"] + 0.14 * a["k"])
            base = Image.blend(poses[a["prev"]], poses[a["cur"]], a["fade"])
        else:
            base = poses[a["cur"]].copy()
        if st in ("rec", "proc"):
            d = ImageDraw.Draw(base)
            d.rectangle([cx0, ycp - band / 2, cx1, ycp + band / 2], fill=pill + (255,))
            slot = (wx1 - wx0) / NBARS
            bw, maxh = slot * 0.52, band * 2.4
            if st == "rec":
                a["amp"] += ((0.9 if int(a["phase"] * 7) % 3 else 0.25) - a["amp"]) * ease_k(0.15, a["k"])
                for i in range(NBARS):
                    ctr = 1 - abs(i - (NBARS - 1) / 2) / ((NBARS - 1) / 2)
                    tgt = 0.14 + a["amp"] * ctr * (0.4 + 0.6 * abs(float(np.sin(a["phase"] * 3 + i))))
                    a["bars"][i] += (tgt - a["bars"][i]) * ease_k(0.35, a["k"])
                    h = a["bars"][i] * maxh
                    x = wx0 + i * slot + (slot - bw) / 2
                    d.rounded_rectangle([x, ycp - h / 2, x + bw, ycp + h / 2], radius=bw / 2, fill=BLUE + (255,))
            else:
                for i in range(5):
                    cxp = wx0 + (wx1 - wx0) * (i + 0.5) / 5
                    off = abs(float(np.sin(a["phase"] * 2.2 - i * 0.9))) * band * 1.7
                    rad = band * 0.6
                    d.ellipse([cxp - rad, ycp - off - rad, cxp + rad, ycp - off + rad], fill=BLUE + (255,))
        return base

    def render_face():
        st = ui["state"]
        ph = a["phase"]
        B = W * 4                                                                  # supersampling 4x -> bordi lisci
        bc = B / 2
        img = Image.new("RGBA", (B, B), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if st == "proc":                                                           # "sta scrivendo": faccina + 2 manine su una nota
            R = B * 0.15
            fcy = B * 0.27
            d.ellipse([bc - R, fcy - R, bc + R, fcy + R], fill=(30, 31, 38, 255))
            for sgn in (-1, 1):                                                     # occhi concentrati, guardano in basso
                exx, ey = bc + sgn * R * 0.42, fcy + R * 0.06
                d.ellipse([exx - R * 0.22, ey - R * 0.22, exx + R * 0.22, ey + R * 0.22], fill=(245, 246, 250, 255))
                d.ellipse([exx - R * 0.11, ey + R * 0.04, exx + R * 0.11, ey + R * 0.26], fill=(28, 28, 36, 255))
            nx0, ny0, nx1, ny1 = B * 0.24, B * 0.5, B * 0.76, B * 0.85             # la nota
            d.rounded_rectangle([nx0, ny0, nx1, ny1], radius=B * 0.03, fill=(248, 249, 252, 255))
            for k in range(3):                                                      # righe che si riempiono a turno
                ly = ny0 + (ny1 - ny0) * (0.3 + 0.2 * k)
                frac = min(1.0, max(0.0, (float(np.sin(ph * 1.5)) * 0.5 + 0.5) * 3 - k))
                if frac > 0.02:
                    d.line([nx0 + B * 0.06, ly, nx0 + B * 0.06 + (nx1 - nx0 - B * 0.12) * frac, ly],
                           fill=(150, 160, 180, 255), width=int(B * 0.009))
            hr = B * 0.055
            d.ellipse([nx0 - hr, ny1 - hr * 1.3, nx0 + hr, ny1 + hr * 0.7], fill=(44, 46, 56, 255))       # mano sx tiene il foglio
            wx = nx0 + B * 0.12 + (nx1 - nx0 - B * 0.24) * (float(np.sin(ph * 3)) * 0.5 + 0.5)             # mano dx scrive
            wy = ny0 + (ny1 - ny0) * 0.52
            d.line([wx, wy, wx + B * 0.055, wy - B * 0.075], fill=(80, 80, 92, 255), width=int(B * 0.014))  # penna
            d.ellipse([wx - hr, wy - hr * 0.5, wx + hr, wy + hr * 1.5], fill=(44, 46, 56, 255))
            return img.resize((W, H), Image.LANCZOS)
        R = B * 0.27
        d.ellipse([bc - R, bc - R, bc + R, bc + R], fill=(30, 31, 38, 255))
        hi = Image.new("RGBA", (B, B), (0, 0, 0, 0))                                # lucido morbido
        ImageDraw.Draw(hi).ellipse([bc - R * 0.7, bc - R * 0.85, bc + R * 0.7, bc - R * 0.05], fill=(255, 255, 255, 24))
        img.alpha_composite(hi.filter(ImageFilter.GaussianBlur(12)))
        d = ImageDraw.Draw(img)
        ux, uy = mouse_dir()
        exd, ey = R * 0.40, bc - R * 0.02
        er = R * 0.25 if st == "rec" else R * 0.21
        sleepy = st == "idle" and (time.perf_counter() - a["last_active"] > 7)
        blink = (ph % 24) < 0.5
        for sgn in (-1, 1):
            exx = bc + sgn * exd
            if sleepy or blink:
                d.line([exx - er, ey, exx + er, ey], fill=(240, 242, 248, 255), width=int(R * 0.13))
            else:
                d.ellipse([exx - er, ey - er, exx + er, ey + er], fill=(245, 246, 250, 255))
                pr = er * 0.48
                qx, qy = exx + ux * er * 0.35, ey + uy * er * 0.35
                d.ellipse([qx - pr, qy - pr, qx + pr, qy + pr], fill=(28, 28, 36, 255))
        if st == "rec":
            d.ellipse([bc - R * 0.09, bc + R * 0.44, bc + R * 0.09, bc + R * 0.6], fill=(245, 246, 250, 255))   # bocca
            for side in (1, -1):                                                          # ondine sonore su entrambi i lati
                wcx, wcy = bc + side * R * 0.98, bc - R * 0.06
                a0 = -50 if side == 1 else 130
                for k in range(3):
                    rr = R * (0.28 + 0.22 * k) + R * 0.1 * (float(np.sin(ph * 3 - k)) * 0.5 + 0.5)
                    d.arc([wcx - rr, wcy - rr, wcx + rr, wcy + rr], a0, a0 + 100,
                          fill=(95, 215, 175, max(70, 255 - k * 62)), width=int(B * 0.014))
        else:
            d.arc([bc - R * 0.28, bc + R * 0.15, bc + R * 0.28, bc + R * 0.5], 20, 160,
                  fill=(240, 242, 248, 255), width=int(R * 0.09))
        return img.resize((W, H), Image.LANCZOS)

    def render_orb():
        st = ui["state"]
        ph = a["phase"]
        B = W * 4
        bc = B / 2
        R = B * 0.27 + (B * 0.012 * abs(float(np.sin(ph * 3))) if st == "rec" else 0)
        accent = (95, 175, 255) if st == "proc" else (95, 215, 175) if st == "rec" else None
        img = Image.new("RGBA", (B, B), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([bc - R, bc - R, bc + R, bc + R], fill=(20, 26, 40, 110))         # vetro più translucido
        core = Image.new("RGBA", (B, B), (0, 0, 0, 0))                              # profondità sfumata
        ImageDraw.Draw(core).ellipse([bc - R * 0.75, bc - R * 0.55, bc + R * 0.75, bc + R * 0.92], fill=(8, 12, 22, 95))
        img.alpha_composite(core.filter(ImageFilter.GaussianBlur(16)))
        d = ImageDraw.Draw(img)
        d.arc([bc - R, bc - R, bc + R, bc + R], 205, 350, fill=(205, 222, 255, 130), width=int(B * 0.008))  # rim light
        d.ellipse([bc - R, bc - R, bc + R, bc + R], outline=(150, 175, 220, 70), width=int(B * 0.004))
        if st in ("rec", "proc"):                                                  # onde a 3 strati
            for amp, freq, alpha, poff in ((0.5, 3.0, 255, 0.0), (0.34, 5.0, 150, 1.7), (0.22, 8.0, 95, 3.2)):
                pts = []
                step = max(2, int(B * 0.008))
                for i in range(int(-R), int(R), step):
                    x = bc + i
                    y = bc + R * amp * float(np.sin(ph * 3 + (i / R) * freq * 3.0 + poff))
                    if (x - bc) ** 2 + (y - bc) ** 2 < (R * 0.86) ** 2:
                        pts.append((x, y))
                if len(pts) > 1:
                    d.line(pts, fill=accent + (alpha,), width=int(B * 0.016), joint="curve")
        hi = Image.new("RGBA", (B, B), (0, 0, 0, 0))                                # gloss brillante
        ImageDraw.Draw(hi).ellipse([bc - R * 0.5, bc - R * 0.72, bc + R * 0.02, bc - R * 0.16], fill=(255, 255, 255, 210))
        img.alpha_composite(hi.filter(ImageFilter.GaussianBlur(8)))
        return img.resize((W, H), Image.LANCZOS)

    def frame():
        base = render_face() if SKIN == "face" else render_orb() if SKIN == "orb" else render_dog()
        a["phase"] += 0.12 * a["k"]
        if a["closing"] is not None:
            s = max(0.05, 1.0 - 0.8 * a["closing"])
            sw_, sh_ = max(1, int(W * s)), max(1, int(H * s))
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            canvas.alpha_composite(base.resize((sw_, sh_), Image.LANCZOS), ((W - sw_) // 2, (H - sh_) // 2))
            return canvas
        return base

    hinst = win32api.GetModuleHandle(None)

    def wproc(hwnd, msg, wp, lp):
        if msg == win32con.WM_LBUTTONDOWN:
            cycle_skin(1)
        elif msg == win32con.WM_RBUTTONDOWN:
            cycle_skin(-1)
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)

    wc = win32gui.WNDCLASS()
    wc.lpszClassName = "WavetypeHUD"
    wc.hInstance = hinst
    wc.lpfnWndProc = wproc
    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass
    ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST |
          win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
    hwnd = win32gui.CreateWindowEx(ex, "WavetypeHUD", "Wavetype", win32con.WS_POPUP,
                                   WX, WY, W, H, 0, 0, hinst, None)

    def show(v):
        if v and not a["vis"]:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            a["vis"] = True
        elif not v and a["vis"]:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            a["vis"] = False

    def tick(hwnd):
        a["k"] = (LOOP_LIVE / LOOP_IDLE) if live_panel_open() else 1.0
        while insert_jobs:
            h, t = insert_jobs.pop(0)
            insert_text(h, t)
            live_pasted()          # il pannello si chiude qui, subito dopo l'incolla
        active = ui["state"] in ("rec", "proc")
        now = time.perf_counter()
        if active:
            a["last_active"] = now
        want = hud_want(active, now - a["last_active"] < 0.6, now - a["boot"] < 4.0)
        if live["open"] or live_panel_holds_hud():
            # La card e' l'unico indicatore: l'HUD sparisce subito, dal tasto premuto (live["open"]
            # e' gia' vero prima che la card compaia), senza animazione d'uscita sopra la card.
            a["closing"] = None
            show(False)
            live_tick()
            return
        if want:
            a["closing"] = None
            show(True)
        elif a["vis"]:
            a["closing"] = 0.0 if a["closing"] is None else min(1.0, a["closing"] + 0.14 * a["k"])
            if a["closing"] >= 1.0:
                a["closing"] = None
                show(False)
        if a["vis"]:
            try:
                paint_layered(hwnd, WX, WY, frame())
            except Exception as e:
                log(f"[ui] paint: {e}")
        live_tick()                # pannello live: stesso thread dell'HUD, mai altrove

    log(f"[alpha] finestra creata hwnd={hwnd}, loop avviato")
    live["ui"] = True              # da qui il pannello ha chi lo disegna
    if card_ready():               # typelib COM del caret (~0,5 s): all'avvio, mai in dettatura
        try:
            caret_mod.set_log(log)
            caret_mod.prewarm()
            if ctx_mod is not None:
                ctx_mod.set_log(log)
        except Exception as e:
            log(f"   [live] prewarm caret: {e}")
    frames = 0
    while not flags["quit"]:
        try:
            win32gui.PumpWaitingMessages()
            tick(hwnd)
        except Exception as e:
            log(f"[alpha] errore loop: {e}")
        frames += 1
        if LOG_HEARTBEAT and frames % 200 == 0:
            log(f"[alpha] battito {frames} skin={SKIN}")
        time.sleep(LOOP_LIVE if live_panel_open() else LOOP_IDLE)
    live["ui"] = False
    try:
        if live["panel"] is not None:
            live["panel"].destroy()
    except Exception:
        pass
    try:
        win32gui.DestroyWindow(hwnd)
    except Exception:
        pass


def build_ui_tk():          # FALLBACK: vecchia UI tkinter (taglio-netto), usata se il motore alpha fallisce
    poses, pill = _load_hud()
    W = H = HUD

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", TRANSP)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{sw // 2 - W // 2}+{sh - H - 52}")
    root.configure(bg=TRANSP)
    c = tk.Canvas(root, width=W, height=H, bg=TRANSP, highlightthickness=0)
    c.pack()
    img_item = c.create_image(0, 0, anchor="nw", image=None)

    def cycle_skin(delta):
        global SKIN
        SKIN = SKINS[(SKINS.index(SKIN) + delta) % len(SKINS)]
        _save_skin(SKIN)
        log(f"   [skin] {SKIN}")
    c.bind("<Button-1>", lambda e: cycle_skin(1))     # sinistro -> prossima interfaccia
    c.bind("<Button-3>", lambda e: cycle_skin(-1))    # destro -> precedente

    try:  # non rubare il focus
        hwnd = root.winfo_id()
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE,
                               ex | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW)
    except Exception as e:
        log(f"[ui] no-activate non applicato: {e}")

    band = BAND_H * HUD
    ycp = YC * HUD
    cx0, cx1 = COVER_L * HUD, (COVER_L + COVER_W) * HUD
    wx0, wx1 = WAVE_L * HUD, (WAVE_L + WAVE_W) * HUD
    a = {"cur": 0, "prev": 0, "fade": 1.0, "bars": [0.15] * NBARS, "amp": 0.3,
         "phase": 0.0, "photo": None, "vis": True, "closing": None,
         "last_active": time.perf_counter(), "boot": time.perf_counter()}

    def mouse_dir():
        """Versore dal centro dell'HUD verso il cursore del mouse."""
        try:
            gx, gy = win32api.GetCursorPos()
            fx = root.winfo_rootx() + W / 2
            fy = root.winfo_rooty() + H / 2
            dx, dy = gx - fx, gy - fy
            dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
            return dx / dist, dy / dist, dist
        except Exception:
            return 0.0, 0.0, 0.0

    def render_dog():
        st = ui["state"]
        tp = POSE_FOR[st]
        if tp != a["cur"]:
            a["prev"], a["cur"], a["fade"] = a["cur"], tp, 0.0
        if a["fade"] < 1.0:
            a["fade"] = min(1.0, a["fade"] + 0.14)
            base = Image.blend(poses[a["prev"]], poses[a["cur"]], a["fade"])
        else:
            base = poses[a["cur"]].copy()
        if st in ("rec", "proc"):
            d = ImageDraw.Draw(base)
            d.rectangle([cx0, ycp - band / 2, cx1, ycp + band / 2], fill=pill + (255,))
            slot = (wx1 - wx0) / NBARS
            bw = slot * 0.52
            maxh = band * 2.4
            if st == "rec":
                a["amp"] += ((0.9 if int(a["phase"] * 7) % 3 else 0.25) - a["amp"]) * 0.15
                for i in range(NBARS):
                    ctr = 1 - abs(i - (NBARS - 1) / 2) / ((NBARS - 1) / 2)
                    tgt = 0.14 + a["amp"] * ctr * (0.4 + 0.6 * abs(float(np.sin(a["phase"] * 3 + i))))
                    a["bars"][i] += (tgt - a["bars"][i]) * 0.35
                    h = a["bars"][i] * maxh
                    x = wx0 + i * slot + (slot - bw) / 2
                    d.rounded_rectangle([x, ycp - h / 2, x + bw, ycp + h / 2], radius=bw / 2, fill=BLUE + (255,))
            else:
                ND = 5
                rad = band * 0.6
                for i in range(ND):
                    cxp = wx0 + (wx1 - wx0) * (i + 0.5) / ND
                    off = abs(float(np.sin(a["phase"] * 2.2 - i * 0.9))) * band * 1.7
                    d.ellipse([cxp - rad, ycp - off - rad, cxp + rad, ycp - off + rad], fill=BLUE + (255,))
        return base

    def render_face():
        st = ui["state"]
        ph = a["phase"]
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        R = W * 0.34                                # più piccola, ed è un CERCHIO
        cx, cy = W / 2, H / 2
        body = {"idle": (26, 26, 30, 255), "rec": (22, 22, 26, 255), "proc": (18, 30, 55, 255)}[st]
        d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=body)
        # anello di stato: rosso pulsante = ascolto · arco blu che gira = elaborazione
        if st == "rec":
            rr = R + W * 0.02 + W * 0.02 * abs(float(np.sin(ph * 3)))
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=(255, 80, 90, 255), width=max(2, int(W * 0.02)))
        elif st == "proc":
            ang = (ph * 60) % 360
            d.arc([cx - R - 3, cy - R - 3, cx + R + 3, cy + R + 3], ang, ang + 100, fill=BLUE + (255,), width=max(2, int(W * 0.025)))
        ux, uy, _ = mouse_dir()
        exd, ey = R * 0.42, cy - R * 0.06
        ew, eh = R * 0.20, R * 0.30
        off = R * 0.16
        sleepy = st == "idle" and (time.perf_counter() - a["last_active"] > 6)
        blink = (ph % 26) < 0.6
        if st == "rec":
            ew, eh = R * 0.22, R * 0.44             # occhi spalancati
        col = (245, 246, 250, 255)
        for sgn in (-1, 1):
            ex = cx + sgn * exd
            px, py = ex + ux * off, ey + uy * off
            if sleepy or blink:
                d.line([px - ew, py, px + ew, py], fill=col, width=max(3, int(R * 0.11)))
            else:
                d.ellipse([px - ew, py - eh, px + ew, py + eh], fill=col)
                if st != "rec":                     # pupilla che segue il mouse -> più viva/3D
                    pr = ew * 0.5
                    qx, qy = px + ux * ew * 0.4, py + uy * eh * 0.4
                    d.ellipse([qx - pr, qy - pr, qx + pr, qy + pr], fill=(18, 18, 26, 255))
        if st == "rec":                             # bocca "o" aperta
            d.ellipse([cx - R * 0.14, cy + R * 0.42, cx + R * 0.14, cy + R * 0.66], fill=col)
        elif st == "proc":                          # 3 puntini "sto pensando"
            for i in range(3):
                bx = cx + (i - 1) * R * 0.26
                r = R * 0.06 * (0.6 + 0.4 * abs(float(np.sin(ph * 2 - i))))
                d.ellipse([bx - r, cy + R * 0.52 - r, bx + r, cy + R * 0.52 + r], fill=BLUE + (255,))
        else:                                       # sorrisino a riposo
            d.arc([cx - R * 0.3, cy + R * 0.2, cx + R * 0.3, cy + R * 0.55], 20, 160, fill=col, width=max(2, int(R * 0.08)))
        return img

    def render_orb():
        st = ui["state"]
        ph = a["phase"]
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, cy = W / 2, H / 2
        R = W * 0.40 + (W * 0.02 * abs(float(np.sin(ph * 3))) if st == "rec" else 0)
        base = (16, 16, 20) if st != "proc" else (14, 28, 58)
        d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=base + (255,))
        for k, cc in ((0.62, (34, 36, 44)), (0.4, (60, 64, 76)), (0.2, (110, 116, 132))):
            rr = R * k                              # shading 3D: alone chiaro verso alto-sinistra
            ox, oy = cx - R * 0.26, cy - R * 0.28
            d.ellipse([ox - rr, oy - rr, ox + rr, oy + rr], fill=cc + (255,))
        if st in ("rec", "proc"):                   # onda interna che scorre
            pts = []
            for i in range(int(-R), int(R), 3):
                x = cx + i
                y = cy + R * 0.45 * float(np.sin(ph * 3 + i * 0.12))
                if (x - cx) ** 2 + (y - cy) ** 2 < (R * 0.82) ** 2:
                    pts.append((x, y))
            if len(pts) > 1:
                d.line(pts, fill=BLUE + (255,), width=max(2, int(W * 0.022)), joint="curve")
            d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=BLUE + (255,), width=max(2, int(W * 0.015)))
        return img

    def render():
        base = render_face() if SKIN == "face" else render_orb() if SKIN == "orb" else render_dog()
        a["phase"] += 0.12
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if a["closing"] is not None:
            s = max(0.05, 1.0 - 0.8 * a["closing"])
            sw_, sh_ = max(1, int(W * s)), max(1, int(H * s))
            canvas.alpha_composite(base.resize((sw_, sh_), Image.LANCZOS), ((W - sw_) // 2, (H - sh_) // 2))
        else:
            canvas.alpha_composite(base, (0, 0))
        a["photo"] = ImageTk.PhotoImage(_key_rgb(canvas))
        c.itemconfig(img_item, image=a["photo"])

    def tick():
        if flags["quit"]:
            root.destroy()
            return
        while insert_jobs:                    # incolla qui, sul thread principale (stabile)
            h, t = insert_jobs.pop(0)
            insert_text(h, t)
        active = ui["state"] in ("rec", "proc")
        now = time.perf_counter()
        if active:
            a["last_active"] = now
        # con HUD_ON: faccina/pallina sempre visibili, il cane si nasconde da idle (hud_want)
        want = hud_want(active, now - a["last_active"] < 0.6, now - a["boot"] < 4.0)
        if want:
            if not a["vis"]:
                root.deiconify()
                root.attributes("-topmost", True)
                a["vis"] = True
            a["closing"] = None
        elif a["vis"]:
            a["closing"] = 0.0 if a["closing"] is None else min(1.0, a["closing"] + 0.14)
            if a["closing"] >= 1.0:
                root.withdraw()
                a["vis"] = False
                a["closing"] = None
        if a["vis"]:
            render()
        root.after(55, tick)

    tick()
    return root


def main():
    global model, _singleton
    _singleton = win32event.CreateMutex(None, False, "Wavetype_singleton")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        log("gia' in esecuzione — esco (single instance).")
        return
    engine = f"Groq ({GROQ_STT_MODEL} + {GROQ_LLM_MODEL})" if USE_GROQ else f"locale {MODEL}"
    log(f"Python {sys.version.split()[0]} | motore: {engine}")
    log(f"mic: {input_device_name()}")
    if LIVE and live_engine is None:
        log(f"[live] moduli non disponibili ({_LIVE_IMPORT_ERR}) — solo dettatura normale")
    elif LIVE and LIVE_ENGINE != "local" and not USE_GROQ:
        log("[live] senza chiave Groq l'anteprima live non parte — solo dettatura normale")
    elif LIVE:
        log(f"[live] motore anteprima: {LIVE_ENGINE} · stile card: "
            f"{live_panel.load_style()} (Win+Ctrl+T cambia)")
        threading.Thread(target=live_preload, name="live-preload", daemon=True).start()
    if WhisperModel is None:
        log(f"[locale] motore offline non disponibile ({_WHISPER_IMPORT_ERR}) — uso solo Groq.")
        if not USE_GROQ:
            log("[locale] ATTENZIONE: nessun motore attivo (Groq assente e locale bloccato). "
                "Aggiungi groq_key.txt o sblocca la DLL di PyAV.")
    else:
        try:
            model = WhisperModel(MODEL, device="cpu", compute_type=COMPUTE, cpu_threads=CPU_THREADS)  # fallback offline
        except Exception as e:
            log(f"[locale] caricamento modello fallito ({e}) — uso solo Groq.")
            model = None
    log("PRONTO. Toggle Win+Ctrl per registrare/fermare. Win+Ctrl+R recupera l'ultima "
        "registrazione. Win+Ctrl+T cambia lo stile della card. Win+Ctrl+Q per chiudere.")
    prune_archive()
    threading.Thread(target=set_mic_max, daemon=True).start()   # COM isolato su thread separato
    if USE_LLM and not USE_GROQ:             # scalda qwen locale solo se Groq non c'e'
        threading.Thread(target=lambda: llm_format("ciao", "it"), daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    try:
        build_ui()                          # motore alpha (blocca in PumpMessages)
    except Exception as e:
        log(f"[ui] motore alpha non disponibile ({e}) — fallback tkinter")
        live["ui"] = False              # nel fallback tkinter il pannello live non c'e'
        try:
            build_ui_tk().mainloop()
        except Exception as e2:
            log(f"[ui] anche tkinter ko ({e2}) — solo console")
            while not flags["quit"]:
                time.sleep(0.2)
    log("uscita.")


if __name__ == "__main__":
    main()
