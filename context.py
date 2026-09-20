"""context.py — il cursore e' a meta' di una frase gia' scritta, o ne inizia una nuova?

Serve a incollare come scriverebbe l'utente: se il punto di inserimento sta dentro una
frase aperta, il testo dettato non comincia con la maiuscola e vuole uno spazio davanti.

Niente esce dal PC e niente viene digitato: si chiede all'albero di accessibilita' il
punto di inserimento e si legge SOLO il pezzo che lo precede (al massimo LOOKBACK
caratteri, il paragrafo corrente), poi si guarda l'ultimo carattere.

Catena di ripiego (misurata il 2026-09-20, sonde in tests/bench/context/):
  1. UIA TextPattern: caret range allargato indietro di un paragrafo.
     Chrome/Electron (Slack, VS Code) rispondono: 6-10 ms a regime, ~290 ms il primo
     colpo (e' l'app che sveglia il suo albero, non noi).
  2. Campi Edit Win32 classici: non hanno TextPattern (misurato), ma hanno
     ValuePattern; la posizione del cursore arriva da EM_GETSEL, che risponde anche
     da un altro processo.
  3. None: non si sa -> chi chiama si comporta come prima (maiuscola).
"""
import ctypes
import time

try:
    import caret as caret_mod          # riusa l'oggetto UIA e il prewarm del caret
except Exception:
    caret_mod = None

LOOKBACK = 120                 # caratteri massimi letti prima del cursore
BUDGET_S = 1.500               # largo apposta: la lettura gira sempre su un thread suo mentre
                               # parli, e il PRIMO colpo su Chrome/Electron costa ~300 ms (e'
                               # l'app che sveglia il suo albero di accessibilita', misurato in
                               # tests/bench/context/). Col budget a 300 ms la prima dettatura
                               # della giornata perdeva il contesto.
EM_GETSEL = 0x00B0

# la frase e' chiusa se prima del cursore c'e' uno di questi
CLOSERS = ".!?:;…"
# parentesi e virgolette chiuse: si guarda il carattere prima
SKIP_BACK = ')"»”’\'`]'
# dopo questi non si mette lo spazio davanti al testo incollato
NO_SPACE_AFTER = ' \t\n\r([{«"“„\'`-–—/'

_LOG = [print]


def set_log(fn):
    _LOG[0] = fn


def log(msg):
    try:
        _LOG[0](msg)
    except Exception:
        pass


def prewarm():
    """Il typelib UIA e' lento la prima volta: lo scalda caret.prewarm() all'avvio."""
    if caret_mod is not None:
        try:
            caret_mod.prewarm()
        except Exception:
            pass


def _uia():
    if caret_mod is None or not caret_mod._uia_ready():
        return None, None
    return caret_mod._uia["obj"], caret_mod._uia["mod"]


def _from_textpattern(el, U):
    """Paragrafo che precede il cursore. '' e' una risposta valida (campo vuoto)."""
    rng = None
    try:
        pat = el.GetCurrentPattern(U.UIA_TextPattern2Id)
        if pat:
            tp2 = pat.QueryInterface(U.IUIAutomationTextPattern2)
            res = tp2.GetCaretRange()
            rng = res[-1] if isinstance(res, tuple) else res
    except Exception:
        rng = None
    if rng is None:
        try:
            pat = el.GetCurrentPattern(U.UIA_TextPatternId)
            if pat:
                tp = pat.QueryInterface(U.IUIAutomationTextPattern)
                sel = tp.GetSelection()
                if sel and sel.Length > 0:
                    rng = sel.GetElement(0)
        except Exception:
            return None
    if rng is None:
        return None
    try:
        c = rng.Clone()
        c.MoveEndpointByUnit(U.TextPatternRangeEndpoint_Start, U.TextUnit_Paragraph, -1)
        txt = c.GetText(LOOKBACK)
    except Exception:
        return None
    if txt is None:
        return None
    # il paragrafo puo' sconfinare fuori dal campo quando il campo e' VUOTO: allora si
    # leggerebbe l'etichetta accanto ("Scrivi qui", "Messaggio a #canale") e si crederebbe di
    # stare a meta' di una frase. Se il testo letto non nasce nello stesso elemento del
    # cursore, il campo e' vuoto: frase nuova.
    try:
        obj = caret_mod._uia["obj"]
        a, b = rng.GetEnclosingElement(), c.GetEnclosingElement()
        if a and b and not obj.CompareElements(a, b):
            return ""
    except Exception:
        pass
    return txt


