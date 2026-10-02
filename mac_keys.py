"""
mac_keys.py — i tasti su macOS: un CGEventTap attivo che tiene lo stato di tasti e modificatori.

Su Windows il worker chiede a GetAsyncKeyState "questo tasto e' giu'?" ogni 20 ms. Su Mac
quella domanda non esiste: si ascoltano gli eventi (flagsChanged, keyDown, keyUp) con un tap
sul suo thread (CFRunLoop proprio) e si tiene un dizionario aggiornato. Il worker a polling
legge quel dizionario e resta uguale nella forma.

Il tap serve anche a "ingoiare" i tasti 1-3 in Edit Mode (su Windows lo fa
keyboard.on_press_key(suppress=True)) e le lettere Q/R/T dentro Ctrl+Option (sono comandi
nostri, non devono arrivare all'app).

Permesso: Accessibilita' (un tap ATTIVO la chiede; Input Monitoring serve solo ai tap in
ascolto). Senza permesso CGEventTapCreate torna None: si logga chiaro e si riprova ogni 2 s.

Qui sopra (KeyState, Hybrid, vk_down) e' logica pura, provata anche su Windows
(tests/test_mac_layer.py). PyObjC (Quartz) si importa solo dentro il thread del tap.
"""
import os
import threading
import time
from collections import deque

# ---- codici tasto macOS (kVK_*, posizioni fisiche ANSI) ----
KC_ESC = 53
KC_Q, KC_R, KC_T = 12, 15, 17
KC_C, KC_V = 8, 9
KC_FN = 63
KC_1, KC_2, KC_3 = 18, 19, 20
KC_PAD1, KC_PAD2, KC_PAD3 = 83, 84, 85
DIGIT_KEYS = {"1": (KC_1, KC_PAD1), "2": (KC_2, KC_PAD2), "3": (KC_3, KC_PAD3)}

# ---- maschere dei modificatori (CGEventFlags) ----
F_SHIFT = 0x00020000      # kCGEventFlagMaskShift
F_CTRL = 0x00040000       # kCGEventFlagMaskControl
F_OPT = 0x00080000        # kCGEventFlagMaskAlternate
F_CMD = 0x00100000        # kCGEventFlagMaskCommand
F_FN = 0x00800000         # kCGEventFlagMaskSecondaryFn

# ---- tipi di evento (CGEventType) ----
T_KEYDOWN, T_KEYUP, T_FLAGS = 10, 11, 12
T_DISABLED_TIMEOUT, T_DISABLED_USER = 0xFFFFFFFE, 0xFFFFFFFF

HOLD_SEC = 0.4            # Fn/Ctrl+Option: sotto = tocco (resta in ascolto), sopra = tieni premuto
# Solo Ctrl+Option: la dettatura parte dopo questa finestra di grazia (o al rilascio, se prima).
# Ctrl+Option e' anche l'inizio di Ctrl+Option+Q/R/T: partendo alla pressione si sentiva il bip e
# si vedeva la card per un attimo prima del comando. Dentro la grazia Q/R/T (o un altro tasto)
# annullano la partenza prima che avvenga. Deve restare sotto HOLD_SEC. Fn resta immediato.
CTRL_OPT_GRACE_SEC = 0.3
RETRY_SEC = 2.0           # tap non creato (permesso mancante): si riprova ogni tanti secondi

# VK di Windows -> stato del Mac, cosi' le funzioni condivise di wavetype.py (_down(vk))
# restano scritte con i codici di sempre.
VK_MAP_KEYS = {0x1B: KC_ESC, 0x51: KC_Q, 0x52: KC_R, 0x54: KC_T}
VK_MAP_FLAGS = {0x11: F_CTRL, 0xA2: F_CTRL, 0xA3: F_CTRL,           # VK_CONTROL, L/R
                0x12: F_OPT, 0xA4: F_OPT, 0xA5: F_OPT,              # VK_MENU (Alt) = Option
                0x5B: F_CMD, 0x5C: F_CMD,                           # VK_LWIN/RWIN = Command
                0x10: F_SHIFT}


