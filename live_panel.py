"""
live_panel.py — la card che mostra le parole MENTRE parli, accanto al cursore.

Perche' esiste: barrare il testo dentro l'editor di un'altra app su Windows non si puo'
(vedi docs/ENGINE-NOTES.md). Quindi il "vedo quello che scrive, si corregge da solo"
avviene in una nostra finestrella sopra a tutto; il testo pulito viene incollato una volta
sola alla fine, dal percorso di sempre.

Dal 19/09 la card si apre all'INIZIO della dettatura ed e' l'unico indicatore a schermo
(l'HUD si fa da parte finche' holds_hud() e' True). Tre stili a scelta (card_styles.py):
stamp, glyph, signal. Fasi: listening -> live -> (paused) -> formatting -> inserted,
oppure cancelled / offline.

La stessa card fa anche EDIT MODE: la command bar a tre chip (1 Grammar, 2 English, 3 Slack),
aperta con open_edit() al posto di open(). Vedi "Edit Mode" qui sotto.

Come si usa (tutto dal thread principale, come l'HUD):
    p = LivePanel(log=log)
    p.set_style(load_style())
    p.open(target_hwnd)          # trova il caret e si piazza, fase "listening"
    p.set_level(x)               # livello voce 0..1, a ogni blocco o a ogni giro del loop
    p.update(engine.snapshot())  # {"committed":..., "tentative":..., "rev":...}; prima parola -> "live"
    p.set_phase("paused" | "formatting" | "inserted" | "cancelled" | "offline" | "live")
    p.tick()                     # un frame: anima + disegna. True finche' e' vivo.
    p.close(pasted=True)         # uscita animata (inserted/cancelled si chiudono da soli)
    p.preview_style("glyph")     # anteprima di 1,6 s sopra il cursore (tasto che cambia stile)

EDIT MODE (contratto con wavetype.py; i tre comandi stanno in edit_chips.py):
    p.open_edit(target_hwnd, sel_words=26)   # card dei comandi accanto al cursore, "listening"
    p.set_level(x)                           # come in dettatura
    p.update(snap)                           # lo snapshot e' l'ISTRUZIONE detta: la card ne
    #                                          ricava da sola il chip (edit_chips.match)
    p.set_chip("2")                          # tasto 1-3: sceglie a mano e blocca il chip
    #                                          (None = torna al riconoscimento automatico)
    p.set_result(words=24)                   # parole del testo riscritto (pillola di esito)
    p.set_phase(f)  # listening | rewriting | done | cancelled | offline | nothing
    p.tick() / p.render_frame() / p.close()  # come in dettatura
La card di Edit NON si chiude da sola: "done" e "cancelled" restano finche' chi comanda non
chiama close(). Le fasi di Edit dentro diventano "e_<fase>" e non incrociano mai quelle della
dettatura: il disegno della dettatura e' rimasto identico (36 fotogrammi confrontati byte a
byte fra prima e dopo, 19/09).

Stati di una parola:
  nuova     -> entra in dissolvenza salendo di 4 px  (~160 ms)
  incerta   -> colore chiaro dello stile (puo' ancora cambiare)
  fissata   -> colore pieno
  tolta     -> barrata (il barrato di ogni stile si disegna da sinistra a destra), tenuta ~0,6 s,
               poi svanisce e le parole dopo scivolano a sinistra (niente stacchi netti)

Disegno: Pillow -> UpdateLayeredWindow (alpha per-pixel vero), stessa tecnica di
`paint_layered` in wavetype.py. Testo a dimensione nativa (nitido a 150%); lastra, ombre e
texture in cache: per frame si disegnano solo parole, meter e timer.
"""
import collections
import ctypes
import difflib
import math
import os
import time
import unicodedata
from ctypes import wintypes

import numpy as np
from PIL import Image

import caret as caret_mod
import card_styles as cs
import edit_chips as ec
from caret import dpi_scope
import paths

ROOT = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ stili (contratto con wavetype.py)
STYLES = cs.STYLES                      # ("stamp", "glyph", "signal")
STYLE_FILE = paths.config("card_style.txt", ROOT)
PHASES = cs.PHASES
# Edit Mode: le fasi che accetta set_phase() quando la card e' aperta con open_edit().
# Dentro diventano "e_<fase>" (cs.EDIT_MODES) cosi' non incrociano mai quelle della dettatura.
EDIT_PHASES = ("listening", "rewriting", "done", "cancelled", "offline", "nothing")
CHIPS = ec.CHIPS


def load_style():
    """Lo stile scelto (card_style.txt nella cartella dell'app). Default e sconosciuto: signal."""
    try:
        with open(STYLE_FILE, encoding="utf-8") as f:
            name = f.read().strip().lower()
    except Exception:
        return cs.DEFAULT_STYLE
    return name if name in STYLES else cs.DEFAULT_STYLE


def save_style(name):
    name = (name or "").strip().lower()
    if name not in STYLES:
        raise ValueError(f"stile sconosciuto: {name!r} (validi: {', '.join(STYLES)})")
    with open(STYLE_FILE, "w", encoding="utf-8") as f:
        f.write(name + "\n")


# ------------------------------------------------------------------ misure (px logici a 96 dpi)
MAX_LINES = 3
EDIT_MAX_LINES = 2        # l'istruzione detta: due righe a schermo, poi le vecchie salgono
KEEP_WORDS = 120          # parole tenute in vita nel pannello (a schermo se ne vedono 3 righe).
#                           Tenere tutta una dettatura lunga costa: misurato 44 ms per update()
#                           a 3.000 parole e 169 ms a 6.000, sul thread che disegna E incolla.
CARET_GAP = 10            # stacco tra la riga che stai scrivendo e la card
HUD_GAP = 12
FIT_MIN_W = 320           # sotto questa larghezza non si stringe: si sposta
EDGE = 8                  # distanza minima dai bordi dell'area di lavoro
HUD_CLASS = "WavetypeHUD"
HUD_TOP_INSET = {"face": 0.10, "orb": 0.20, "dog": 0.0}

# ------------------------------------------------------------------ tempi (s)
T_IN = 0.16               # una parola nuova entra
T_STRIKE = 0.26           # il barrato si disegna da sinistra a destra
T_HOLD = 0.60             # quanto resta barrata in tutto (poi svanisce)
T_GONE = 0.20             # dissolvenza della parola tolta
T_FIXED = 1.2             # Stamp: l'evidenziatore resta sulla parola appena fissata
TAU_GLIDE = 0.075         # costante di scorrimento delle parole (piu' basso = piu' pronto)
TAU_SIZE = 0.085          # costante di crescita della card
TAU_SCROLL = 0.055        # le righe vecchie salgono svelte: la coda nuova non aspetta
T_OPEN = 0.17
T_CLOSE_PASTE = 0.22
T_CLOSE_ESC = 0.18
T_INSERTED = 0.9          # la pillola "Inserted" resta, poi si chiude da sola
T_RECOVERED = 1.4         # recupero incollato: le parole si vedono solo qui, resta un filo di piu'
#                           (come wavetype.LIVE_RECOVERED_HOLD). Senza questo la card non si chiude:
#                           close() in un esito e' ignorata apposta (visto nell'e2e del 20/09)
T_CANCELLED = 1.2         # la card "Cancelled" resta, poi si chiude da sola
T_GHOST = 0.18            # la card piena si ritira verso il cursore mentre compare la pillola
T_PREVIEW = 1.6           # anteprima dello stile
LV_DT = 0.07              # un campione di livello voce ogni 70 ms (storia che scorre)
LV_KEEP = 64

