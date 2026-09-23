"""
paths.py — dove Wavetype legge e scrive i suoi file.

Dal sorgente (python wavetype.py) non cambia niente: ogni file resta dove e' sempre stato,
relativo alla cartella di lavoro o accanto al modulo che lo usa.

Dall'eseguibile impacchettato (PyInstaller, sys.frozen) la cartella del programma non e' il
posto giusto per i dati dell'utente: un aggiornamento la sovrascrive e una disinstallazione la
cancella. Quindi:
  - config()   -> %APPDATA%\\Wavetype       file piccoli che l'utente puo' aprire e modificare:
                                            groq_key.txt, vocab.txt, card_style.txt, skin.txt
  - state()    -> %LOCALAPPDATA%\\Wavetype  roba della macchina, anche pesante, che non deve
                                            viaggiare coi profili roaming: recordings/ (giorni di
                                            WAV), models/ (~750 MB), log, storico, contatori Groq
  - resource() -> la cartella dei file impacchettati (sys._MEIPASS): font, immagini
Regola di Windows: Roaming per le impostazioni, Local per cio' che e' grande o legato al PC.
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _user_dir(env, fallback):
    base = os.environ.get(env) or os.path.join(os.path.expanduser("~"), fallback)
    return os.path.join(base, "Wavetype")


CONFIG_DIR = _user_dir("APPDATA", r"AppData\Roaming") if FROZEN else ""
STATE_DIR = _user_dir("LOCALAPPDATA", r"AppData\Local") if FROZEN else ""


def _in(folder, name):
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, name)


def config(name, base=""):
    """File di impostazioni. Sorgente: `base/name` (o `name` relativo alla cartella di lavoro,
    come oggi). Eseguibile: %APPDATA%\\Wavetype\\name."""
    if FROZEN:
        return _in(CONFIG_DIR, name)
    return os.path.join(base, name) if base else name


def state(name, base=""):
    """Dati della macchina (archivio, modelli, log). Sorgente come config(); eseguibile:
    %LOCALAPPDATA%\\Wavetype\\name."""
    if FROZEN:
        return _in(STATE_DIR, name)
    return os.path.join(base, name) if base else name


def resource(name, base=""):
    """File in sola lettura spediti con l'app (assets/). Eseguibile: dentro il pacchetto."""
    if FROZEN:
        return os.path.join(BUNDLE, name)
    return os.path.join(base, name) if base else name