class KeyState:
    """Tasti giu' e modificatori, aggiornati dagli eventi del tap. Logica pura.

    I modificatori si prendono SOLO da flagsChanged: un keyDown sintetico (il nostro Cmd+V)
    porta flag inventati (solo Command) e, se li si credesse, Fn tenuto premuto
    risulterebbe rilasciato a meta' dettatura.

    Fn si legge solo dal flagsChanged del tasto 63: il bit F_FN lo accendono anche frecce,
    Home/End e Canc avanti, e un flagsChanged di Shift arrivato con una freccia giu' lo porta.

    edge = (chord giu'?, istante del cambio): l'ora vera del tasto, presa dal thread del tap.
    Il worker misura tocco/tenuto con questa e non con l'ora del suo giro (che si ferma
    finche' la registrazione si apre).

    other = un tasto qualunque premuto mentre il chord e' giu' (Fn+Canc, Fn+frecce,
    Ctrl+Option+frecce): allora Fn era un modificatore, non una dettatura. Si azzera a ogni
    nuova pressione del chord.

    src = chi ha fatto l'ultima pressione del chord: "fn" o "ctrl_option" (Fn vince se sono giu'
    insieme). Il worker fa partire subito Fn e aspetta la grazia per Ctrl+Option.

    cmds = Q/R/T premuti con Ctrl+Option, ed ESC, in coda finche' il worker li prende (take_cmd).
    Il worker li guardava "giu' adesso": mentre apre la registrazione e' fermo, e un tocco piu'
    breve di quel tempo si perdeva (run #2 della CI Mac: Ctrl+Option+Q non chiudeva l'app)."""

    def __init__(self):
        self.keys = set()
        self.flags = 0
        self.fn_down = False
        self.edge = (False, 0.0)
        self.src = ""
        self.other = False
        self.swallow = {}        # keycode -> callback(keycode); keyDown e keyUp non arrivano all'app
        self.chord_letters = (KC_Q, KC_R, KC_T)
        self.cmds = deque(maxlen=16)    # append/popleft atomici: due thread senza lock

    def _set(self, fl, fn, now):
        """Nuovi modificatori. L'ordine conta (un altro thread legge senza lock): prima `other` e
        `src`, poi `edge`, poi i flag; chi vede la nuova pressione in `edge` vede gia' `other`
        azzerato e la sorgente giusta."""
        new = fn or (bool(fl & F_CTRL) and bool(fl & F_OPT))
        if new != self.edge[0]:
            if new:
                self.other = False
                self.src = "fn" if fn else "ctrl_option"
            self.edge = (new, time.perf_counter() if now is None else now)
        self.fn_down = fn
        self.flags = fl

    def handle(self, kind, kc, flags, autorepeat=False, now=None):
        """kind = T_KEYDOWN | T_KEYUP | T_FLAGS. Torna True se l'evento va ingoiato."""
        if kind == T_FLAGS:
            fl = int(flags)
            self._set(fl, bool(fl & F_FN) if kc == KC_FN else self.fn_down, now)
            return False
        if kind == T_KEYDOWN:
            self.keys.add(kc)
            if not autorepeat and (kc == KC_ESC or (kc in self.chord_letters
                                                    and self.ctrl_option())):
                self.cmds.append(kc)
            if not autorepeat and self.edge[0] and kc != KC_ESC and kc not in self.swallow \
                    and not (kc in self.chord_letters and self.ctrl_option()):
                self.other = True
        elif kind == T_KEYUP:
            self.keys.discard(kc)
        else:
            return False
        cb = self.swallow.get(kc)
        if cb is not None:
            if kind == T_KEYDOWN and not autorepeat:
                try:
                    cb(kc)
                except Exception:
                    pass
            return True
        # Ctrl+Option+Q/R/T sono comandi di Wavetype: l'app sotto non li vede
        return kc in self.chord_letters and self.ctrl_option()

    def down(self, kc):
        return kc in self.keys

    def take_cmd(self):
        """Il prossimo ESC o Q/R/T premuto con Ctrl+Option (keycode), o None. Una volta sola."""
        try:
            return self.cmds.popleft()
        except IndexError:
            return None

    def resync(self, flags, fn, now=None):
        """Il tap e' stato spento da macOS: gli eventi di quel tempo sono persi. Tasti svuotati
        (un keyUp perso lascerebbe Q "giu'" per sempre: Ctrl+Option chiuderebbe l'app) e
        modificatori riletti dal sistema."""
        self.keys = set()
        self.cmds.clear()
        self._set(int(flags), bool(fn), now)

    def fn(self):
        return self.fn_down

    def ctrl_option(self):
        return bool(self.flags & F_CTRL) and bool(self.flags & F_OPT)

    def chord(self):
        """La scorciatoia di dettatura: Fn (Globe), o Ctrl+Option per tastiere senza Fn Apple."""
        return self.fn() or self.ctrl_option()

    def vk_down(self, vk):
        if vk in VK_MAP_KEYS:
            return self.down(VK_MAP_KEYS[vk])
        f = VK_MAP_FLAGS.get(vk)
        return bool(f and self.flags & f)