# WinDLL() e non ctypes.windll.user32: `windll` e' una cache di processo, e le funzioni che ne
# escono sono LE STESSE per tutti i moduli. wavetype.py mette i suoi argtypes sulle stesse funzioni
# (CreateDIBSection, UpdateLayeredWindow) con le SUE classi _BMIH/_SIZE/_BLEND: chi importa per
# ultimo vince e l'altro si prende "expected LP__BMIH instance instead of pointer to _BMIH" a ogni
# frame (misurato: il pannello dentro wavetype.py non disegnava mai). Con un'istanza propria ogni
# modulo tiene le sue firme.
_user = ctypes.WinDLL("user32")
_gdi = ctypes.WinDLL("gdi32")


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPPM", ctypes.c_int32),
                ("biYPPM", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


_user.GetDC.restype = wintypes.HDC
_user.GetDC.argtypes = [wintypes.HWND]
_user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user.UpdateLayeredWindow.restype = wintypes.BOOL
_user.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                      ctypes.POINTER(_SIZE), wintypes.HDC,
                                      ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
                                      ctypes.POINTER(_BLEND), wintypes.DWORD]
_user.FindWindowW.restype = wintypes.HWND
_user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user.GetWindowRect.restype = wintypes.BOOL
_user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user.GetForegroundWindow.restype = wintypes.HWND
_gdi.CreateCompatibleDC.restype = wintypes.HDC
_gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi.CreateDIBSection.restype = wintypes.HBITMAP
_gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BMIH), wintypes.UINT,
                                  ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_gdi.SelectObject.restype = wintypes.HGDIOBJ
_gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi.DeleteDC.argtypes = [wintypes.HDC]

_PUNCT = " \t\n.,;:!?…\"'`()[]{}«»“”‘’-–—/\\"


def _norm(t):
    """Forma normalizzata per il confronto: senza punteggiatura, minuscola.
    Gli accenti restano (siamo in italiano), ma le due forme Unicode si uniformano."""
    return unicodedata.normalize("NFC", t.strip(_PUNCT).lower())


def _ease(dt, tau):
    """Quanto avvicinarsi al bersaglio in questo frame: indipendente dal frame rate."""
    return 1.0 - math.exp(-max(0.0, dt) / tau)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _smooth(p):
    p = _clamp(p, 0.0, 1.0)
    return p * p * (3 - 2 * p)