def _from_win32_edit(el, U):
    """Campo Edit classico: tutto il testo da ValuePattern, il cursore da EM_GETSEL."""
    try:
        vp = el.GetCurrentPattern(U.UIA_ValuePatternId)
        if not vp:
            return None
        val = vp.QueryInterface(U.IUIAutomationValuePattern).CurrentValue
    except Exception:
        return None
    if val is None:
        return None
    try:
        import win32gui
        import win32process
        fg = win32gui.GetForegroundWindow()
        tid_fg, _ = win32process.GetWindowThreadProcessId(fg)
        tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
        ctypes.windll.user32.AttachThreadInput(tid_me, tid_fg, True)
        try:
            h = win32gui.GetFocus()
        finally:
            ctypes.windll.user32.AttachThreadInput(tid_me, tid_fg, False)
        if not h:
            return None
        sel = win32gui.SendMessage(h, EM_GETSEL, 0, 0)
        if not isinstance(sel, int):
            return None
        start = sel & 0xFFFF
    except Exception:
        return None
    return val[max(0, start - LOOKBACK):start]


def before_caret(hwnd=None, budget=BUDGET_S):
    """Testo che precede il cursore, o None se non si sa (allora si fa come prima)."""
    t0 = time.perf_counter()
    obj, U = _uia()
    if obj is None:
        return None
    if hwnd:                                  # il focus e' globale: se la finestra bersaglio non
        try:                                  # e' quella davanti, leggeremmo un'altra app
            if ctypes.windll.user32.GetForegroundWindow() != hwnd:
                return None
        except Exception:
            pass
    try:
        el = obj.GetFocusedElement()
    except Exception as e:
        log(f"   [ctx] focus: {e}")
        return None
    if not el:
        return None
    txt = _from_textpattern(el, U)
    if txt is None and time.perf_counter() - t0 < budget:
        txt = _from_win32_edit(el, U)
    if time.perf_counter() - t0 > budget:
        log(f"   [ctx] fuori budget ({(time.perf_counter()-t0)*1000:.0f} ms), lascio perdere")
        return None
    return txt


# ------------------------------------------------------------------ la regola
def starts_sentence(prev):
    """True = il dettato apre una frase (maiuscola, come sempre). None = non si sa -> True."""
    if prev is None:
        return True
    # gli spazi orizzontali non contano, il fine riga si': a capo = frase nuova
    tail = prev.rstrip(" \t")
    if not tail:
        return True
    if tail[-1] in "\r\n":
        return True
    i = len(tail) - 1
    while i >= 0 and tail[i] in SKIP_BACK:     # "(sì)" -> guarda la ')' e poi la 'ì'
        i -= 1
    if i < 0:
        return True
    return tail[i] in CLOSERS


def _keep_case(word, keep=()):
    """Parole che non si abbassano: sigle, nomi del dizionario, la 'I' inglese."""
    if not word:
        return True
    if word.isupper() and len(word) > 1:
        return True
    if word == "I":
        return True
    low = word.lower()
    # nel dizionario i nomi possono essere piu' parole ("Fondazione Alberto Genovese"):
    # qui conta la prima, perche' e' quella che finirebbe sotto la lente della minuscola
    return any(low == k.split()[0].lower() for k in keep if k.strip())


def fit(prev, text, keep=()):
    """Il testo da incollare davvero, dato cio' che sta prima del cursore.

    keep = nomi da non abbassare mai (il dizionario dell'utente).
    """
    if not text:
        return text
    if starts_sentence(prev):
        return text
    head = text.lstrip()
    if not head:
        return text
    word = head.split()[0].strip("\"'«([")
    if not _keep_case(word, keep):
        head = head[0].lower() + head[1:]
    if prev and prev[-1] not in NO_SPACE_AFTER:
        head = " " + head
    return head