class Hybrid:
    """Un tasto, due modi (spec mac): premi e rilasci in fretta (< hold s) = resti in ascolto
    finche' ripremi (toggle, come Win+Ctrl); tieni premuto >= hold s = parli finche' tieni, al
    rilascio si ferma. feed() torna "start", "stop" o None. Logica pura.
    `now` e' l'istante del cambio del tasto (KeyState.edge), non l'ora in cui lo si guarda.

    Stati: idle -> pressed (start) -> rilascio presto: latched | rilascio tardi: idle (stop)
           latched -> pressione: closing (stop) -> rilascio: idle
    reset(): la registrazione e' finita da un'altra parte (ESC, tasto 1-3, comando): si torna
    a riposo, e se il tasto e' ancora giu' il suo rilascio non conta."""

    def __init__(self, hold=HOLD_SEC):
        self.hold = hold
        self.state = "idle"
        self.t = 0.0
        self.prev = False

    def feed(self, down, now):
        ev = None
        if down and not self.prev:
            if self.state == "idle":
                self.state, self.t, ev = "pressed", now, "start"
            elif self.state == "latched":
                self.state, ev = "closing", "stop"
        elif not down and self.prev:
            if self.state == "pressed":
                if now - self.t < self.hold:
                    self.state = "latched"
                else:
                    self.state, ev = "idle", "stop"
            elif self.state == "closing":
                self.state = "idle"
        self.prev = bool(down)
        return ev

    def active(self):
        """True se per la macchina una registrazione e' in corso."""
        return self.state in ("pressed", "latched")

    def reset(self):
        self.state = "closing" if self.prev else "idle"


# ------------------------------------------------------------------ il tap (solo macOS)
STATE = KeyState()
_tap = {"status": "off", "why": "", "thread": None, "stop": False, "tap": None}
_LOG = [print]


def set_log(fn):
    _LOG[0] = fn


def log(msg):
    try:
        _LOG[0](msg)
    except Exception:
        pass


def status():
    """("off" | "starting" | "active" | "failed", motivo)."""
    return _tap["status"], _tap["why"]


def vk_down(vk):
    return STATE.vk_down(vk)


def chord_down():
    return STATE.chord()


def chord_edge():
    """(chord giu'?, istante dell'ultimo cambio), letti insieme (una tupla sola)."""
    return STATE.edge


def chord_source():
    """"fn" o "ctrl_option": chi ha fatto l'ultima pressione del chord (KeyState.src)."""
    return STATE.src


def down(kc):
    return STATE.down(kc)


def take_cmd():
    return STATE.take_cmd()


def set_swallow(mapping):
    """{keycode: callback} da ingoiare (Edit Mode: 1-3), {} per smettere. Atomico: si
    sostituisce il dizionario intero, il thread del tap legge sempre uno coerente."""
    STATE.swallow = dict(mapping or {})