def _skin():
    """La skin dell'HUD, dallo stesso file che scrive wavetype.py. Nel dubbio: 'dog' (il piu' alto)."""
    try:
        with open(paths.config("skin.txt", ROOT), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "dog"


class _Word:
    __slots__ = ("text", "norm", "tentative", "state", "born", "gone_at", "fixed_at",
                 "x", "y", "tx", "ty", "placed", "chunks", "adv", "lines", "fk")

    def __init__(self, text, tentative, now):
        self.text = text
        self.norm = _norm(text)
        self.tentative = tentative
        self.state = "live"          # live | gone
        self.born = now
        self.gone_at = 0.0
        self.fixed_at = -1.0 if tentative else now
        self.x = self.y = 0.0
        self.tx = self.ty = 0.0
        self.placed = False
        self.chunks = None           # cache di misura: [(testo, dx, riga)]
        self.adv = 0.0
        self.lines = 1
        self.fk = None               # font con cui e' stata misurata

    def retext(self, text):
        self.text = text
        self.norm = _norm(text)
        self.chunks = None


# ------------------------------------------------------------------ pannello
class LivePanel:
    """Una sola istanza per processo. Tutte le chiamate win32 sul thread principale."""

    def __init__(self, log=print, clock=None, scale=None, style=None):
        self.log = log
        self.clock = clock or time.perf_counter
        self.words = []
        self._rev = None
        self._last_t = None
        self.phase = "closed"        # ciclo di vita: closed | opening | live | closing
        self.mode = "listening"      # fase della dettatura (contratto): vedi PHASES
        self._t_phase = 0.0
        self._t_mode = 0.0
        self._pasted = True
        self.hwnd = None
        self._shown = False
        self._target = None
        self._anchor = None          # (caret_x, caret_y, caret_h) in px fisici
        self._flip = True            # True = card SOPRA al caret (cresce verso l'alto)
        self._work = (0, 0, 1920, 1080)
        self._hud = None
        self._box = None
        self.hud_override = None     # test/mockup: rettangolo HUD imposto a mano ("none" = nessuno)
        self.anchor_override = None  # test/demo: (x, y, h) del caret in px fisici, al posto di caret.py
        self.work_override = None    # test/demo: area di lavoro (l, t, r, b)
        self._scroll = 0.0
        self._card_w = 0.0
        self._card_h = 0.0
        self._m = 0.0                # 0 = card compatta ("listening"), 1 = card piena
        self._n_lines = 1
        self._forced_scale = scale
        self.style_name = style if style in STYLES else load_style()
        self.style = None
        self._sprites = {}
        self._widths = {}
        self._plates = {}
        self.recover = False         # Win+Ctrl+R: la card del recupero (vedi open_recover)
        self._rec_dur = 0.0          # durata dell'audio che si sta recuperando (secondi)
        self.edit = False            # Edit Mode: la card dei tre comandi (vedi open_edit)
        self.sel_words = 0           # parole della selezione da riscrivere
        self.chip = None             # id del chip acceso (edit_chips), None = istruzione libera
        self._chip_forced = False    # scelto col tasto 1-3: l'istruzione detta non lo cambia piu'
        self._instr = ""             # istruzione detta finora (committed + tentative)
        self._res_words = 0          # parole del testo riscritto (pillola di esito)
        self._maxl = MAX_LINES
        self.log_prewarm = []        # (fase, m, ms) delle lastre costruite in anticipo
        self._refs = {}              # lastre di riferimento (una per stile/fase/lato), vedi _plate()
        self._pill_cache = {}
        self._lv = collections.deque([0.0] * LV_KEEP, maxlen=LV_KEEP)
        self._lv_peak = 0.0
        self._lv_t = 0.0
        self._t0 = 0.0
        self._t_freeze = None
        self._t_fmt = 0.0
        self._n_words = 0
        self.preview_name = ""
        self._pv = None              # copione dell'anteprima: [(t, committed, tentative)]
        self._pv_t0 = 0.0
        self._pill_at = None         # (cx, top, bottom) del caret dopo l'incolla
        self._last = None            # ultimo frame della card piena: (img, x, y)
        self._ghost = None
        self.last_frame_ms = 0.0
        self.font_name = ""
        self._set_scale(scale or 1.0)

    # ---------------------------------------------------------- scala, stile, font
    def _set_scale(self, s):
        self.scale = s
        self.style = cs.make(self.style_name, s)
        st = self.style
        self._px = lambda v: v * s
        self.margin = int(round(st.margin * s))
        if self.edit:
            # Edit: corpo piu' piccolo, interlinea piu' stretta e la colonna del testo rientrata
            # di quanto misura l'etichetta YOU (le righe successive restano allineate sotto).
            self.line_h = st.edit_line_h * s
            self.font = st.edit_body_font()
            self.font_struck = self.font
            self.struck_tr = 0.0
            you = cs.label_width(cs.E_YOU.upper(), st.edit_you_font(), st.edit_you_tr())
            self.text_x = st.edit_inset * s + you + st.edit_you_gap * s
            self.pad_r = st.edit_pad_r * s
        else:
            self.line_h = st.line_h * s
            self.text_x = st.text_inset * s
            self.pad_r = st.pad_r * s
            self.font = st.body_font()
            self.font_struck = st.struck_font()
            self.struck_tr = st.struck_tracking() if hasattr(st, "struck_tracking") else 0.0
        self.card_w_max = int(round(st.card_w * s))
        self.text_w = self.card_w_max - self.text_x - self.pad_r
        asc, desc = self.font.getmetrics()
        self.glyph_h = asc + desc
        self.space_w = self.font.getlength(" ")
        try:
            self.font_name = "%s %s" % self.font.getname()
        except Exception:
            self.font_name = "?"
        self._sprites.clear()
        self._widths.clear()
        self._plates.clear()
        self._refs.clear()
        self._pill_cache.clear()
        for w in self.words:
            w.chunks = None

    def set_style(self, name):
        """Cambia stile subito, anche a card aperta."""
        name = (name or "").strip().lower()
        if name not in STYLES:
            self.log(f"   [panel] stile sconosciuto: {name!r}")
            return
        if name == self.style_name and self.style is not None:
            return
        self.style_name = name
        self._set_scale(self.scale)
        if self.phase != "closed":
            self._plan()
            if self.preview_name:
                self.preview_name = cs.TITLES[name]

    def _wfont(self, w):
        """Font della parola: il barrato di Glyph passa al carattere a punti (Doto)."""
        if w.state == "gone" and self.font_struck is not self.font:
            f, tr = self.font_struck, self.struck_tr
        else:
            f, tr = self.font, 0.0
        if not cs.covers(f, w.text):
            f, tr = cs.fallback_for(self.font), 0.0
        return f, tr

    # ---------------------------------------------------------- misure testo
    def _adv(self, text, f=None, tr=0.0):
        f = f or self.font
        key = (id(f), text, tr)
        w = self._widths.get(key)
        if w is None:
            if tr:
                w = sum(f.getlength(c) + tr for c in text) - tr
            else:
                w = float(f.getlength(text))
            if len(self._widths) > 4000:
                self._widths.clear()
            self._widths[key] = w
        return w

    def _measure(self, w):
        """Riempie chunks/adv/lines. Le parole piu' lunghe della riga vengono spezzate
        (un URL lungo non deve uscire dalla card), ma restano UNA parola per il diff."""
        f, tr = self._wfont(w)
        fk = (id(f), tr, w.state == "gone")
        if w.chunks is not None and w.fk == fk:
            return
        w.fk = fk
        pad = self._px(self.style.strike_pad) if w.state == "gone" else 0.0
        full = self._adv(w.text, f, tr) + 2 * pad
        if full <= self.text_w:
            w.chunks = [(w.text, pad, 0)]
            w.adv = full
            w.lines = 1
            return
        chunks, cur, line = [], "", 0
        for ch in w.text:
            if self._adv(cur + ch, f, tr) > self.text_w and cur:
                chunks.append((cur, 0.0, line))
                line += 1
                cur = ch
            else:
                cur += ch
        if cur:
            chunks.append((cur, 0.0, line))
        w.chunks = chunks
        w.adv = self._adv(chunks[-1][0], f, tr)
        w.lines = len(chunks)

    # ---------------------------------------------------------- ciclo di vita
    def open(self, target_hwnd=None):
        """Nuova dettatura: la card compare subito, compatta, in "listening".
        Un'anteprima di stile in corso si interrompe qui."""
        self._open(target_hwnd, False, 0)

    def open_edit(self, target_hwnd=None, sel_words=0):
        """EDIT MODE: la card dei tre comandi accanto al cursore, gia' aperta per intero.
        `sel_words` = parole della selezione da riscrivere (vanno nella testata).
        Da qui in poi set_phase() parla la lingua di EDIT_PHASES e update() riceve
        l'ISTRUZIONE detta, non la dettatura."""
        self._open(target_hwnd, True, sel_words)

    def open_recover(self, target_hwnd=None, dur=0.0):
        """Win+Ctrl+R: la card del recupero dell'ultima registrazione, gia' in "recovering".
        Nessun motore dietro: le parole arrivano in un colpo solo col testo trascritto
        (comando "final"), il tempo a destra e' la durata dell'audio, non un cronometro."""
        self._open(target_hwnd, False, 0, recover=True, dur=dur)

    def _open(self, target_hwnd, edit, sel_words, recover=False, dur=0.0):
        self.recover = bool(recover)
        try:
            self._rec_dur = max(0.0, float(dur or 0.0))
        except Exception:
            self._rec_dur = 0.0
        self.edit = bool(edit)
        self.sel_words = max(0, int(sel_words or 0))
        self.chip = None
        self._chip_forced = False
        self._instr = ""
        self._res_words = 0
        self._maxl = EDIT_MAX_LINES if self.edit else MAX_LINES
        with dpi_scope():
            rect = self._caret(target_hwnd)
            if rect:
                cx, cy, _cw, ch = rect
            else:
                try:
                    px = wintypes.POINT()
                    _user.GetCursorPos(ctypes.byref(px))
                    cx, cy = px.x, px.y
                except Exception:
                    cx, cy = 0, 0
                ch = 0
            self._work = self.work_override or caret_mod.work_area(cx, cy)
            dpi = caret_mod.dpi_for_point(cx, cy)
            self._set_scale(self._forced_scale or dpi / 96.0)
            self._anchor = (cx, cy, ch) if rect else None
            self._target = target_hwnd
            self._hud = self._find_hud()
            self._plan()
        now = self.clock()
        self.words = []
        self._rev = None
        self._last_t = None
        self._scroll = 0.0
        self.mode = "e_listening" if self.edit else ("recovering" if self.recover else "listening")
        self._t_mode = now
        self._m = 1.0 if self.edit else 0.0
        cw, chh = self._target_size()
        self._card_w, self._card_h = float(cw), float(chh)
        self.phase = "opening"
        self._t_phase = now
        self._t0 = now
        self._t_freeze = None
        self._n_words = 0
        self._n_lines = 1
        self.preview_name = ""
        self._pv = None
        self._pill_at = None
        self._last = self._ghost = None
        self._lv = collections.deque([0.0] * LV_KEEP, maxlen=LV_KEEP)
        self._lv_peak = 0.0
        self._lv_t = now
        self._ensure_window()
        self.log(f"   [panel] aperto{' edit' if self.edit else (' recupero' if self.recover else '')} "
                 f"stile={self.style_name} "
                 f"scala={self.scale:.2f} dpi={dpi} caret={rect} font={self.font_name} "
                 f"box={self._box}")

    def _caret(self, target_hwnd, budget=None):
        if self.anchor_override:
            x, y, h = self.anchor_override
            return (x, y, 2, h)
        try:
            if budget is None:
                return caret_mod.get_caret_rect(target_hwnd)
            return caret_mod.get_caret_rect(target_hwnd, budget)
        except Exception as e:
            self.log(f"   [panel] caret: {e}")
            return None

    def close(self, pasted=True):
        """Uscita animata. In "inserted" e "cancelled" la card si chiude da sola a tempo:
        una close() che arriva in quel momento non la taglia."""
        if self.phase in ("closed", "closing"):
            return
        if self.mode in ("inserted", "recovered", "cancelled"):
            return
        self._pasted = pasted
        self.phase = "closing"
        self._t_phase = self.clock()

    def is_open(self):
        return self.phase != "closed"

    def holds_hud(self):
        """True finche' la card e' a schermo (anche in uscita): wavetype.py nasconde l'HUD.
        La card e' l'unico indicatore durante la dettatura; l'HUD torna a card sparita."""
        return self.phase in ("opening", "live", "closing")

    # ---------------------------------------------------------- fasi e livello (contratto)
    def set_phase(self, phase):
        phase = (phase or "").strip().lower()
        if self.edit:
            return self._edit_phase(phase)
        if phase not in PHASES:
            self.log(f"   [panel] fase sconosciuta: {phase!r}")
            return
        if self.phase == "closed" or phase == self.mode:
            return
        now = self.clock()
        if phase in ("formatting", "inserted", "cancelled", "recovering", "recovered",
                     "no_audio", "unrecovered"):
            if self._t_freeze is None:
                self._t_freeze = now
        elif phase in ("listening", "live", "paused", "offline"):
            self._t_freeze = None
        if phase == "formatting":
            self._t_fmt = now
        if phase in ("inserted", "recovered"):
            self._ghost = self._last
            self._pill_at = self._caret_after_paste()
            if not self._n_words:
                self._n_words = sum(1 for w in self.words if w.state == "live")
        if phase in ("inserted", "recovered", "cancelled") and self.phase == "closing":
            self.phase = "live"                  # una close() arrivata prima: la fase vince
        self.mode = phase
        self._t_mode = now

    def set_level(self, x):
        """Livello voce 0..1. Costa un confronto: la storia si campiona nel frame."""
        try:
            x = float(x)
        except Exception:
            return
        if x > self._lv_peak:
            self._lv_peak = x if x < 1.0 else 1.0

    # ---------------------------------------------------------- Edit Mode (contratto)
    def _edit_phase(self, phase):
        """set_phase() in Edit Mode. Le fasi stanno in EDIT_PHASES; dentro portano il prefisso
        "e_" (nessuna cache e nessun ramo della dettatura le vede)."""
        if phase not in EDIT_PHASES:
            self.log(f"   [panel] fase Edit sconosciuta: {phase!r}")
            return
        mode = "e_" + phase
        if self.phase == "closed" or mode == self.mode:
            return
        now = self.clock()
        if mode in ("e_rewriting", "e_done", "e_cancelled") and self._t_freeze is None:
            self._t_freeze = now
        if mode == "e_rewriting":
            self._t_fmt = now
        if mode == "e_done":
            self._ghost = self._last
            self._pill_at = self._caret_after_paste()
        if mode in ("e_done", "e_cancelled") and self.phase == "closing":
            self.phase = "live"            # una close() arrivata prima: l'esito vince
        self.mode = mode
        self._t_mode = now

    def set_chip(self, key):
        """Tasto 1-3 (o l'id di un chip): il comando e' scelto a mano e l'istruzione detta non
        lo cambia piu'. None = torna al riconoscimento automatico. Torna l'id scelto, o None."""
        if key is None:
            self.chip, self._chip_forced = None, False
            return None
        c = ec.by_key(key) or ec.by_id(key)
        if c is None:
            self.log(f"   [panel] chip sconosciuto: {key!r}")
            return None
        self.chip, self._chip_forced = c.id, True
        return c.id

    def set_result(self, words=0, chip=None):
        """Esito della riscrittura: parole del testo nuovo (e, se serve, il chip da nominare
        nella pillola). Si chiama prima di set_phase("done")."""
        try:
            self._res_words = max(0, int(words or 0))
        except Exception:
            self._res_words = 0
        if chip is not None:
            self.set_chip(chip)

    def instruction(self):
        """L'istruzione detta finora (committed + tentative), come la vede la card."""
        return self._instr

    # -- dati per gli stili (Edit)
    def edit_sub(self):
        """Il testo piccolo accanto all'etichetta della testata."""
        if self.mode == "e_offline":
            return "offline"
        if self.mode in ("e_listening", "e_rewriting") and self.sel_words:
            n = self.sel_words
            return f"{n} word" + ("" if n == 1 else "s")
        return ""

    def edit_row(self):
        """(chip da disegnare, acceso, tutti spenti, chip unico) per la riga dei comandi."""
        if self.mode in cs.E_CHIPS_ONE:
            return (), None, False, ec.by_id(self.chip)
        if self.mode in cs.E_CHIPS_ALL:
            return ec.CHIPS, self.chip, (self.chip is None and self.has_words()), None
        return (), None, False, None

    def has_words(self):
        return any(w.state == "live" for w in self.words)

    def edit_done_text(self):
        return ec.done_text(self.chip)

    def edit_words_text(self):
        n = self._res_words or self.sel_words
        return f"{n} word" + ("" if n == 1 else "s")

    def preview_style(self, name):
        """Anteprima di 1,6 s sopra il cursore, in "live", con un testo d'esempio e una
        correzione barrata. Non tocca una dettatura a schermo; un open() vero la interrompe."""
        if self.phase != "closed" and not self.preview_name:
            self.set_style(name)                 # dettatura in corso: cambia stile e basta
            return
        self.set_style(name)
        try:
            fg = _user.GetForegroundWindow()
        except Exception:
            fg = None
        self.open(fg)
        now = self.clock()
        self.preview_name = cs.TITLES.get(self.style_name, self.style_name.title())
        self.mode = "live"
        self._pv_t0 = now
        self._pv = [(0.00, "Ciao Marco, ti scrivo per il", "preventivo di martedì"),
                    (0.30, "Ciao Marco, ti scrivo per il preventivo di giovedì…", "")]

    # ---------------------------------------------------------- diff a parole
    def update(self, snap):
        """Riceve lo snapshot del motore e calcola DA SOLO il diff a parole."""
        try:
            if self.phase == "closed":
                return
            rev = snap.get("rev")
            if rev is not None and rev == self._rev:
                return                       # niente di nuovo: non si riavvia nessuna animazione
            self._rev = rev
            ten_all = (snap.get("tentative") or "").split()
            com_all = (snap.get("committed") or "").split()
            self._n_words = len(com_all) + len(ten_all)
            if self.edit:
                self._instr = " ".join(com_all + ten_all)
                if not self._chip_forced:
                    self.chip = ec.match(self._instr)      # conservativo: vedi edit_chips.match
                self._apply([(t, False) for t in com_all] + [(t, True) for t in ten_all])
                return
            com = com_all[-max(1, KEEP_WORDS - len(ten_all)):]
            # le parole cadute dalla testa sono gia' sopra al corpo visibile (tagliate fuori):
            # spariscono senza barrato a vista, il costo resta piatto su ogni dettatura
            target = [(t, False) for t in com] + [(t, True) for t in ten_all]
            if target and self.mode == "listening":
                self.mode = "live"               # prima parola: la card si apre in piena
                self._t_mode = self.clock()
            self._apply(target)
        except Exception as e:
            self.log(f"   [panel] update: {e}")

    def _apply(self, target):
        now = self.clock()
        alive = [w for w in self.words if w.state == "live"]
        pre, buf, ai = {}, [], 0
        for w in self.words:
            if w.state == "gone":
                buf.append(w)
            else:
                pre[ai] = buf
                buf = []
                ai += 1
        tail = buf

        a = [w.norm for w in alive]
        b = [_norm(t) for t, _ in target]
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        out = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    w = alive[i1 + k]
                    out.extend(pre.get(i1 + k, ()))
                    txt, tent = target[j1 + k]
                    if w.text != txt:
                        w.retext(txt)        # punteggiatura/maiuscola: cambia sul posto
                    if w.tentative and not tent:
                        w.fixed_at = now
                    w.tentative = tent
                    out.append(w)
            else:
                for k in range(i1, i2):      # sparite o sostituite -> si barrano
                    w = alive[k]
                    out.extend(pre.get(k, ()))
                    w.state = "gone"
                    w.gone_at = now
                    w.chunks = None          # Glyph: il barrato cambia carattere, si rimisura
                    out.append(w)
                for k in range(j1, j2):      # nuove -> entrano in dissolvenza
                    txt, tent = target[k]
                    out.append(_Word(txt, tent, now))
        out.extend(tail)
        self.words = out

    # ---------------------------------------------------------- dati per gli stili
    def timer_text(self, pad=False):
        if self.recover:
            return cs.fmt_time(self._rec_dur, pad)
        end = self._t_freeze if self._t_freeze is not None else self.clock()
        return cs.fmt_time(end - self._t0, pad)

    def count_text(self):
        """Conteggio parole: solo nelle dettature lunghe (oltre le 3 righe visibili)."""
        if self._n_lines > MAX_LINES and self._n_words:
            return f"{self._n_words} w"
        return ""

    def words_total(self):
        return self._n_words or sum(1 for w in self.words if w.state == "live")

    def levels(self, n):
        lv = self._lv
        return [lv[i] for i in range(len(lv) - n, len(lv))]

    def level_track(self, n):
        arr = np.fromiter((self._lv[i] for i in range(len(self._lv) - n, len(self._lv))), np.float32, n)
        frac = _clamp((self.clock() - self._lv_t) / LV_DT, 0.0, 1.0)
        return arr, frac

    def progress(self):
        if self.mode in ("inserted", "recovered"):
            return 1.0
        t = self.clock() - self._t_fmt
        return 0.94 * (1.0 - math.exp(-t / 1.0))

    # ---------------------------------------------------------- tempo che passa
    def _auto(self, now):
        """Storia del livello, copione dell'anteprima, chiusure a tempo."""
        if self.preview_name and self._pv is not None:
            t = now - self._pv_t0
            while self._pv and t >= self._pv[0][0]:
                _, com, ten = self._pv.pop(0)
                self._rev = None
                self.update({"committed": com, "tentative": ten, "rev": -7 - len(self._pv)})
                self.mode = "live"
            self._lv_peak = max(self._lv_peak, 0.25 + 0.55 * abs(math.sin(t * 7.3) * math.sin(t * 2.1 + 1)))
            if t >= T_PREVIEW and self.phase not in ("closing", "closed"):
                self._pasted = False
                self.phase = "closing"
                self._t_phase = now
        steps = 0
        while now - self._lv_t >= LV_DT and steps < LV_KEEP:
            v = self._lv_peak
            self._lv.append(v if v > 0 else 0.0)
            self._lv_peak = 0.0
            self._lv_t += LV_DT
            steps += 1
        if steps >= LV_KEEP:
            self._lv_t = now
        if self.phase in ("opening", "live"):
            if self.mode == "inserted" and now - self._t_mode >= T_INSERTED:
                self._pasted = False
                self.phase = "closing"
                self._t_phase = now
            elif self.mode == "recovered" and now - self._t_mode >= T_RECOVERED:
                self._pasted = False
                self.phase = "closing"
                self._t_phase = now
            elif self.mode == "cancelled" and now - self._t_mode >= T_CANCELLED:
                self._pasted = False
                self.phase = "closing"
                self._t_phase = now

    # ---------------------------------------------------------- animazione
    def _purge(self, now):
        self.words = [w for w in self.words
                      if not (w.state == "gone" and now - w.gone_at > T_HOLD + T_GONE + 0.45)]

    def _occupies(self, w, now):
        """La parola barrata tiene il suo posto finche' NON e' sparita del tutto.
        Prima svanisce, poi il testo si richiude: se le due cose si accavallano si
        vedono le parole passare una sopra l'altra (visto a schermo, 18/09)."""
        return not (w.state == "gone" and now - w.gone_at >= T_HOLD + T_GONE)

    def _layout(self, now):
        x, line = 0.0, 0
        for w in self.words:
            if not self._occupies(w, now):
                continue
            self._measure(w)
            f, tr = self._wfont(w)
            first = self._adv(w.chunks[0][0], f, tr)
            if x > 0 and (w.lines > 1 or x + first > self.text_w):
                line += 1
                x = 0.0
            w.tx, w.ty = x, line * self.line_h
            line += w.lines - 1
            x += w.adv + self.space_w
        return line + 1

    def _alpha(self, w, now):
        if w.state == "gone":
            t = now - w.gone_at
            if t < T_HOLD:
                return 1.0
            return _clamp(1.0 - (t - T_HOLD) / T_GONE, 0.0, 1.0)
        return _clamp((now - w.born) / T_IN, 0.0, 1.0)

    def _compact(self):
        """In Edit la card nasce gia' piena (i chip ci sono da subito): mai compatta."""
        return (not self.edit and self.mode in ("listening", "recovering")
                and not any(w.state == "live" for w in self.words))

    def _zones(self, m=None):
        """(alto, pad_top, pad_bot, basso) in px fisici per la fase corrente."""
        m = self._mq() if m is None else m
        z = self.style.zones_of(self.mode, self._flip, m, self.chip is not None)
        return tuple(v * self.scale for v in z)

    def _mq(self):
        """m a tre livelli (0, mezzo, pieno): la cromia cambia di 3 px per volta mentre la card
        si allarga (invisibile nel movimento) e le lastre di riferimento restano tre."""
        return round(self._m * 2) / 2.0

    def _target_size(self, n_lines=1):
        if self._compact():
            w, h = self.style.compact_size(self, self.mode)
            return w * self.scale, h * self.scale
        top, pt, pb, bot = self._zones(1.0)
        if self.edit and self.mode not in cs.E_SAID:
            return float(self.card_w_max), top + bot       # niente istruzione a schermo
        if self.recover and not self.words:
            return float(self.card_w_max), top + bot       # recupero a vuoto: testata e pie', basta
        vis = max(1, min(n_lines, self._maxl))
        return float(self.card_w_max), top + pt + vis * self.line_h + pb + bot

    def _step(self, now):
        dt = 0.0 if self._last_t is None else min(0.25, now - self._last_t)
        self._last_t = now
        self._purge(now)
        n_lines = self._layout(now) if self.words else 1
        self._n_lines = n_lines
        kg = _ease(dt, TAU_GLIDE)
        for w in self.words:
            if not w.placed:
                w.x, w.y, w.placed = w.tx, w.ty, True
            elif self._occupies(w, now):
                w.x += (w.tx - w.x) * kg
                w.y += (w.ty - w.y) * kg
        ks = _ease(dt, TAU_SIZE)
        tgt_m = 0.0 if self._compact() else 1.0
        self._m += (tgt_m - self._m) * ks
        if abs(self._m - tgt_m) < 0.02:
            self._m = tgt_m
        tgt_w, tgt_h = self._target_size(n_lines)
        tgt_scroll = max(0.0, (n_lines - self._maxl) * self.line_h)
        self._card_h += (tgt_h - self._card_h) * ks
        self._card_w += (tgt_w - self._card_w) * ks
        if abs(tgt_h - self._card_h) < 1.5:       # arrivati: misura esatta (e lastra in cache)
            self._card_h = tgt_h
        if abs(tgt_w - self._card_w) < 1.5:
            self._card_w = tgt_w
        self._scroll += (tgt_scroll - self._scroll) * _ease(dt, TAU_SCROLL)
        return n_lines

    # ---------------------------------------------------------- disegno
    def _sprite(self, text, f, tr, color, alpha):
        """Bitmap della parola gia' colorata: e' la cache che tiene tick() sotto il ms."""
        ab = int(_clamp(alpha, 0, 1) * 16 + 0.5)
        key = (text, id(f), tr, color, ab)
        sp = self._sprites.get(key)
        if sp is None:
            m, _adv = cs.text_mask(text, f, tr)
            if ab < 16:
                m = m.point(lambda v, a=ab / 16.0: int(v * a))
            sp = Image.new("RGBA", m.size, tuple(color) + (0,))
            sp.putalpha(m)
            self._sprites[key] = sp
            if len(self._sprites) > 900:
                self._sprites.clear()
        return sp

    def _over_mask(self, layer):
        """Dettatura lunga: le righe vecchie salgono e sfumano sotto la maschera in cima al
        corpo (la stessa mask-image del mockup di ogni stile). Entra piano con lo scorrimento."""
        s = _clamp(self._scroll / max(1.0, self.line_h), 0.0, 1.0)
        if s <= 0.01:
            return layer
        h = layer.height
        key = ("over", self.style_name, self.scale, h, round(s, 2))
        g = self._sprites.get(key)
        if g is None:
            pts = self.style.over_mask
            ys = np.arange(h, dtype=np.float32) / self.scale
            a = np.interp(ys, [p[0] for p in pts], [p[1] for p in pts]).astype(np.float32)
            g = 1.0 - s * (1.0 - a)
            self._sprites[key] = g
        arr = np.asarray(layer).copy()
        arr[:, :, 3] = (arr[:, :, 3] * g[:, None]).astype(np.uint8)
        return Image.fromarray(arr, "RGBA")

    def _word_color(self, w, idx, split):
        st = self.style
        if self.edit:
            if w.state == "gone":
                return st.ec_gone
            return st.ec_tent if w.tentative else st.ec_commit
        if w.state == "gone":
            return st.c_gone
        if self.mode == "cancelled":
            return st.c_cancel
        if self.mode == "formatting":
            if st.c_after is not None:
                return st.c_commit if idx < split else st.c_after
            if st.name == "glyph":
                return st.c_commit
        return st.c_tent if w.tentative else st.c_commit

    def _text_layer(self, cw, bh, pad_top, now):
        layer = Image.new("RGBA", (cw, max(1, bh)), (0, 0, 0, 0))
        over = []                                     # barrati: sopra alle parole
        st = self.style
        base_y = pad_top - self._scroll
        y_glyph = (self.line_h - self.glyph_h) / 2.0
        live = [w for w in self.words if w.state == "live"]
        split = int(round(len(live) * self.progress())) if self.mode == "formatting" else 0
        hl = None
        if st.name == "stamp" and self.mode == "live":
            fixed = [w for w in live if not w.tentative and w.fixed_at >= 0]
            if fixed and now - fixed[-1].fixed_at < T_FIXED:
                hl = fixed[-1]
        last = None
        idx = -1
        for w in self.words:
            if w.state == "live":
                idx += 1
            a = self._alpha(w, now)
            if self._occupies(w, now):
                last = w
            if a <= 0.01:
                continue
            col = self._word_color(w, idx, split)
            f, tr = self._wfont(w)
            asc, desc = f.getmetrics()
            gh = asc + desc
            rise = (1.0 - min(1.0, (now - w.born) / T_IN)) * self._px(4) if w.state == "live" else 0.0
            self._measure(w)
            if w is hl:
                t = now - w.fixed_at
                ha = a * _clamp((T_FIXED - t) / 0.35, 0.0, 1.0)
                for text, dx, dl in w.chunks:
                    st.highlight(layer, self.text_x + w.x + dx, base_y + w.y + dl * self.line_h + y_glyph + rise,
                                 self._adv(text, f, tr), self.glyph_h, ha)
            for text, dx, dl in w.chunks:
                px = self.text_x + w.x + dx
                py = base_y + w.y + dl * self.line_h + (self.line_h - gh) / 2.0 + rise
                if py > bh or py + gh < 0:
                    continue
                cs.blit(layer, self._sprite(text, f, tr, col, a), px, py)
                if self.mode == "cancelled" and w.state == "live" and hasattr(st, "cancel_line"):
                    over.append(("cancel", px, base_y + w.y + dl * self.line_h + y_glyph + self.glyph_h * 0.56,
                                 self._adv(text, f, tr), a))
            if w.state == "gone":
                p = _clamp((now - w.gone_at) / T_STRIKE, 0.0, 1.0)
                p = 1 - (1 - p) * (1 - p)            # ease-out: parte svelta, si posa
                for text, dx, dl in w.chunks:
                    over.append(("strike", self.text_x + w.x + dx, base_y + w.y + dl * self.line_h,
                                 self._adv(text, f, tr), p, a))
        for o in over:
            if o[0] == "strike":
                _, x, ly, wd, p, a = o
                st.strike(layer, x, ly, wd, p, round(a * 8) / 8.0)
            else:
                _, x, cy, wd, a = o
                st.cancel_line(layer, x, cy, wd, a)
        if self.mode == "offline" and last is not None:       # la coda si ferma: "…"
            self._measure(last)
            dots = "···" if st.name == "glyph" else "…"
            tr = self._px(16.5 * 0.2) if st.name == "glyph" else 0.0
            x = self.text_x + last.x + last.adv + self.space_w
            y = base_y + last.y + (last.lines - 1) * self.line_h + y_glyph
            if x + self._adv(dots, self.font, tr) > self.text_x + self.text_w:
                x, y = self.text_x, y + self.line_h
            cs.blit(layer, self._sprite(dots, self.font, tr, st.c_tent, 1.0), x, y)
        return layer

    FOOTER_MODES = ("paused", "cancelled", "offline")

    def _max_h(self):
        return self.style.edit_max_height() if self.edit else self.style.max_height()

    def _ref(self, mode, m, build=True):
        """Lastra di riferimento per (stile, fase, lato, m): larga quanto la card piena, alta
        quanto la misura massima. Costa 15-30 ms (misurato): si costruisce una volta sola."""
        key = (self.style_name, self.scale, mode, self._flip, m)
        ref = self._refs.get(key)
        if ref is None and build:
            href = int(math.ceil(self._max_h() * self.scale)) + 8
            kw = {"tex": False} if hasattr(self.style, "texture_layer") else {}
            if len(self._refs) > 40:
                self._refs.clear()
            ref = self._refs[key] = self.style.plate(self, mode, self.card_w_max, href, self._flip, m, **kw)
        return ref

    def _plate(self, cw, ch, m):
        """Lastra della card (ombra, fondo, testata, pie') per questa misura.

        Costruirla da zero costa 15-30 ms (misurato), e la card cambia misura a ogni riga nuova
        e mentre si apre: la si ricompone a 9 fette da una lastra di riferimento (_ref). Angoli
        copiati, bordi e centro ripetuti (il corpo e' uniforme; in orizzontale si ripete con il
        passo del tratteggio di Glyph). La texture a punti di Glyph si posa dopo, intera,
        ancorata in alto come nel CSS."""
        mode = self.mode if not self.preview_name else "live"
        key = (self.style_name, self.scale, mode, cw, ch, self._flip, m)
        pl = self._plates.get(key)
        if pl is not None:
            return pl
        M = self.margin
        top, _pt, _pb, bot = self._zones(m)
        r = self.style.radius * self.scale
        # le fette laterali devono contenere tutta la sfumatura dell'ombra (che entra sotto la
        # card per spread + 3 sigma): se no il centro ripetuto e il bordo non combaciano
        reach = getattr(self.style, "shadow_reach", 0) * self.scale
        dy = getattr(self.style, "shadow_dy", 0) * self.scale
        ts = int(M + max(top + r, reach) + 6)
        bs = int(M + max(bot + r, reach + dy) + 6)
        ls = rs = int(M + max(r, reach) + 6)
        W, H = cw + 2 * M, ch + 2 * M
        ref = self._ref(mode, m)
        tex = hasattr(self.style, "texture_layer")
        pl = None
        if cw == self.card_w_max or mode not in self.FOOTER_MODES:
            pw = max(1, int(getattr(self.style, "h_period", lambda: 1)()))
            pl = cs.slice9(ref, W, H, ls, rs, ts, bs, pw)
        if pl is None:
            pl = self.style.plate(self, mode, cw, ch, self._flip, m)
        elif tex:
            pl.alpha_composite(self.style.texture_layer(cw, ch, m), (M, M))
        if len(self._plates) > 64:
            self._plates.clear()
        self._plates[key] = pl
        return pl

    def _prewarm(self, budget_ms):
        """Costruisce in anticipo UNA lastra di riferimento che servira' fra poco (apertura
        piena, pausa, formattazione, annullo), solo se il frame appena disegnato era leggero:
        cosi' il cambio di fase non paga i 15-30 ms nel suo frame."""
        if budget_ms <= 0 or self.phase not in ("opening", "live"):
            return
        todo = ([("e_rewriting", 1.0), ("e_cancelled", 1.0), ("e_offline", 1.0),
                 ("e_nothing", 1.0)] if self.edit else
                [("live", 0.5), ("live", 1.0), ("paused", 1.0), ("formatting", 1.0),
                 ("cancelled", 1.0), ("offline", 1.0)])
        for mode, m in todo:
            if self._ref(mode, m, build=False) is None:
                t0 = time.perf_counter()
                self._ref(mode, m)
                self.log_prewarm.append((mode, m, round((time.perf_counter() - t0) * 1000, 1)))
                if len(self.log_prewarm) > 50:
                    del self.log_prewarm[:25]
                return

    def _card(self, now):
        # misure arrotondate a 2 px: la crescita resta fluida, le cache reggono
        cw = max(8, int(round(self._card_w)) // 2 * 2)
        ch = max(8, int(round(self._card_h)) // 2 * 2)
        if abs(self._card_w - self.card_w_max) < 1.5:
            cw = self.card_w_max
        m = self._mq()
        mode = self.mode if not self.preview_name else "live"
        img = self._plate(cw, ch, m).copy()
        M = self.margin
        top, pt, pb, bot = self._zones(m)
        bh = int(ch - top - bot)
        if bh > 2 and m > 0.05:
            layer = self._over_mask(self._text_layer(cw, bh, pt, now))
            if m < 0.999:
                arr = np.asarray(layer).copy()
                arr[:, :, 3] = (arr[:, :, 3] * m).astype(np.uint8)
                layer = Image.fromarray(arr, "RGBA")
            img.alpha_composite(layer, (M, M + int(round(top))))
        front = self.style.edit_front if self.edit else self.style.front
        front(self, img, M, M, mode, cw, ch, self._flip, m, now)
        return img

    # ---------------------------------------------------------- frame
    def _render(self):
        """(img, x, y) del frame, o None se la card e' chiusa."""
        now = self.clock()
        self._auto(now)
        if self.phase == "closed":
            return None
        if self.mode in ("inserted", "e_done"):
            return self._render_pill(now)
        self._step(now)
        img = self._card(now)
        x, y = self._place(*img.size)
        self._last = (img, x, y)
        img = self._transition(img, now)
        return img, x, y

    def _transition(self, img, now):
        if self.phase == "opening":
            p = _clamp((now - self._t_phase) / T_OPEN, 0, 1)
            if p >= 1.0:
                self.phase = "live"
            else:
                img = self._scaled(img, 0.955 + 0.045 * p, _smooth(p))
        elif self.phase == "closing":
            dur = T_CLOSE_PASTE if self._pasted else T_CLOSE_ESC
            p = _clamp((now - self._t_phase) / dur, 0, 1)
            if self._pasted:                 # il testo se ne va verso il cursore
                img = self._scaled(img, 1.0 - 0.70 * p, (1.0 - p) ** 1.5, to_caret=True)
            else:                            # si dissolve e basta
                img = self._scaled(img, 1.0 - 0.03 * p, (1.0 - p) ** 1.4)
            if p >= 1.0:
                self.phase = "closed"
        return img

    def render_frame(self):
        """Lo stesso disegno, senza finestra: serve ai test e ai PNG."""
        r = self._render()
        if r is None:
            return Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        return r[0]

    def _caret_after_paste(self):
        """Dove sta il cursore dopo l'incolla (in fondo al testo appena scritto): la pillola
        di conferma ci si appoggia sopra. Budget stretto: siamo sul loop principale."""
        try:
            with dpi_scope():
                r = self._caret(self._target, budget=0.08)
        except Exception:
            r = None
        if r:
            return (r[0], r[1], r[1] + max(r[3], int(self._px(16))))
        if self._anchor:
            cx, cy, h = self._anchor
            return (cx, cy, cy + max(h, int(self._px(16))))
        return None

    def _render_pill(self, now):
        if self.edit:
            key = (self.style_name, self.scale, "e", self.chip, self.edit_words_text())
            build = self.style.edit_pill
        else:
            key = (self.style_name, self.scale, self.words_total(), self.timer_text())
            build = self.style.pill
        pill = self._pill_cache.get(key)
        if pill is None:
            self._pill_cache.clear()
            pill = self._pill_cache[key] = build(self)
        M = self.margin
        pw, ph = pill.width - 2 * M, pill.height - 2 * M
        wl, wt, wr, wb = self._work
        e = int(self._px(EDGE))
        gap = int(self._px(CARET_GAP))
        if self._pill_at:
            cx, ctop, cbot = self._pill_at
            px = cx - pw // 2
            py = ctop - gap - ph if self._flip else cbot + gap
        elif self._box:
            l, t, b, _f, _c = self._box
            px, py = l, (b - ph if self._flip else t)
        else:
            px, py = (wl + wr - pw) // 2, wb - ph - int(self._px(56))
        px = int(_clamp(px, wl + e, max(wl + e, wr - e - pw)))
        py = int(_clamp(py, wt + e, max(wt + e, wb - e - ph)))
        t = now - self._t_mode
        p = _smooth(t / 0.16)
        img = pill
        if p < 1.0:
            img = self._scaled(pill, 0.92 + 0.08 * p, p)
        img = self._transition(img, now)
        x0, y0 = px - M, py - M
        g = self._ghost
        if g is not None and t < T_GHOST:
            gimg, gx, gy = g
            q = _clamp(t / T_GHOST, 0.0, 1.0)
            ghost = self._scaled(gimg, 1.0 - 0.06 * q, (1.0 - q) ** 1.5, to_caret=True)
            L, T = min(x0, gx), min(y0, gy)
            R = max(x0 + img.width, gx + gimg.width)
            B = max(y0 + img.height, gy + gimg.height)
            out = Image.new("RGBA", (R - L, B - T), (0, 0, 0, 0))
            out.alpha_composite(ghost, (gx - L, gy - T))
            out.alpha_composite(img, (x0 - L, y0 - T))
            return out, L, T
        return img, x0, y0

    def _scaled(self, img, s, a, to_caret=False):
        """Ridimensiona e applica un'opacita' globale.
        `to_caret`: collassa verso l'angolo dove sta il cursore (in basso a sinistra se la card
        e' sopra al caret, in alto a sinistra se e' sotto)."""
        W, H = img.size
        if s < 0.999:
            sw, sh = max(1, int(W * s)), max(1, int(H * s))
            small = img.resize((sw, sh), Image.BILINEAR)
        else:
            sw, sh, small = W, H, img
        if a < 0.999:
            f = _clamp(a, 0, 1)
            small = small.copy() if small is img else small
            small.putalpha(small.getchannel("A").point(lambda v, f=f: int(v * f)))
        if sw == W and sh == H:
            return small
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ax = 0 if to_caret else (W - sw) // 2
        ay = (H - sh) if self._flip else 0      # cresce/collassa dal lato ancorato
        out.paste(small, (ax, ay))
        return out

    # ---------------------------------------------------------- posizione
    def _find_hud(self):
        """L'HUD si fa da parte mentre la card e' a schermo (holds_hud): niente da evitare.
        Resta la possibilita' di imporne uno nei test (hud_override)."""
        r = None if self.hud_override in (None, "none") else self.hud_override
        if not r:
            return None
        g = int(self._px(HUD_GAP))
        top = r[1] + int((r[3] - r[1]) * HUD_TOP_INSET.get(_skin(), 0.0))
        return (r[0] - g, top - g, r[2] + g, r[3] + g)

    def _plan(self):
        """Decide UNA volta per dettatura dove vive la card, misurandola alla sua misura
        MASSIMA (larga card_w_max, alta 3 righe + pie'): cosi' mentre cresce non puo'
        finire sulla riga del cursore o fuori schermo, e non salta da un posto all'altro.
        Preferita: sopra al caret di CARET_GAP, testo allineato al caret. Se sopra non c'e'
        posto: sotto (Signal gira l'oscilloscopio verso il caret)."""
        self.card_w_max = int(round(self.style.card_w * self.scale))
        wl, wt, wr, wb = self._work
        e = int(self._px(EDGE))
        W = min(self.card_w_max, max(8, wr - wl - 2 * e))
        H = int(math.ceil(self._max_h() * self.scale))
        gap = int(self._px(CARET_GAP))
        hud = self._hud
        fit_min = min(W, int(self._px(FIT_MIN_W)))

        def hits(r, o):
            return o is not None and r[0] < o[2] and o[0] < r[2] and r[1] < o[3] and o[1] < r[3]

        line = None
        if self._anchor:
            cx, cy, chh = self._anchor
            line = (wl, cy, wr, cy + max(chh, int(self._px(16))))

        def ok(c):
            l, t, w = c[0], c[1], c[2]
            r = (l, t, l + w, t + H)
            return (l >= wl + e and l + w <= wr - e and t >= wt + e and t + H <= wb - e
                    and not hits(r, hud) and not hits(r, line))

        def clampx(l, w):
            return int(_clamp(l, wl + e, max(wl + e, wr - e - w)))

        cands = []
        if self._anchor:
            cx, cy, chh = self._anchor
            below = cy + max(chh, int(self._px(16))) + gap
            above = cy - gap - H
            sides = [(above, True), (below, False)]
            if above < wt + e:
                sides.reverse()
            ideal = int(cx - self.text_x)             # la colonna del testo sopra al caret
            for t, flip in sides:
                cands.append((clampx(ideal, W), t, W, flip, None))
                if hud is not None:
                    w = int(_clamp(hud[0] - ideal, fit_min, W))
                    cands.append((clampx(min(ideal, hud[0] - w), w), t, w, flip, None))
                    l0 = max(ideal, hud[2])
                    w = int(_clamp(wr - e - l0, fit_min, W))
                    cands.append((clampx(l0, w), t, w, flip, None))
        mid = (wl + wr) // 2
        cands.append((clampx(mid - W // 2, W), wb - H - int(self._px(56)), W, True, None))

        def cost(c):
            if not self._anchor:
                return 0.0
            l, t, w, flip, _ = c
            cx, cy, _h = self._anchor
            ay = t + H if flip else t
            dx = 0.0 if l <= cx <= l + w else min(abs(cx - l), abs(cx - l - w))
            return math.hypot(dx + abs(l - (cx - self.text_x)) * 0.25, ay - cy) + (W - w) * 0.5

        valid = [c for c in cands if ok(c)]
        pick = min(valid, key=cost) if valid else cands[0]
        l, t, w, flip, center = pick
        t = int(_clamp(t, wt + e, max(wt + e, wb - e - H)))
        self._box = (l, t, t + H, flip, center)
        if flip != self._flip:
            self._plates.clear()
        self._flip = flip
        if w != self.card_w_max:
            self.card_w_max = w
        self.text_w = self.card_w_max - self.text_x - self.pad_r
        for wd in self.words:
            wd.chunks = None

    def _place(self, img_w, img_h):
        """Posizione della finestra per QUESTO frame, dentro la scatola decisa da _plan().
        Sopra al caret: attaccata al bordo basso (cresce verso l'alto). Sotto: al bordo alto."""
        cw = img_w - 2 * self.margin
        ch = img_h - 2 * self.margin
        if self._box:
            l, t, b, flip, center = self._box
            top = b - ch if flip else t
            if center is not None:
                l = int(_clamp(center - cw // 2, l, l + self.card_w_max - cw))
            return l - self.margin, top - self.margin
        wl, wt, wr, wb = self._work
        return (wl + wr) // 2 - cw // 2 - self.margin, wb - ch - int(self._px(56)) - self.margin

    # ---------------------------------------------------------- finestra
    def _ensure_window(self):
        if self.hwnd:
            return
        import win32gui
        import win32con
        import win32api
        hinst = win32api.GetModuleHandle(None)
        cls = "WavetypeLivePanel"
        try:
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = cls
            wc.hInstance = hinst
            wc.lpfnWndProc = lambda h, m, w, l: win32gui.DefWindowProc(h, m, w, l)
            win32gui.RegisterClass(wc)
        except Exception:
            pass
        ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW |
              win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TRANSPARENT)
        # La finestra prende la consapevolezza DPI del thread che la CREA: fuori da dpi_scope
        # nascerebbe "unaware" e Windows moltiplicherebbe per 1,5 (al 150%) posizione e misura
        # passate in pixel fisici: card stirata e spinta fuori schermo (foto e2e del 19/09).
        with dpi_scope():
            self.hwnd = win32gui.CreateWindowEx(ex, cls, "Wavetype live", win32con.WS_POPUP,
                                                0, 0, 8, 8, 0, 0, hinst, None)
        self._shown = False

    def _show(self, v):
        import win32gui
        import win32con
        if v and not self._shown:
            win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)
            self._shown = True
        elif not v and self._shown:
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            self._shown = False

    def tick(self):
        """Un frame. True finche' il pannello e' vivo; False quando ha finito di chiudersi."""
        if self.phase == "closed":
            if self._shown:
                self._show(False)
            return False
        t0 = time.perf_counter()
        try:
            with dpi_scope():
                r = self._render()
                if r is None or self.phase == "closed":
                    self._show(False)
                    self.last_frame_ms = (time.perf_counter() - t0) * 1000
                    return False
                img, x, y = r
                self._paint(x, y, img)
                self._show(True)
                self.last_frame_ms = (time.perf_counter() - t0) * 1000
                if self.last_frame_ms < 9.0:
                    self._prewarm(33.0 - self.last_frame_ms)
        except Exception as e:
            self.log(f"   [panel] tick: {e}")
        self.last_frame_ms = (time.perf_counter() - t0) * 1000
        return self.phase != "closed"

    def _paint(self, x, y, img):
        """UpdateLayeredWindow: alpha per-pixel vero, come paint_layered in wavetype.py.
        Sposta e ridimensiona la finestra nello stesso colpo."""
        w, h = img.size
        # BGRA premoltiplicato direttamente dall'impacchettatore di Pillow ("BGRa"): 1,9 ms
        # contro 12 ms della stessa conversione in numpy (misurato su 836x286, 19/09)
        buf = img.tobytes("raw", "BGRa")
        screen = _user.GetDC(None)
        mem = hbmp = old = None
        # try/finally: se una chiamata solleva a meta' strada, DC e bitmap vanno restituiti lo
        # stesso. Senza, ogni frame fallito lasciava due DC appesi (misurato: 5.057 oggetti GDI
        # in un giro del banco e2e, contro un tetto di 10.000 per processo).
        try:
            mem = _gdi.CreateCompatibleDC(screen)
            bmi = _BMIH()
            bmi.biSize = ctypes.sizeof(_BMIH)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bits = ctypes.c_void_p()
            hbmp = _gdi.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            ctypes.memmove(bits, buf, len(buf))
            old = _gdi.SelectObject(mem, hbmp)
            blend = _BLEND(0, 0, 255, 1)
            _user.UpdateLayeredWindow(self.hwnd, screen,
                                      ctypes.byref(wintypes.POINT(int(x), int(y))),
                                      ctypes.byref(_SIZE(w, h)), mem,
                                      ctypes.byref(wintypes.POINT(0, 0)), 0,
                                      ctypes.byref(blend), 2)
        finally:
            if mem is not None:
                if old:
                    _gdi.SelectObject(mem, old)
                if hbmp:
                    _gdi.DeleteObject(hbmp)
                _gdi.DeleteDC(mem)
            _user.ReleaseDC(None, screen)

    def destroy(self):
        try:
            if self.hwnd:
                import win32gui
                win32gui.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.hwnd = None
        self.phase = "closed"