def start(log_fn=None):
    """Avvia il thread del tap (una volta sola). Non blocca e non solleva."""
    if log_fn is not None:
        set_log(log_fn)
    if _tap["thread"] is not None:
        return
    _tap["stop"] = False
    _tap["status"] = "starting"
    th = threading.Thread(target=_run, name="mac-keys", daemon=True)
    _tap["thread"] = th
    th.start()


def stop():
    _tap["stop"] = True


def _run():
    try:
        import Quartz
    except Exception as e:
        _tap["status"], _tap["why"] = "failed", f"Quartz non disponibile: {e}"
        log(f"[tasti] {_tap['why']}")
        return
    me = os.getpid()
    mask = (1 << T_KEYDOWN) | (1 << T_KEYUP) | (1 << T_FLAGS)

    def resync():
        """Dopo un tap spento: modificatori riletti dal sistema, tasti svuotati. Fn = bit F_FN
        E tasto 63 giu': se uno dei due dice "su" vale su (meglio fermare una dettatura tenuta
        che lasciarne una fantasma aperta)."""
        try:
            hid = Quartz.kCGEventSourceStateHIDSystemState
            fl = int(Quartz.CGEventSourceFlagsState(hid))
            fn = bool(fl & F_FN) and bool(Quartz.CGEventSourceKeyState(hid, KC_FN))
            STATE.resync(fl, fn)
        except Exception:
            STATE.keys = set()

    def callback(proxy, etype, event, refcon):
        try:
            if etype in (T_DISABLED_TIMEOUT, T_DISABLED_USER):
                # macOS spegne un tap che risponde lento (o su input sicuro): lo si riaccende
                t = _tap["tap"]
                if t is not None:
                    Quartz.CGEventTapEnable(t, True)
                    resync()
                return event
            # gli eventi che posta Wavetype stesso (Cmd+V, Cmd+C) non cambiano lo stato
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID) == me:
                return event
            kc = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
            rep = bool(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat))
            fl = int(Quartz.CGEventGetFlags(event))
            if STATE.handle(int(etype), kc, fl, autorepeat=rep):
                return None                  # ingoiato: l'app sotto non lo vede
        except Exception:
            pass                             # niente log qui: il callback deve essere istantaneo
        return event

    warned = False
    while not _tap["stop"]:
        tap = None
        try:
            tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                          Quartz.kCGEventTapOptionDefault, mask, callback, None)
        except Exception as e:
            _tap["why"] = f"CGEventTapCreate: {e}"
        if tap is None:
            if not _tap["why"].startswith("CGEventTapCreate:"):
                _tap["why"] = ("tap dei tasti non creato: manca il permesso Accessibilita' "
                               "(Impostazioni di Sistema > Privacy e sicurezza > Accessibilita')")
            if not warned:
                log(f"[tasti] {_tap['why']} — riprovo ogni {RETRY_SEC:.0f} s")
                warned = True
            _tap["status"] = "failed"
            time.sleep(RETRY_SEC)
            continue
        _tap["tap"] = tap
        try:
            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(loop, src, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
        except Exception as e:
            _tap["status"], _tap["why"] = "failed", f"run loop del tap: {e}"
            log(f"[tasti] {_tap['why']} — riprovo fra {RETRY_SEC:.0f} s")
            _tap["tap"] = None
            time.sleep(RETRY_SEC)
            continue
        _tap["status"], _tap["why"] = "active", ""
        log("[tasti] tap attivo: Fn o Ctrl+Option per dettare")
        while not _tap["stop"]:
            # a fette da 1 s: cosi' stop() viene visto, e un tap morto si ricrea
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 1.0, False)
            try:
                if not Quartz.CGEventTapIsEnabled(tap):
                    Quartz.CGEventTapEnable(tap, True)
                    resync()
            except Exception:
                pass
        try:
            Quartz.CGEventTapEnable(tap, False)
            Quartz.CFRunLoopRemoveSource(loop, src, Quartz.kCFRunLoopCommonModes)
        except Exception:
            pass
    _tap["status"] = "off"
